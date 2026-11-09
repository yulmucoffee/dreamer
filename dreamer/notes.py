"""`dreamer note`: memory input from any harness or model.

Agents in harnesses whose transcripts dreamer cannot read (Cursor, Gemini CLI,
Copilot, ...) - or any agent that wants to flag something explicitly - record
an observation with `dreamer note "..."`. Each note is its own file under
.dreamer/inbox/ (inside the working tree, so sandboxed agents can write it;
kept out of commits via .git/info/exclude). The next dreaming pass reads the
notes before transcripts and deletes them once the pass is applied.
"""

import os
import re
import time
import uuid
from pathlib import Path
from typing import List, Tuple

INBOX = Path(".dreamer") / "inbox"
NOTE_CLIP = 2000
MAX_NOTES = 200  # beyond this, oldest notes are dropped rather than growing forever


def inbox(root: Path) -> Path:
    return root / INBOX


def add(root: Path, text: str, source: str = "") -> Path:
    text = text.strip()
    if not text:
        raise ValueError("empty note")
    d = inbox(root)
    d.mkdir(parents=True, exist_ok=True)
    source = re.sub(r"[^A-Za-z0-9_.-]", "", source)[:32] or "agent"
    name = f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}.md"
    tmp = d / f".{name}.tmp"
    tmp.write_text(f"<!-- source: {source} -->\n{text[:NOTE_CLIP]}\n", encoding="utf-8")
    os.replace(tmp, d / name)
    files = pending(root)
    for old in files[:-MAX_NOTES]:
        old.unlink(missing_ok=True)
    return d / name


def pending(root: Path) -> List[Path]:
    d = inbox(root)
    return sorted(p for p in d.glob("*.md") if p.is_file() and not p.is_symlink()) if d.is_dir() else []


def render(root: Path, budget: int) -> Tuple[str, List[str]]:
    """Notes as digest text (oldest first, within `budget`) and the files used."""
    lines, used, files = [], 0, []
    for p in pending(root):
        raw = p.read_text(encoding="utf-8", errors="replace")
        m = re.match(r"<!-- source: ([^>]*) -->\n", raw)
        source = m.group(1).strip() if m else "agent"
        body = raw[m.end():] if m else raw
        stamp = p.name[:8]
        line = f"- ({stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}, {source}) " + body.strip().replace("\n", "\n  ")
        if used + len(line) > budget and lines:
            break
        lines.append(line)
        used += len(line)
        files.append(str(p))
    if not lines:
        return "", []
    return "### Notes recorded by agents during sessions\n" + "\n".join(lines), files


def consume(files: List[str]) -> None:
    for f in files:
        Path(f).unlink(missing_ok=True)
