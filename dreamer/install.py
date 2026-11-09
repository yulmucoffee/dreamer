"""`dreamer init` / `dreamer uninstall`. Every file touched is inside the repo."""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

from dreamer import notes, repo, store, targets
from dreamer.config import EXAMPLE_TOML, DreamerConfig

HOOK_TAG = "dreamer run --auto"
GIT_HOOK_MARKER = "# dreamer hook: background memory dreaming"
GUARD = '[ -n "$DREAMER_DREAMING" ] && exit 0; command -v dreamer >/dev/null 2>&1 || exit 0; '

# Detached so it outlives the harness's short hook budget; waits so the
# transcript (written asynchronously) is flushed. Every hook does nothing if
# dreamer is not installed (teammates) or inside a dreaming pass (recursion).
SESSION_END_COMMAND = (
    GUARD + f'(cd "$CLAUDE_PROJECT_DIR" && nohup {HOOK_TAG} --quiet --wait 20 </dev/null >/dev/null 2>&1 &); exit 0'
)
CODEX_STOP_COMMAND = GUARD + f'(nohup {HOOK_TAG} --quiet --wait 10 </dev/null >/dev/null 2>&1 &); exit 0'

GIT_HOOK = f"""#!/usr/bin/env bash
{GIT_HOOK_MARKER}
[ -n "$DREAMER_DREAMING" ] && exit 0
if command -v dreamer >/dev/null 2>&1; then
    (nohup {HOOK_TAG} --quiet </dev/null >/dev/null 2>&1 &)
fi
"""

CURSOR_SCRIPT = Path(".cursor") / "hooks" / "dreamer.sh"
CURSOR_SCRIPT_TEXT = """#!/bin/sh
# dreamer: record this Cursor session for memory consolidation. Never blocks Cursor.
if [ -z "$DREAMER_DREAMING" ] && command -v dreamer >/dev/null 2>&1; then
  exec dreamer capture cursor
fi
cat >/dev/null
echo '{"continue": true}'
"""
CURSOR_EVENTS = ("beforeSubmitPrompt", "afterAgentResponse", "afterShellExecution", "afterFileEdit", "sessionEnd")

# Which instruction file each harness reads.
HARNESS_TARGET = {"claude": "claude", "codex": "agents", "cursor": "agents", "gemini": "gemini", "agents": "agents"}
HARNESSES = tuple(HARNESS_TARGET)


def detect_harnesses(root: Path) -> List[str]:
    """Harnesses this person uses, judged by installed CLIs and config dirs (read only)."""
    home = Path.home()
    found = []
    probes = {"claude": ("claude", ".claude"), "codex": ("codex", ".codex"),
              "cursor": ("cursor-agent", ".cursor"), "gemini": ("gemini", ".gemini")}
    for name, (binary, conf) in probes.items():
        if shutil.which(binary) or (home / conf).is_dir() or (name == "gemini" and (root / "GEMINI.md").exists()):
            found.append(name)
    return found or ["claude", "agents"]


def _load_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def _write_json(root: Path, path: Path, data: dict, existed: bool) -> None:
    repo.atomic_write(path, json.dumps(data, indent=2) + "\n")
    if not existed:  # a file we created is personal; one you already track stays tracked
        repo.exclude(root, str(path.relative_to(root)))


def _install_codex_hook(root: Path) -> str:
    path = root / ".codex" / "hooks.json"
    existed = path.exists()
    data = _load_json(path)
    if data is None:
        return f"skipped: {path} is not valid JSON"
    groups = data.setdefault("hooks", {}).setdefault("Stop", [])
    for g in groups:
        for h in g.get("hooks", []):
            if HOOK_TAG in h.get("command", ""):
                if h["command"] == CODEX_STOP_COMMAND:
                    return "unchanged"
                h["command"] = CODEX_STOP_COMMAND
                _write_json(root, path, data, existed)
                return "updated"
    groups.append({"hooks": [{"type": "command", "command": CODEX_STOP_COMMAND, "async": True, "timeout": 30}]})
    _write_json(root, path, data, existed)
    return "installed (Codex may ask you to trust this project's hooks)"


def _install_cursor_hooks(root: Path) -> str:
    path = root / ".cursor" / "hooks.json"
    existed = path.exists()
    data = _load_json(path)
    if data is None:
        return f"skipped: {path} is not valid JSON"
    data.setdefault("version", 1)
    hooks = data.setdefault("hooks", {})
    cmd = str(CURSOR_SCRIPT)
    added = 0
    for event in CURSOR_EVENTS:
        entries = hooks.setdefault(event, [])
        if not any(e.get("command") == cmd for e in entries):
            entries.append({"command": cmd})
            added += 1
    script = root / CURSOR_SCRIPT
    script_existed = script.exists()
    if not script_existed or script.read_text(encoding="utf-8") != CURSOR_SCRIPT_TEXT:
        repo.atomic_write(script, CURSOR_SCRIPT_TEXT)
        script.chmod(0o755)
        if not script_existed:
            repo.exclude(root, str(CURSOR_SCRIPT))
    if added:
        _write_json(root, path, data, existed)
        return "installed"
    return "unchanged"


def _remove_codex_hook(root: Path) -> bool:
    path = root / ".codex" / "hooks.json"
    data = _load_json(path)
    if not data or "Stop" not in data.get("hooks", {}):
        return False
    before = json.dumps(data)
    kept = []
    for g in data["hooks"]["Stop"]:
        hs = [h for h in g.get("hooks", []) if HOOK_TAG not in h.get("command", "")]
        if hs:
            kept.append({**g, "hooks": hs})
    if kept:
        data["hooks"]["Stop"] = kept
    else:
        del data["hooks"]["Stop"]
    if json.dumps(data) == before:
        return False
    if not data["hooks"] and set(data) <= {"hooks"}:
        path.unlink()
    else:
        repo.atomic_write(path, json.dumps(data, indent=2) + "\n")
    return True


def _remove_cursor_hooks(root: Path) -> bool:
    path = root / ".cursor" / "hooks.json"
    data = _load_json(path)
    changed = False
    if data and "hooks" in data:
        for event in list(data["hooks"]):
            entries = [e for e in data["hooks"][event] if e.get("command") != str(CURSOR_SCRIPT)]
            if len(entries) != len(data["hooks"][event]):
                changed = True
                if entries:
                    data["hooks"][event] = entries
                else:
                    del data["hooks"][event]
        if changed:
            if not data["hooks"] and set(data) <= {"hooks", "version"}:
                path.unlink()
            else:
                repo.atomic_write(path, json.dumps(data, indent=2) + "\n")
    script = root / CURSOR_SCRIPT
    if script.exists():
        script.unlink()
        changed = True
    return changed


def _settings_path(root: Path) -> Path:
    return root / ".claude" / "settings.local.json"


def _install_session_hook(root: Path) -> str:
    path = _settings_path(root)
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return f"skipped: {path} is not valid JSON; add a SessionEnd hook running `{HOOK_TAG}` manually"
    groups = data.setdefault("hooks", {}).setdefault("SessionEnd", [])
    for g in groups:
        for h in g.get("hooks", []):
            if HOOK_TAG in h.get("command", ""):
                if h["command"] == SESSION_END_COMMAND:
                    return "unchanged"
                h["command"] = SESSION_END_COMMAND
                repo.atomic_write(path, json.dumps(data, indent=2) + "\n")
                return "updated"
    groups.append({"hooks": [{"type": "command", "command": SESSION_END_COMMAND}]})
    repo.atomic_write(path, json.dumps(data, indent=2) + "\n")
    return "installed"


def _remove_session_hook(root: Path) -> bool:
    path = _settings_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    groups = data.get("hooks", {}).get("SessionEnd", [])
    kept = []
    removed = False
    for g in groups:
        hooks = [h for h in g.get("hooks", []) if HOOK_TAG not in h.get("command", "")]
        removed |= len(hooks) != len(g.get("hooks", []))
        if hooks:
            kept.append({**g, "hooks": hooks})
    if not removed:
        return False
    if kept:
        data["hooks"]["SessionEnd"] = kept
    else:
        del data["hooks"]["SessionEnd"]
        if not data["hooks"]:
            del data["hooks"]
    repo.atomic_write(path, json.dumps(data, indent=2) + "\n")
    return True


_git_exclude = repo.exclude


def _migrate_legacy(root: Path, cfg: DreamerConfig, out: List[str]) -> None:
    """Old layout: MEMORY.md at the repo root, topics in memory/."""
    old = root / store.INDEX
    new = root / cfg.memory.dir / store.INDEX
    if old.is_file() and not new.exists() and old != new:
        d = cfg.memory.dir.rstrip("/") + "/"
        text = old.read_text(encoding="utf-8").replace(f"]({d}", "](")
        repo.atomic_write(new, text)
        old.unlink()
        out.append(f"moved {store.INDEX} -> {cfg.memory.dir}/{store.INDEX}")


def init(root: Path, cfg: DreamerConfig, *, harnesses: Optional[List[str]] = None, git_hook: bool = False,
         hooks: bool = True, with_config: bool = False) -> List[str]:
    """Set up memory for the harnesses this person uses (default: detected).
    One harness, or several sharing one memory, are the same setup."""
    out: List[str] = []
    harnesses = harnesses or detect_harnesses(root)
    out.append(f"harnesses: {', '.join(harnesses)}")
    mem = root / cfg.memory.dir
    mem.mkdir(parents=True, exist_ok=True)
    _migrate_legacy(root, cfg, out)
    idx = mem / store.INDEX
    if not idx.exists():
        idx.write_text("# Project memory\n", encoding="utf-8")
        out.append(f"created {cfg.memory.dir}/{store.INDEX}")
    repo.exclude(root, repo.LOCAL_DIR + "/")

    for kind in dict.fromkeys(HARNESS_TARGET[h] for h in harnesses):
        name = targets.FILES[kind]
        changed = targets.upsert(root / name, targets.block(kind, root, cfg))
        out.append(f"{name} block {'written' if changed else 'unchanged'}")

    if hooks:
        if "claude" in harnesses:
            out.append(f"Claude Code SessionEnd hook (.claude/settings.local.json): {_install_session_hook(root)}")
            _git_exclude(root, ".claude/settings.local.json")
        if "codex" in harnesses:
            out.append(f"Codex Stop hook (.codex/hooks.json): {_install_codex_hook(root)}")
        if "cursor" in harnesses:
            out.append(f"Cursor capture hooks (.cursor/hooks.json): {_install_cursor_hooks(root)}")

    if git_hook:
        hook = repo.git_path(root, "hooks/post-commit")
        if hook is None:
            out.append("git hook skipped: not a git repository")
        elif hook.exists() and GIT_HOOK_MARKER not in hook.read_text(encoding="utf-8", errors="replace"):
            out.append(f"git hook skipped: {hook} exists; add `({HOOK_TAG} --quiet &)` to it yourself")
        else:
            hook.parent.mkdir(parents=True, exist_ok=True)
            hook.write_text(GIT_HOOK, encoding="utf-8")
            hook.chmod(0o755)
            out.append(f"installed git post-commit hook at {hook}")

    if with_config and not (root / "dreamer.toml").exists():
        (root / "dreamer.toml").write_text(EXAMPLE_TOML, encoding="utf-8")
        out.append("created dreamer.toml")
    return out


def uninstall(root: Path) -> List[str]:
    out = []
    for name in targets.FILES.values():
        if targets.remove(root / name):
            out.append(f"removed {name} block")
    if _remove_session_hook(root):
        out.append("removed Claude Code SessionEnd hook")
    if _remove_codex_hook(root):
        out.append("removed Codex Stop hook")
    if _remove_cursor_hooks(root):
        out.append("removed Cursor capture hooks")
    hook = repo.git_path(root, "hooks/post-commit")
    if hook and hook.exists() and GIT_HOOK_MARKER in hook.read_text(encoding="utf-8", errors="replace"):
        hook.unlink()
        out.append("removed git post-commit hook")
    out.append("memory files, notes and state were kept")
    return out
