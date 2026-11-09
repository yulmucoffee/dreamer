"""`dreamer capture`: record sessions from harnesses whose transcripts dreamer
cannot read, using their hooks.

Cursor: project hooks (.cursor/hooks.json) pipe documented event payloads
(beforeSubmitPrompt.prompt, afterAgentResponse.text, afterShellExecution
command/output, afterFileEdit.file_path) into `dreamer capture cursor`, which
appends them to .dreamer/sessions/cursor-<conversation>.jsonl. sessionEnd
starts a background dreaming pass.

Any other harness with hooks can use `dreamer capture generic`, sending one
JSON object per event: {"session": "...", "kind": "user|assistant|tool|result",
"text": "...", "name": "...", "input": ..., "output": "...", "error": false}.

Capture never blocks the harness: it always exits 0 and, for prompt hooks,
answers {"continue": true}.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from dreamer import repo

SESSIONS = Path(repo.LOCAL_DIR) / "sessions"
TEXT_CLIP = 8000
OUTPUT_CLIP = 4000
RETENTION_DAYS = 30


def sessions_dir(root: Path) -> Path:
    return root / SESSIONS


def _norm_cursor(ev: Dict[str, Any]) -> List[Dict[str, Any]]:
    name = ev.get("hook_event_name")
    if name == "beforeSubmitPrompt":
        return [{"kind": "user", "text": str(ev.get("prompt", ""))}]
    if name == "afterAgentResponse":
        return [{"kind": "assistant", "text": str(ev.get("text", ""))}]
    if name == "afterShellExecution":
        return [{"kind": "tool", "name": "Shell", "input": {"command": ev.get("command", "")}},
                {"kind": "result", "output": str(ev.get("output", ""))}]
    if name == "afterFileEdit":
        return [{"kind": "tool", "name": "Edit", "input": {"file_path": ev.get("file_path", "")}}]
    return []


def _session_file(root: Path, source: str, session: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", session)[:80] or "unknown"
    return sessions_dir(root) / f"{source}-{safe}.jsonl"


def record(root: Path, source: str, session: str, events: List[Dict[str, Any]], cwd: Path) -> Optional[Path]:
    if not events:
        return None
    path = _session_file(root, source, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(path, "a", encoding="utf-8") as f:
        for e in events:
            e = dict(e)
            for k, n in (("text", TEXT_CLIP), ("output", OUTPUT_CLIP)):
                if isinstance(e.get(k), str) and len(e[k]) > n:
                    e[k] = e[k][:n] + f" …[+{len(e[k]) - n} chars]"
            f.write(json.dumps({"ts": now, "source": source, "session": session, "cwd": str(cwd), **e},
                               ensure_ascii=False) + "\n")
    return path


def _trigger_dream(root: Path) -> None:
    if os.environ.get("DREAMER_DREAMING"):
        return
    subprocess.Popen(
        [sys.executable, "-m", "dreamer.cli", "run", "--auto", "--quiet", "--wait", "5", "--repo-dir", str(root)],
        cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def handle(source: str, raw: str, cwd: Path) -> str:
    """Process one hook event. Returns what to print (hook protocol answer)."""
    answer = json.dumps({"continue": True})
    try:
        ev = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return answer
    if not isinstance(ev, dict):
        return answer
    roots = ev.get("workspace_roots") or []
    where = Path(roots[0]) if roots and isinstance(roots[0], str) else cwd
    root = repo.find_root(where)
    # Only record into a repository that opted in with `dreamer init`.
    from dreamer.config import DreamerConfig
    if not (root / DreamerConfig.load(root).memory.dir / "MEMORY.md").is_file():
        return answer
    if source == "cursor":
        session = str(ev.get("conversation_id") or "unknown")
        events = _norm_cursor(ev)
        if ev.get("hook_event_name") == "sessionEnd":
            _trigger_dream(root)
    else:
        session = str(ev.get("session") or "unknown")
        events = [{k: v for k, v in ev.items() if k in ("kind", "text", "name", "input", "output", "error")}]
        if ev.get("kind") == "end":
            events = []
            _trigger_dream(root)
    if not os.environ.get("DREAMER_DREAMING"):  # never record a dreaming pass itself
        record(root, source, session, events, where)
    return answer


def prune(root: Path, cursors: Dict[str, Any]) -> None:
    """Drop captured sessions that are fully dreamed and older than the
    retention window, mirroring Claude Code's own transcript cleanup."""
    d = sessions_dir(root)
    if not d.is_dir():
        return
    cutoff = time.time() - RETENTION_DAYS * 86400
    for p in d.glob("*.jsonl"):
        entry = cursors.get(str(p), {})
        if p.stat().st_mtime < cutoff and int(entry.get("offset", -1)) == p.stat().st_size:
            p.unlink(missing_ok=True)
