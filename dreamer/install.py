"""`dreamer init` / `dreamer uninstall`. Every file touched is inside the repo."""

import json
import re
import subprocess
from pathlib import Path
from typing import List, Optional

from dreamer import notes, repo, store, targets
from dreamer.config import EXAMPLE_TOML, DreamerConfig

HOOK_TAG = "dreamer run --auto"
GIT_HOOK_MARKER = "# dreamer hook: background memory dreaming"

# Detached so it survives Claude Code's short SessionEnd budget; waits so the
# transcript (written asynchronously) is flushed. Does nothing if dreamer is
# not installed, so teammates without dreamer are unaffected.
SESSION_END_COMMAND = (
    'command -v dreamer >/dev/null 2>&1 || exit 0; '
    f'(cd "$CLAUDE_PROJECT_DIR" && nohup {HOOK_TAG} --quiet --wait 20 </dev/null >/dev/null 2>&1 &); exit 0'
)

GIT_HOOK = f"""#!/usr/bin/env bash
{GIT_HOOK_MARKER}
if command -v dreamer >/dev/null 2>&1; then
    (nohup {HOOK_TAG} --quiet </dev/null >/dev/null 2>&1 &)
fi
"""


def default_targets(root: Path) -> List[str]:
    """Claude Code and the AGENTS.md family always; Gemini CLI when it is in use."""
    out = ["claude", "agents"]
    if (root / "GEMINI.md").exists():
        out.append("gemini")
    return out


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


def _git_exclude(root: Path, rel: str) -> None:
    """Keep personal files out of commits via .git/info/exclude (not committed)."""
    if not repo.is_git(root):
        return
    if subprocess.run(["git", "check-ignore", "-q", rel], cwd=root).returncode == 0:
        return
    exclude = repo.git_path(root, "info/exclude")
    if exclude is None:
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if rel not in text.splitlines():
        exclude.write_text(text + ("" if text.endswith("\n") or not text else "\n") + rel + "\n", encoding="utf-8")


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
         session_hook: bool = True, with_config: bool = False) -> List[str]:
    out: List[str] = []
    mem = root / cfg.memory.dir
    mem.mkdir(parents=True, exist_ok=True)
    _migrate_legacy(root, cfg, out)
    idx = mem / store.INDEX
    if not idx.exists():
        idx.write_text("# Project memory\n", encoding="utf-8")
        out.append(f"created {cfg.memory.dir}/{store.INDEX}")

    for kind in harnesses or default_targets(root):
        name = targets.FILES[kind]
        changed = targets.upsert(root / name, targets.block(kind, root, cfg))
        out.append(f"{name} block {'written' if changed else 'unchanged'}")

    if session_hook:
        out.append(f"Claude Code SessionEnd hook in .claude/settings.local.json {_install_session_hook(root)}")
        _git_exclude(root, ".claude/settings.local.json")
    _git_exclude(root, str(notes.INBOX.parent) + "/")

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
        out.append("removed SessionEnd hook")
    hook = repo.git_path(root, "hooks/post-commit")
    if hook and hook.exists() and GIT_HOOK_MARKER in hook.read_text(encoding="utf-8", errors="replace"):
        hook.unlink()
        out.append("removed git post-commit hook")
    out.append("memory files, notes and state were kept")
    return out
