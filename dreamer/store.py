"""The memory store: a directory of Markdown files with hard budgets.

`MemoryTool` implements the Anthropic memory tool (memory_20250818) over a
staging copy of the store. Every write is checked against the budgets, so the
store cannot outgrow them no matter how many passes run; the model has to
merge, condense or delete to make room.
"""

import hashlib
import os
import re
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dreamer.config import MemoryConfig

INDEX = "MEMORY.md"
VIRTUAL_ROOT = "/memories"
MAX_DEPTH = 2
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


class ToolError(Exception):
    pass


def iter_files(root: Path) -> List[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*.md") if p.is_file() and not p.is_symlink())


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in iter_files(root):
        h.update(str(p.relative_to(root)).encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def usage(root: Path) -> Tuple[int, int]:
    files = iter_files(root)
    return len(files), sum(p.stat().st_size for p in files)


def listing(root: Path) -> str:
    rows = []
    for p in iter_files(root):
        rel = p.relative_to(root).as_posix()
        first = next((l.strip() for l in p.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()), "")
        rows.append(f"- {VIRTUAL_ROOT}/{rel} ({p.stat().st_size} B) {first[:80]}")
    return "\n".join(rows) or "(empty)"


def validate(root: Path, cfg: MemoryConfig) -> List[str]:
    """Structural problems: missing index, broken links, unreachable pages, budgets."""
    issues: List[str] = []
    index = root / INDEX
    if not index.is_file():
        return [f"{INDEX} is missing"]
    files = iter_files(root)
    linked = {index.resolve()}
    for p in files:
        for target in _LINK.findall(p.read_text(encoding="utf-8", errors="replace")):
            if re.match(r"^[a-z]+:", target) or target.startswith("#"):
                continue
            t = target.split("#", 1)[0]
            if not t.endswith(".md"):
                continue
            if t.startswith(VIRTUAL_ROOT + "/"):
                dest = root / t[len(VIRTUAL_ROOT) + 1 :]
            elif t.startswith("/"):
                dest = root / t.lstrip("/")
            else:
                dest = p.parent / t
            dest = dest.resolve()
            if dest.is_file():
                linked.add(dest)
            else:
                issues.append(f"broken link in {p.relative_to(root).as_posix()}: {target}")
    for p in files:
        if p.resolve() not in linked:
            issues.append(f"unreachable page (nothing links to it): {p.relative_to(root).as_posix()}")
    n, total = usage(root)
    text = index.read_text(encoding="utf-8")
    if len(text.splitlines()) > cfg.index_max_lines or len(text.encode()) > cfg.index_max_bytes:
        issues.append(f"{INDEX} over budget")
    if total > cfg.store_max_bytes or n > cfg.max_files:
        issues.append("store over budget")
    return issues


class MemoryTool:
    """Backend for the memory tool, confined to one directory."""

    def __init__(self, root: Path, cfg: MemoryConfig):
        self.root = root.resolve()
        self.cfg = cfg
        self.changed = False

    # -- path safety -----------------------------------------------------
    def _resolve(self, vpath: str) -> Path:
        if not isinstance(vpath, str) or not (vpath == VIRTUAL_ROOT or vpath.startswith(VIRTUAL_ROOT + "/")):
            raise ToolError(f"path must be under {VIRTUAL_ROOT}")
        rel = vpath[len(VIRTUAL_ROOT) :].strip("/")
        parts = [p for p in rel.split("/") if p]
        if any(p in (".", "..") or p.startswith(".") for p in parts):
            raise ToolError("invalid path component")
        target = self.root.joinpath(*parts) if parts else self.root
        # Refuse anything that resolves outside the store, e.g. via a symlink.
        cur = self.root
        for p in parts:
            cur = cur / p
            if cur.is_symlink():
                raise ToolError("symlinks are not allowed")
        if target.resolve() != self.root and self.root not in target.resolve().parents:
            raise ToolError("path escapes the memory directory")
        return target

    def _rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()

    def _check_file(self, p: Path, content: str, adding_file: bool) -> None:
        if p.suffix != ".md":
            raise ToolError("only .md files can be stored")
        if len(p.relative_to(self.root).parts) > MAX_DEPTH:
            raise ToolError(f"at most {MAX_DEPTH - 1} directory level is allowed")
        size = len(content.encode("utf-8"))
        if p == self.root / INDEX:
            lines = len(content.splitlines())
            if lines > self.cfg.index_max_lines or size > self.cfg.index_max_bytes:
                raise ToolError(
                    f"{INDEX} would be {lines} lines / {size} B; budget is {self.cfg.index_max_lines} lines / "
                    f"{self.cfg.index_max_bytes} B. Move detail into topic files or drop low-value lines."
                )
        elif size > self.cfg.topic_max_bytes:
            raise ToolError(f"{self._rel(p)} would be {size} B; per-file budget is {self.cfg.topic_max_bytes} B. Split or condense.")
        n, total = usage(self.root)
        old = p.stat().st_size if p.exists() else 0
        if adding_file and n + 1 > self.cfg.max_files:
            raise ToolError(f"store already has {n} files (max {self.cfg.max_files}); merge or delete pages first")
        new_total = total - old + size
        if new_total > self.cfg.store_max_bytes:
            raise ToolError(f"store would be {new_total} B (max {self.cfg.store_max_bytes} B); merge, condense or delete first")

    def _write(self, p: Path, content: str) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, p)
        self.changed = True

    def _read(self, p: Path) -> str:
        if not p.is_file():
            raise ToolError(f"{VIRTUAL_ROOT}/{self._rel(p)} does not exist")
        return p.read_text(encoding="utf-8")

    # -- commands --------------------------------------------------------
    def execute(self, inp: Dict) -> str:
        cmd = inp.get("command")
        fn = getattr(self, f"cmd_{cmd}", None)
        if fn is None:
            raise ToolError(f"unknown command {cmd!r}")
        return fn(inp)

    def cmd_view(self, inp: Dict) -> str:
        p = self._resolve(inp.get("path", VIRTUAL_ROOT))
        if p.is_dir():
            n, total = usage(self.root)
            return (
                f"Store usage: {n}/{self.cfg.max_files} files, {total}/{self.cfg.store_max_bytes} B\n"
                + listing(self.root)
            )
        lines = self._read(p).splitlines()
        start, end = 1, len(lines)
        rng = inp.get("view_range")
        if isinstance(rng, list) and len(rng) == 2:
            start = max(1, int(rng[0]))
            end = len(lines) if int(rng[1]) == -1 else min(len(lines), int(rng[1]))
        body = "\n".join(f"{i:6d}\t{lines[i - 1]}" for i in range(start, end + 1))
        return f"Here's the content of {inp.get('path')} with line numbers:\n{body}"

    def cmd_create(self, inp: Dict) -> str:
        p = self._resolve(inp.get("path", ""))
        if p == self.root or p.is_dir():
            raise ToolError("path is a directory")
        text = inp.get("file_text", "")
        self._check_file(p, text, adding_file=not p.exists())
        self._write(p, text)
        return f"File created successfully at: {inp.get('path')}"

    def cmd_str_replace(self, inp: Dict) -> str:
        p = self._resolve(inp.get("path", ""))
        text = self._read(p)
        old, new = inp.get("old_str", ""), inp.get("new_str", "")
        count = text.count(old) if old else 0
        if count != 1:
            raise ToolError(f"old_str must appear exactly once in {inp.get('path')} (found {count})")
        updated = text.replace(old, new, 1)
        self._check_file(p, updated, adding_file=False)
        self._write(p, updated)
        return f"The memory file {inp.get('path')} has been edited."

    def cmd_insert(self, inp: Dict) -> str:
        p = self._resolve(inp.get("path", ""))
        lines = self._read(p).splitlines()
        at = int(inp.get("insert_line", 0))
        if not 0 <= at <= len(lines):
            raise ToolError(f"insert_line must be between 0 and {len(lines)}")
        lines[at:at] = str(inp.get("insert_text", "")).splitlines()
        updated = "\n".join(lines) + "\n"
        self._check_file(p, updated, adding_file=False)
        self._write(p, updated)
        return f"Text inserted at line {at} of {inp.get('path')}."

    def cmd_delete(self, inp: Dict) -> str:
        p = self._resolve(inp.get("path", ""))
        if p == self.root or p == self.root / INDEX:
            raise ToolError(f"{INDEX} and the memory root cannot be deleted")
        if p.is_dir():
            shutil.rmtree(p)
        elif p.is_file():
            p.unlink()
        else:
            raise ToolError(f"{inp.get('path')} does not exist")
        self.changed = True
        return f"Deleted {inp.get('path')}"

    def cmd_rename(self, inp: Dict) -> str:
        src, dst = self._resolve(inp.get("old_path", "")), self._resolve(inp.get("new_path", ""))
        if self.root / INDEX in (src, dst) or self.root in (src, dst):
            raise ToolError(f"{INDEX} cannot be renamed or replaced")
        if not src.exists():
            raise ToolError(f"{inp.get('old_path')} does not exist")
        if dst.exists():
            raise ToolError(f"{inp.get('new_path')} already exists")
        if src.is_file():
            self._check_file(dst, src.read_text(encoding="utf-8"), adding_file=False)
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
        self.changed = True
        return f"Renamed {inp.get('old_path')} to {inp.get('new_path')}"


# -- staging, apply, snapshots -------------------------------------------------

def copy_store(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for p in iter_files(src):
        target = dst / p.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)


def apply_store(staged: Path, live: Path) -> List[str]:
    """Make `live` match `staged`, touching only files that changed. Only
    Markdown files are managed; anything else in the directory is left alone."""
    live.mkdir(parents=True, exist_ok=True)
    changed = []
    staged_rel = {p.relative_to(staged) for p in iter_files(staged)}
    for rel in sorted(staged_rel):
        s, l = staged / rel, live / rel
        data = s.read_bytes()
        if not l.exists() or l.read_bytes() != data:
            l.parent.mkdir(parents=True, exist_ok=True)
            tmp = l.with_name(f".{l.name}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, l)
            changed.append(rel.as_posix())
    for p in iter_files(live):
        rel = p.relative_to(live)
        if rel not in staged_rel:
            p.unlink()
            changed.append(f"-{rel.as_posix()}")
    for d in sorted((d for d in live.rglob("*") if d.is_dir()), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()
    return changed


def snapshot(live: Path, snaps: Path, state_json: Optional[str], keep: int) -> Path:
    dest = snaps / time.strftime("%Y%m%dT%H%M%S")
    i = 1
    while dest.exists():
        dest = snaps / f"{time.strftime('%Y%m%dT%H%M%S')}-{i}"
        i += 1
    copy_store(live, dest / "memory")
    if state_json is not None:
        (dest / "state.json").write_text(state_json, encoding="utf-8")
    for old in sorted(p for p in snaps.iterdir() if p.is_dir())[:-keep]:
        shutil.rmtree(old, ignore_errors=True)
    return dest
