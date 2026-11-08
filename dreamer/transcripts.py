"""Incremental, repo-scoped reading of Claude Code session transcripts.

Isolation: transcripts are only considered when the directory name matches
this repository's path slug AND each record's own `cwd` lies inside the
repository root. A slug collision (e.g. /a/b-c vs /a/b/c) or a neighbouring
repository with a common prefix is therefore filtered out record by record.

Token efficiency: raw transcripts are dominated by thinking blocks, file
dumps and tool output. The digest keeps what memory is made of (what the user
asked and corrected, what was decided, which commands ran and what failed)
and clips the rest. The full raw text stays reachable via `search`.
"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from dreamer.config import DreamerConfig

USER_CLIP = 2000
ASSISTANT_CLIP = 1500
TOOL_INPUT_CLIP = 160
RESULT_OK_CLIP = 140
RESULT_ERR_CLIP = 600
SEARCH_CONTEXT = 300
SEARCH_TOTAL = 6000

_WRAPPERS = re.compile(
    r"<(system-reminder|local-command-stdout|local-command-stderr|command-message|"
    r"user-prompt-submit-hook|persisted-output)\b[^>]*>.*?</\1>",
    re.DOTALL,
)
_INPUT_KEYS = ("command", "file_path", "path", "pattern", "url", "query", "description", "skill")


def claude_dir(cfg: DreamerConfig) -> Path:
    d = cfg.transcripts.claude_dir or os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    return Path(d).expanduser()


def slug(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def candidate_files(root: Path, cfg: DreamerConfig) -> List[Path]:
    projects = claude_dir(cfg) / "projects"
    prefix = slug(root)[:150]
    if not projects.is_dir():
        return []
    files = []
    for d in projects.iterdir():
        if d.is_dir() and d.name.startswith(prefix):
            files.extend(p for p in d.glob("*.jsonl") if p.is_file() and not p.is_symlink())
    return sorted(files, key=lambda p: (p.stat().st_mtime, str(p)))


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


def _oneline(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _clean_user(text: str) -> str:
    return _WRAPPERS.sub("", text).strip()


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _tool_summary(name: str, inp: Any) -> str:
    if isinstance(inp, dict):
        parts = [f"{k}={_oneline(str(inp[k]))}" for k in _INPUT_KEYS if k in inp][:2]
        if parts:
            return f"{name}: " + _clip(" ".join(parts), TOOL_INPUT_CLIP)
    return f"{name}: " + _clip(_oneline(json.dumps(inp, ensure_ascii=False)), TOOL_INPUT_CLIP)


def render(record: Dict[str, Any], full: bool = False) -> List[str]:
    """Render one transcript record. `full=True` skips clipping (for search)."""
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

    clip = (lambda t, n: t.strip()) if full else _clip
    out: List[str] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            text = _clean_user(b.get("text", "")) if kind == "user" else b.get("text", "")
            if text.strip():
                who = "USER" if kind == "user" else "ASSISTANT"
                out.append(f"{who}: " + clip(text, USER_CLIP if kind == "user" else ASSISTANT_CLIP))
        elif t == "tool_use":
            inp = b.get("input")
            out.append("→ " + (f"{b.get('name')}: {json.dumps(inp, ensure_ascii=False)}" if full else _tool_summary(str(b.get("name")), inp)))
        elif t == "tool_result":
            text = _result_text(b.get("content"))
            if not text.strip():
                continue
            if b.get("is_error"):
                out.append("  ✗ " + clip(text, RESULT_ERR_CLIP))
            else:
                out.append("  ⇢ " + (text.strip() if full else _clip(_oneline(text), RESULT_OK_CLIP)))
    return out


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
    offsets: Dict[str, int] = field(default_factory=dict)  # file -> new cursor
    sessions: List[str] = field(default_factory=list)
    pending: bool = False  # more material left beyond this batch


def collect(root: Path, cfg: DreamerConfig, cursors: Dict[str, Any], budget: int) -> Batch:
    """Digest new transcript material (oldest first) up to `budget` chars."""
    batch = Batch()
    parts: List[str] = []
    used = 0
    for path in candidate_files(root, cfg):
        key = str(path)
        offset = int(cursors.get(key, {}).get("offset", 0))
        size = path.stat().st_size
        if size < offset:  # file was rewritten; start over
            offset = 0
        if size == offset:
            continue
        current_session: Optional[str] = None
        new_offset = offset
        for end, raw in _lines_from(path, offset):
            try:
                rec = json.loads(raw)
            except ValueError:
                new_offset = end
                continue
            lines = render(rec) if in_repo(rec.get("cwd"), root) else []
            if lines:
                sid = str(rec.get("sessionId", path.stem))
                header = []
                if sid != current_session:
                    current_session = sid
                    ts = str(rec.get("timestamp", ""))[:10]
                    branch = rec.get("gitBranch") or "-"
                    header = [f"\n### Session {sid[:8]} · {ts} · branch {branch}" + (" (continued)" if offset else "")]
                    if sid[:8] not in batch.sessions:
                        batch.sessions.append(sid[:8])
                chunk = "\n".join(header + lines)
                if used + len(chunk) > budget and used > 0:
                    batch.pending = True
                    break
                parts.append(chunk)
                used += len(chunk)
            new_offset = end
        if new_offset != offset:
            batch.offsets[key] = new_offset
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
    for path in reversed(candidate_files(root, cfg)):  # newest first
        for _, raw in _lines_from(path, 0):
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if not in_repo(rec.get("cwd"), root):
                continue
            text = "\n".join(render(rec, full=True))
            for m in rx.finditer(text):
                a, b = max(0, m.start() - SEARCH_CONTEXT), min(len(text), m.end() + SEARCH_CONTEXT)
                hit = (
                    f"[session {str(rec.get('sessionId', ''))[:8]} · {str(rec.get('timestamp', ''))[:16]}]\n"
                    f"{'…' if a else ''}{text[a:b]}{'…' if b < len(text) else ''}"
                )
                hits.append(hit)
                total += len(hit)
                break  # one hit per record keeps results diverse
            if len(hits) >= max_results or total >= SEARCH_TOTAL:
                return "\n\n".join(hits)[:SEARCH_TOTAL]
    return "\n\n".join(hits)[:SEARCH_TOTAL] if hits else "No matches in this repository's transcripts."
