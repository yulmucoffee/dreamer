"""`dreamer init` / `dreamer uninstall`. Every file touched is inside the repo."""

import json
import re
import subprocess
from pathlib import Path
from typing import List

from dreamer import repo, store
from dreamer.config import EXAMPLE_TOML, DreamerConfig

BLOCK_BEGIN = "<!-- dreamer:begin (managed by dreamer; edits inside this block are overwritten) -->"
BLOCK_END = "<!-- dreamer:end -->"
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


def claude_block(cfg: DreamerConfig) -> str:
    d = cfg.memory.dir.rstrip("/")
    return f"""{BLOCK_BEGIN}
## Project memory
@{d}/{store.INDEX}

The index above is this project's long-term memory, maintained between sessions from past session transcripts. Detail lives in topic files under `{d}/` (links are relative to the linking file); open one when its topic matters. Notes are predictions, not guarantees: if one turns out wrong, correct it in place.
{BLOCK_END}"""


def _upsert_block(path: Path, block: str) -> str:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    pattern = re.compile(re.escape(BLOCK_BEGIN) + r".*?" + re.escape(BLOCK_END), re.DOTALL)
    if pattern.search(text):
        new = pattern.sub(lambda _: block, text, count=1)
    else:
        new = (text.rstrip() + "\n\n" if text.strip() else "") + block + "\n"
    if new != text:
        repo.atomic_write(path, new)
        return "updated"
    return "unchanged"


def _remove_block(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(r"\n*" + re.escape(BLOCK_BEGIN) + r".*?" + re.escape(BLOCK_END) + r"\n?", re.DOTALL)
    new = pattern.sub("\n", text).strip()
    if new == text.strip():
        return False
    if new:
        repo.atomic_write(path, new + "\n")
    else:
        path.unlink()
    return True


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


def init(root: Path, cfg: DreamerConfig, *, git_hook: bool = False, cursor: bool = False, with_config: bool = False) -> List[str]:
    out: List[str] = []
    mem = root / cfg.memory.dir
    mem.mkdir(parents=True, exist_ok=True)
    _migrate_legacy(root, cfg, out)
    idx = mem / store.INDEX
    if not idx.exists():
        idx.write_text("# Project memory\n", encoding="utf-8")
        out.append(f"created {cfg.memory.dir}/{store.INDEX}")

    out.append(f"CLAUDE.md block {_upsert_block(root / 'CLAUDE.md', claude_block(cfg))}")
    out.append(f"SessionEnd hook in .claude/settings.local.json {_install_session_hook(root)}")
    _git_exclude(root, ".claude/settings.local.json")

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

    if cursor:
        rules = root / ".cursor" / "rules" / "dreamer.mdc"
        if not rules.exists():
            d = cfg.memory.dir.rstrip("/")
            repo.atomic_write(rules, f"---\ndescription: Project memory maintained by dreamer\nalwaysApply: true\n---\n"
                                     f"Read `{d}/{store.INDEX}` before making assumptions about this project; "
                                     f"topic files under `{d}/` hold detail.\n")
            out.append("created .cursor/rules/dreamer.mdc")

    if with_config and not (root / "dreamer.toml").exists():
        (root / "dreamer.toml").write_text(EXAMPLE_TOML, encoding="utf-8")
        out.append("created dreamer.toml")
    return out


def uninstall(root: Path) -> List[str]:
    out = []
    if _remove_block(root / "CLAUDE.md"):
        out.append("removed CLAUDE.md block")
    if _remove_session_hook(root):
        out.append("removed SessionEnd hook")
    hook = repo.git_path(root, "hooks/post-commit")
    if hook and hook.exists() and GIT_HOOK_MARKER in hook.read_text(encoding="utf-8", errors="replace"):
        hook.unlink()
        out.append("removed git post-commit hook")
    out.append("memory files and state were kept")
    return out
