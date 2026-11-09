"""Inject the memory index into each harness's instruction file.

- CLAUDE.md (Claude Code)    -> `@memory/MEMORY.md` import, loaded at launch
- GEMINI.md (Gemini CLI)     -> `@memory/MEMORY.md` import, loaded at launch
- AGENTS.md (Codex, Cursor, GitHub Copilot, Windsurf, Amp, opencode, Zed, ...)
  -> an inline copy, because AGENTS.md has no import syntax. The copy is
  refreshed whenever memory changes (`sync`).

Only a managed block between markers is ever written; the rest of each file is
left alone. Claude Code reads CLAUDE.md instead of AGENTS.md when both exist,
so the index is not injected twice by default.
"""

import re
from pathlib import Path
from typing import Dict, List

from dreamer import repo, store
from dreamer.config import DreamerConfig

BLOCK_BEGIN = "<!-- dreamer:begin (managed by dreamer; edits inside this block are overwritten) -->"
BLOCK_END = "<!-- dreamer:end -->"
FILES: Dict[str, str] = {"claude": "CLAUDE.md", "agents": "AGENTS.md", "gemini": "GEMINI.md"}
_BLOCK = re.compile(re.escape(BLOCK_BEGIN) + r".*?" + re.escape(BLOCK_END), re.DOTALL)
_LINK = re.compile(r"(\[[^\]]*\]\()([^)\s]+)(\))")

NOTE_HINT = ('If you learn something a future session here would need and it may not be obvious from this '
             'session\'s record, save it with `dreamer note "<what and why>"`. '
             'When asked to consolidate memory ("dream"), run `dreamer dream start` and follow its instructions.')


def _rebase_links(text: str, d: str) -> str:
    def fix(m):
        target = m.group(2)
        if re.match(r"^[a-z]+:", target) or target.startswith(("#", "/")):
            return m.group(0)
        return f"{m.group(1)}{d}/{target}{m.group(3)}"
    return _LINK.sub(fix, text)


def block(kind: str, root: Path, cfg: DreamerConfig) -> str:
    d = cfg.memory.dir.rstrip("/")
    about = (f"This is the project's long-term memory, distilled between sessions from what happened in them. "
             f"Detail lives in topic files under `{d}/`; open one when its topic matters. Notes are predictions, "
             f"not guarantees: if one turns out wrong, correct it")
    if kind in ("claude", "gemini"):
        body = f"## Project memory\n@{d}/{store.INDEX}\n\n{about} in `{d}/` (links there are relative to the linking file)."
        if kind == "gemini":
            body += f"\n{NOTE_HINT}"
    else:
        idx = root / d / store.INDEX
        index = idx.read_text(encoding="utf-8") if idx.exists() else ""
        index = re.sub(r"\A\s*# [^\n]*\n?", "", index)  # the block has its own heading
        index = re.sub(r"^(#+) ", r"#\1 ", _rebase_links(index, d), flags=re.MULTILINE).strip()
        body = (f"## Project memory\n{about} in `{d}/`, not here: this block is a copy of `{d}/{store.INDEX}`.\n"
                f"{NOTE_HINT}" + (f"\n\n{index}" if index else ""))
    return f"{BLOCK_BEGIN}\n{body}\n{BLOCK_END}"


def upsert(path: Path, content: str) -> bool:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if _BLOCK.search(text):
        new = _BLOCK.sub(lambda _: content, text, count=1)
    else:
        new = (text.rstrip() + "\n\n" if text.strip() else "") + content + "\n"
    if new == text:
        return False
    repo.atomic_write(path, new)
    return True


def remove(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    new = re.sub(r"\n*" + _BLOCK.pattern + r"\n?", "\n", text, flags=re.DOTALL).strip()
    if new == text.strip():
        return False
    if new:
        repo.atomic_write(path, new + "\n")
    else:
        path.unlink()
    return True


def installed(root: Path) -> List[str]:
    out = []
    for kind, name in FILES.items():
        p = root / name
        if p.exists() and BLOCK_BEGIN in p.read_text(encoding="utf-8", errors="replace"):
            out.append(kind)
    return out


def sync(root: Path, cfg: DreamerConfig) -> List[str]:
    """Refresh every installed block (the AGENTS.md copy follows MEMORY.md)."""
    return [FILES[k] for k in installed(root) if upsert(root / FILES[k], block(k, root, cfg))]


def stale(root: Path, cfg: DreamerConfig) -> List[str]:
    out = []
    for k in installed(root):
        m = _BLOCK.search((root / FILES[k]).read_text(encoding="utf-8"))
        if m and m.group(0) != block(k, root, cfg):
            out.append(FILES[k])
    return out
