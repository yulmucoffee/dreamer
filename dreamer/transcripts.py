"""Incremental, repo-scoped reading of coding-agent session transcripts.

Sources:
- Claude Code: ~/.claude/projects/<path-slug>/*.jsonl (or $CLAUDE_CONFIG_DIR)
- Codex CLI:   ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl (or $CODEX_HOME)
- Captured:    .dreamer/sessions/*.jsonl, written by harness hooks through
               `dreamer capture` (Cursor; any harness with hooks)
Any agent can also leave explicit observations with `dreamer note`.

Isolation: a record is used only when the working directory it belongs to
lies inside this repository (path-boundary check, so `proj` never matches
`proj-other`). Claude Code records carry their own `cwd`; Codex records
inherit the session's `cwd` from its header, and a Codex session whose header
points elsewhere is marked foreign after reading that one line.

Token efficiency: transcripts are dominated by reasoning, file dumps and tool
output. The digest keeps what memory is made of (what the user asked and
corrected, what was decided, which commands ran and what failed, commit
messages) and clips the rest. The full text stays reachable via `search`.
"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from dreamer.config import DreamerConfig

# Every dreaming pass (headless engine prompt, `dreamer dream start` output)
# carries this marker, so a pass is never fed back into a later pass.
MARKER = "[dreamer:dream-pass]"

USER_CLIP = 2000
ASSISTANT_CLIP = 1500
TOOL_INPUT_CLIP = 160
RESULT_OK_CLIP = 140
RESULT_ERR_CLIP = 600
# Commit messages record what changed and why, in the author's own summary:
# the best evidence a later pass has for checking a note, so keep them.
COMMIT_CLIP = 1500
SEARCH_CONTEXT = 300
SEARCH_TOTAL = 6000

_WRAPPERS = re.compile(
    r"<(system-reminder|local-command-stdout|local-command-stderr|command-message|"
    r"user-prompt-submit-hook|persisted-output|environment_context|user_instructions)\b[^>]*>.*?</\1>",
    re.DOTALL,
)
# Context that harnesses inject as "user" messages; not something the user said.
_INJECTED_PREFIXES = ("# AGENTS.md instructions", "<environment_context>", "<user_instructions>", "<permissions")
_COMMIT = r"\bgit\b[^|;&\n]*?\bcommit\b[^|;&\n]*?"
_HEREDOC_BODY = r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n(.*?)\n\s*\1\b"
# The heredoc must feed the commit itself: `-F - <<EOF` or `-m "$(cat <<EOF ...)"`.
_COMMIT_FILE = re.compile(_COMMIT + r"\s-[a-zA-Z]*F\s*-\s[^|;&\n]*?" + _HEREDOC_BODY, re.DOTALL)
_COMMIT_CAT = re.compile(_COMMIT + r"\s-[a-zA-Z]*m\s*\"\$\(cat\s*" + _HEREDOC_BODY, re.DOTALL)
_COMMIT_M = re.compile(_COMMIT + r"\s-[a-zA-Z]*m\s*(\"(?:[^\"\\]|\\.)*\"|'[^']*'|[^\s'\"]+)")
_TRAILER = re.compile(r"^\s*(Co-Authored-By|Signed-off-by|Claude-Session):.*$\n?", re.IGNORECASE | re.MULTILINE)
_INPUT_KEYS = ("command", "cmd", "file_path", "path", "pattern", "url", "query", "description", "skill")
_EXIT_FAIL = re.compile(r'"exit_code"\s*:\s*[1-9]|\bExit code:?\s*[1-9]|exited with code [1-9]', re.IGNORECASE)
_PATCH_FILES = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)


def commit_message(cmd: str) -> str:
    """The message of a `git commit` in a shell command, without trailers."""
    m = _COMMIT_FILE.search(cmd) or _COMMIT_CAT.search(cmd)
    if m:
        msg = m.group(2)
    else:
        m = _COMMIT_M.search(cmd)
        if not m:
            return ""
        msg = m.group(1)
        if msg[:1] in "\"'":
            msg = msg[1:-1]
    return _TRAILER.sub("", msg).strip()


# -- shared rendering helpers ----------------------------------------------------

def in_repo(cwd: Any, root: Path) -> bool:
    if not isinstance(cwd, str) or not cwd:
        return False
    try:
        p = Path(os.path.realpath(cwd))
    except (OSError, ValueError):
        return False
    return p == root or root in p.parents


def _clip(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else text[:n] + f" …[+{len(text) - n} chars]"


def _keep(text: str, n: int) -> str:
    return text.strip()


def _oneline(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _clean_user(text: str) -> str:
    if text.lstrip().startswith(_INJECTED_PREFIXES):
        return ""
    return _WRAPPERS.sub("", text).strip()


def _as_cmd(v: Any) -> str:
    return " ".join(map(str, v)) if isinstance(v, list) else str(v)


def _tool_summary(name: str, inp: Any) -> str:
    if isinstance(inp, dict):
        cmd = inp.get("command", inp.get("cmd"))
        msg = commit_message(_as_cmd(cmd)) if cmd is not None else ""
        parts = [f"{k}={_oneline(_as_cmd(inp[k]))}" for k in _INPUT_KEYS if k in inp][:2]
        if parts:
            out = f"{name}: " + _clip(" ".join(parts), TOOL_INPUT_CLIP)
            if msg:
                out += "\n    commit message: " + _clip(msg, COMMIT_CLIP).replace("\n", "\n    ")
            return out
    return f"{name}: " + _clip(_oneline(json.dumps(inp, ensure_ascii=False)), TOOL_INPUT_CLIP)


def _tool_line(name: str, inp: Any, full: bool) -> str:
    if isinstance(inp, dict) and "dreamer dream " in _as_cmd(inp.get("command", inp.get("cmd", ""))):
        return ""  # the mechanics of a dreaming pass are not session activity
    return "→ " + (f"{name}: {json.dumps(inp, ensure_ascii=False)}" if full else _tool_summary(name, inp))


def _result_line(text: str, is_error: bool, full: bool) -> Optional[str]:
    if not text.strip() or MARKER in text:
        return None
    if is_error:
        return "  ✗ " + (text.strip() if full else _clip(text, RESULT_ERR_CLIP))
    return "  ⇢ " + (text.strip() if full else _clip(_oneline(text), RESULT_OK_CLIP))


def _said(who: str, text: str, full: bool) -> str:
    return f"{who}: " + (_keep if full else _clip)(text, USER_CLIP if who == "USER" else ASSISTANT_CLIP)


# -- Claude Code -------------------------------------------------------------------

def claude_dir(cfg: DreamerConfig) -> Path:
    d = cfg.transcripts.claude_dir or os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    return Path(d).expanduser()


def slug(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _claude_files(root: Path, cfg: DreamerConfig) -> List[Path]:
    projects = claude_dir(cfg) / "projects"
    prefix = slug(root)[:150]
    if not projects.is_dir():
        return []
    files: List[Path] = []
    for d in projects.iterdir():
        if d.is_dir() and d.name.startswith(prefix):
            files.extend(p for p in d.glob("*.jsonl") if p.is_file() and not p.is_symlink())
    return files


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") in ("text", "input_text"))
    return ""


def render(record: Dict[str, Any], full: bool = False) -> List[str]:
    """Render one Claude Code record. `full=True` skips clipping (for search)."""
    if record.get("isSidechain") or record.get("isMeta"):
        return []
    kind = record.get("type")
    msg = record.get("message")
    if kind not in ("user", "assistant") or not isinstance(msg, dict):
        return []
    content = msg.get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
    if not isinstance(blocks, list):
        return []
    out: List[str] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            text = _clean_user(b.get("text", "")) if kind == "user" else b.get("text", "")
            if text.strip():
                out.append(_said("USER" if kind == "user" else "ASSISTANT", text, full))
        elif t == "tool_use":
            out.append(_tool_line(str(b.get("name")), b.get("input"), full))
        elif t == "tool_result":
            line = _result_line(_result_text(b.get("content")), bool(b.get("is_error")), full)
            if line:
                out.append(line)
    return out


def _user_texts(rec: Dict[str, Any]) -> str:
    msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _skip_session(ctx: Dict[str, Any], sid: str, text: str) -> bool:
    """True for a dreaming pass's own session (and remembers it)."""
    skipped = ctx.setdefault("skip", [])
    if sid in skipped:
        return True
    if MARKER in text:
        skipped.append(sid)
        return True
    return False


def _claude_parse(rec: Dict[str, Any], ctx: Dict[str, Any], root: Path, full: bool):
    if not in_repo(rec.get("cwd"), root):
        return None
    sid = str(rec.get("sessionId", ""))
    if _skip_session(ctx, sid, _user_texts(rec) if rec.get("type") == "user" else ""):
        return None
    return render(rec, full), sid, str(rec.get("timestamp", "")), rec.get("gitBranch")


# -- Codex CLI -----------------------------------------------------------------------

def codex_dir(cfg: DreamerConfig) -> Path:
    d = cfg.transcripts.codex_dir or os.environ.get("CODEX_HOME") or "~/.codex"
    return Path(d).expanduser()


def _codex_files(root: Path, cfg: DreamerConfig) -> List[Path]:
    sessions = codex_dir(cfg) / "sessions"
    if not sessions.is_dir():
        return []
    # Plain .jsonl only: Codex later compresses old rollouts to .jsonl.zst, by
    # which time they have normally been dreamed already.
    return [p for p in sessions.rglob("rollout-*.jsonl") if p.is_file() and not p.is_symlink()]


def _codex_output(out: Any) -> str:
    if isinstance(out, str):
        try:  # older rollouts wrap shell output as {"output": ..., "metadata": {...}}
            j = json.loads(out)
            if isinstance(j, dict) and "output" in j:
                code = (j.get("metadata") or {}).get("exit_code")
                return str(j["output"]) + (f"\nExit code: {code}" if code else "")
        except ValueError:
            pass
        return out
    return _result_text(out)


def render_codex(p: Dict[str, Any], full: bool = False) -> List[str]:
    t = p.get("type")
    if t == "message":
        role = p.get("role")
        if role not in ("user", "assistant"):
            return []
        texts = [c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict) and c.get("type") in ("input_text", "output_text", "text")]
        out = []
        for text in texts:
            text = _clean_user(text) if role == "user" else text
            if text.strip():
                out.append(_said("USER" if role == "user" else "ASSISTANT", text, full))
        return out
    if t == "function_call":
        args: Any = p.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                pass
        return [_tool_line(str(p.get("name")), args, full)]
    if t == "local_shell_call":
        return [_tool_line("shell", {"command": (p.get("action") or {}).get("command")}, full)]
    if t == "custom_tool_call":
        inp = str(p.get("input", ""))
        files = _PATCH_FILES.findall(inp)
        if files and not full:
            return [f"→ {p.get('name')}: files=" + _clip(", ".join(files), TOOL_INPUT_CLIP)]
        return [_tool_line(str(p.get("name")), inp if full else _clip(_oneline(inp), TOOL_INPUT_CLIP), True)]
    if t in ("function_call_output", "custom_tool_call_output"):
        text = _codex_output(p.get("output"))
        line = _result_line(text, bool(_EXIT_FAIL.search(text)), full)
        return [line] if line else []
    if t == "web_search_call":
        action = p.get("action") or {}
        return [_tool_line("web_search", {"query": action.get("query", "")}, full)]
    return []


def _codex_parse(rec: Dict[str, Any], ctx: Dict[str, Any], root: Path, full: bool):
    t, p = rec.get("type"), rec.get("payload")
    if not isinstance(p, dict):
        return None
    if t == "session_meta":
        ctx["cwd"] = p.get("cwd")
        ctx["sid"] = str(p.get("id") or p.get("session_id") or "")
        ctx["branch"] = (p.get("git") or {}).get("branch")
        if not in_repo(ctx["cwd"], root):
            ctx["foreign"] = True
        return None
    if t == "turn_context":
        if p.get("cwd"):
            ctx["cwd"] = p["cwd"]
        return None
    if t != "response_item" or not in_repo(ctx.get("cwd"), root):
        return None
    if p.get("type") == "message" and p.get("role") == "user" and MARKER in json.dumps(p.get("content"), ensure_ascii=False):
        ctx["foreign"] = True  # a dreaming pass run through Codex: never read it
        return None
    return render_codex(p, full), ctx.get("sid", ""), str(rec.get("timestamp", "")), ctx.get("branch")


# -- captured sessions (Cursor hooks, generic hooks) --------------------------------

def _captured_files(root: Path, cfg: DreamerConfig) -> List[Path]:
    d = root / ".dreamer" / "sessions"
    return [p for p in d.glob("*.jsonl") if p.is_file() and not p.is_symlink()] if d.is_dir() else []


def _captured_parse(rec: Dict[str, Any], ctx: Dict[str, Any], root: Path, full: bool):
    if not in_repo(rec.get("cwd"), root):
        return None
    sid, kind = str(rec.get("session", "")), rec.get("kind")
    if _skip_session(ctx, sid, str(rec.get("text", "")) if kind == "user" else ""):
        return None
    if kind in ("user", "assistant"):
        text = _clean_user(str(rec.get("text", ""))) if kind == "user" else str(rec.get("text", ""))
        lines = [_said(kind.upper(), text, full)] if text.strip() else []
    elif kind == "tool":
        lines = [_tool_line(str(rec.get("name", "tool")), rec.get("input"), full)]
    elif kind == "result":
        out = str(rec.get("output", ""))
        line = _result_line(out, bool(rec.get("error")) or bool(_EXIT_FAIL.search(out)), full)
        lines = [line] if line else []
    else:
        lines = []
    return lines, sid, str(rec.get("ts", "")), None


# -- sources -------------------------------------------------------------------------

Parser = Callable[[Dict[str, Any], Dict[str, Any], Path, bool], Optional[Tuple[List[str], str, str, Optional[str]]]]
SOURCES: Dict[str, Tuple[Callable[[Path, DreamerConfig], List[Path]], Parser]] = {
    "claude": (_claude_files, _claude_parse),
    "codex": (_codex_files, _codex_parse),
    "captured": (_captured_files, _captured_parse),
}


def candidate_files(root: Path, cfg: DreamerConfig) -> List[Tuple[str, Path]]:
    out = []
    for name in cfg.transcripts.sources:
        if name in SOURCES:
            out.extend((name, p) for p in SOURCES[name][0](root, cfg))
    return sorted(out, key=lambda sp: (sp[1].stat().st_mtime, str(sp[1])))


def _lines_from(path: Path, offset: int) -> Iterator[Tuple[int, bytes]]:
    """Yield (end_offset, line) for complete lines after `offset`. A trailing
    partial line (session still being written) is left for the next pass."""
    with open(path, "rb") as f:
        f.seek(offset)
        pos = offset
        for line in f:
            if not line.endswith(b"\n"):
                return
            pos += len(line)
            yield pos, line


@dataclass
class Batch:
    text: str = ""
    cursors: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # file -> new cursor entry
    sessions: List[str] = field(default_factory=list)
    pending: bool = False  # more material left beyond this batch
    notes: List[str] = field(default_factory=list)  # note files included (consumed on success)


def collect(root: Path, cfg: DreamerConfig, cursors: Dict[str, Any], budget: int, used: int = 0) -> Batch:
    """Digest new transcript material (oldest first) up to `budget` chars."""
    batch = Batch()
    parts: List[str] = []
    for source, path in candidate_files(root, cfg):
        key = str(path)
        entry = cursors.get(key, {})
        offset = int(entry.get("offset", 0))
        ctx = dict(entry.get("ctx", {}))
        size = path.stat().st_size
        if size < offset:  # file was rewritten; start over
            offset, ctx = 0, {}
        if ctx.get("foreign"):
            if size != offset:
                batch.cursors[key] = {"offset": size, "ctx": ctx}
            continue
        if size == offset:
            continue
        parse = SOURCES[source][1]
        current: Optional[str] = None
        new_offset = offset
        for end, raw in _lines_from(path, offset):
            try:
                rec = json.loads(raw)
            except ValueError:
                new_offset = end
                continue
            got = parse(rec, ctx, root, False) if isinstance(rec, dict) else None
            if ctx.get("foreign"):  # another repo's session: skip the rest unread
                new_offset = size
                break
            if got and any(got[0]):
                lines, sid, ts, branch = got
                lines = [l for l in lines if l]
                header = []
                if sid != current:
                    current = sid
                    header = [f"\n### Session {sid[:8]} · {ts[:10]} · branch {branch or '-'} · {source}"
                              + (" (continued)" if offset else "")]
                    if sid[:8] not in batch.sessions:
                        batch.sessions.append(sid[:8])
                chunk = "\n".join(header + lines)
                if used + len(chunk) > budget and used > 0:
                    batch.pending = True
                    break
                parts.append(chunk)
                used += len(chunk)
            new_offset = end
        if new_offset != offset or ctx != entry.get("ctx", {}):
            batch.cursors[key] = {"offset": new_offset, "ctx": ctx}
        if batch.pending:
            break
    batch.text = "\n".join(parts).strip()
    return batch


def search(root: Path, cfg: DreamerConfig, pattern: str, max_results: int = 8) -> str:
    """Search the full (unclipped) transcripts of this repository only."""
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error:
        rx = re.compile(re.escape(pattern), re.IGNORECASE)
    max_results = max(1, min(int(max_results or 8), 20))
    hits: List[str] = []
    total = 0
    for source, path in reversed(candidate_files(root, cfg)):  # newest first
        parse, ctx = SOURCES[source][1], {}
        for _, raw in _lines_from(path, 0):
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            got = parse(rec, ctx, root, True) if isinstance(rec, dict) else None
            if ctx.get("foreign"):
                break
            if not got or not any(got[0]):
                continue
            text = "\n".join(l for l in got[0] if l)
            m = rx.search(text)
            if m:
                a, b = max(0, m.start() - SEARCH_CONTEXT), min(len(text), m.end() + SEARCH_CONTEXT)
                hit = (f"[{source} session {got[1][:8]} · {got[2][:16]}]\n"
                       f"{'…' if a else ''}{text[a:b]}{'…' if b < len(text) else ''}")
                hits.append(hit)
                total += len(hit)
            if len(hits) >= max_results or total >= SEARCH_TOTAL:
                return "\n\n".join(hits)[:SEARCH_TOTAL]
    return "\n\n".join(hits)[:SEARCH_TOTAL] if hits else "No matches in this repository's transcripts."
