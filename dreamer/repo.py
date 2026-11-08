"""Repository-scoped paths, state, locking and atomic file helpers.

Everything dreamer writes lives inside the repository: the memory directory,
a managed block in CLAUDE.md, .claude/settings.local.json, and private state
under the (per-worktree) git dir. Nothing is written to $HOME or global config.
"""

import contextlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

STATE_VERSION = 1
LOG_MAX_BYTES = 1024 * 1024


def _git(root: Path, *args: str) -> Optional[str]:
    try:
        res = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    except OSError:
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def find_root(start: Path) -> Path:
    start = Path(start).resolve()
    top = _git(start, "rev-parse", "--show-toplevel")
    return Path(top).resolve() if top else start


def is_git(root: Path) -> bool:
    return _git(root, "rev-parse", "--git-dir") is not None


def state_dir(root: Path) -> Path:
    """Private, never-committed state. Per worktree, because each worktree has
    its own checkout of the memory directory."""
    git_dir = _git(root, "rev-parse", "--absolute-git-dir")
    d = Path(git_dir) / "dreamer" if git_dir else root / ".dreamer"
    d.mkdir(parents=True, exist_ok=True)
    return d


def git_path(root: Path, rel: str) -> Optional[Path]:
    p = _git(root, "rev-parse", "--git-path", rel)
    return (root / p).resolve() if p else None


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def load_state(sdir: Path) -> Dict[str, Any]:
    p = sdir / "state.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("version") == STATE_VERSION:
            return data
    except (OSError, ValueError):
        pass
    return {"version": STATE_VERSION, "files": {}, "failures": 0, "runs": []}


def save_state(sdir: Path, state: Dict[str, Any]) -> None:
    state["runs"] = state.get("runs", [])[-50:]
    atomic_write(sdir / "state.json", json.dumps(state, indent=1, sort_keys=True))


class Busy(Exception):
    pass


@contextlib.contextmanager
def lock(sdir: Path):
    """Non-blocking exclusive lock so overlapping hooks never run two passes."""
    import fcntl

    f = open(sdir / "lock", "w")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy()
        yield
    finally:
        f.close()


def log(sdir: Path, msg: str) -> None:
    p = sdir / "dreamer.log"
    try:
        if p.exists() and p.stat().st_size > LOG_MAX_BYTES:
            tail = p.read_bytes()[-LOG_MAX_BYTES // 2 :]
            p.write_bytes(tail)
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except OSError:
        pass
