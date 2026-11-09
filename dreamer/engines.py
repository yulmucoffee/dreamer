"""Who plays the model in an automatic pass.

- api:          the built-in loop on the Claude API (needs Anthropic credentials)
- claude-code:  `claude -p` with your Claude subscription
- codex:        `codex exec` with your ChatGPT/OpenAI login
- cursor:       `cursor-agent -p` with your Cursor plan

The CLI engines drive the same agent protocol as an interactive "dream"
(`dreamer dream start ... finish`), so budgets, validation, snapshots and the
cursor work identically whichever model runs. `auto` picks api when Anthropic
credentials are set, otherwise the first agent CLI found on PATH.

Recursion guards: the child runs with DREAMER_DREAMING=1 (every dreamer hook
exits early on it), and its prompt carries transcripts.MARKER so the pass's
own session is never fed into a later pass.
"""

import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from dreamer import dream, repo
from dreamer.config import DreamerConfig
from dreamer.llm import DreamError
from dreamer.transcripts import MARKER

PROMPT = (
    MARKER + " Consolidate this repository's long-term memory. From the repository root run "
    "`dreamer dream {personal}start{force}` and follow the instructions it prints exactly, using only `dreamer dream ...` "
    "commands, until `dreamer dream finish` reports applied or unchanged. Do not edit any other file. "
    "If `dreamer dream start` reports skipped, stop."
)

CLI: Dict[str, Callable[[str, str], List[str]]] = {
    "claude-code": lambda prompt, model: ["claude", "-p", prompt, "--allowedTools", "Bash(dreamer dream:*)"]
    + (["--model", model] if model else []),
    "codex": lambda prompt, model: ["codex", "exec", "--full-auto", prompt] + (["-m", model] if model else []),
    "cursor": lambda prompt, model: ["cursor-agent", "-p", "--force", prompt] + (["--model", model] if model else []),
}
BINARIES = {"claude-code": "claude", "codex": "codex", "cursor": "cursor-agent"}


def resolve(cfg: DreamerConfig, requested: str = "") -> Optional[str]:
    name = requested or cfg.dream.engine
    if name != "auto":
        return name
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "api"
    for engine in ("claude-code", "codex", "cursor"):
        if shutil.which(BINARIES[engine]):
            return engine
    return None


def command(cfg: DreamerConfig, engine: str, force: bool, personal_pass: bool = False) -> List[str]:
    prompt = PROMPT.format(force=" --force" if force else "", personal="--personal " if personal_pass else "")
    if cfg.dream.engine_command:  # custom harness: "mycli --headless {prompt}"
        return [a.replace("{prompt}", prompt) for a in shlex.split(cfg.dream.engine_command)]
    if engine not in CLI:
        raise DreamError(f"unknown engine {engine!r}; choose api, claude-code, codex, cursor or auto")
    return CLI[engine](prompt, cfg.dream.engine_model)


def run(root: Path, cfg: DreamerConfig, *, engine: str = "", auto: bool = False, force: bool = False,
        dry_run: bool = False) -> dream.Result:
    if os.environ.get("DREAMER_DREAMING"):
        return dream.Result("skipped", "inside a dreaming pass")
    name = resolve(cfg, engine)
    if name is None:
        return dream.Result("skipped", "no engine available: set Anthropic credentials, install claude, codex or "
                                       "cursor-agent, or ask your agent to \"dream\"")
    if name == "none":
        return dream.Result("skipped", "automatic dreaming is off (engine = none); ask your agent to \"dream\"")
    if name == "api":
        return dream.run(root, cfg, auto=auto, force=force, dry_run=dry_run)
    if dry_run:
        raise DreamError("--dry-run is only available with the api engine; use `dreamer dream start` to preview")

    sdir = repo.state_dir(root)
    # Cheap gate first, so a CLI is never launched for nothing.
    with repo.lock(sdir):
        state = repo.load_state(sdir)
        if auto and state.get("failures", 0) >= cfg.dream.max_failures:
            return dream.Result("skipped", f"paused after {state['failures']} failed passes; run `dreamer run` manually")
        gated = dream._gate(root, cfg, sdir, state, dream._gather(root, cfg, state), force, False)
        if gated:
            return gated
    from dreamer import personal
    cmd = command(cfg, name, force, personal.is_root(root))
    with repo.lock(sdir, "engine.lock"):  # one engine at a time; a second hook just skips
        return _launch(root, cfg, sdir, name, cmd)


def _launch(root: Path, cfg: DreamerConfig, sdir: Path, name: str, cmd: List[str]) -> dream.Result:
    since = dream._now()
    env = {**os.environ, "DREAMER_DREAMING": "1"}
    log = sdir / f"engine-{name}.log"
    started = time.time()
    try:
        with open(log, "w", encoding="utf-8") as out:
            proc = subprocess.run(cmd, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                  timeout=cfg.dream.engine_timeout)
        code: Optional[int] = proc.returncode
    except FileNotFoundError:
        err = DreamError(f"{cmd[0]} not found on PATH")
        dream._fail(sdir, repo.load_state(sdir), err, {}, name)
        raise err
    except subprocess.TimeoutExpired:
        code = None

    state = repo.load_state(sdir)
    new_runs = [r for r in state.get("runs", []) if r.get("at", "") >= since]
    if dream._external_active(sdir):  # the agent left the pass open
        dream.external_abort(root)
        err = DreamError(f"{name} did not finish the pass" + (" (timed out)" if code is None else f" (exit {code})")
                         + f"; see {log}")
        dream._fail(sdir, state, err, {}, name)
        raise err
    done = [r for r in new_runs if r.get("status") in ("applied", "unchanged")]
    if not done:
        if new_runs and new_runs[-1].get("status") == "failed":  # already counted by the pass itself
            raise DreamError(f"{name} pass failed: {new_runs[-1].get('error')}; see {log}")
        err = DreamError(f"{name} exited without running a pass (exit {code}); see {log}")
        dream._fail(sdir, state, err, {}, name)
        raise err
    last = done[-1]
    last["mode"] = name
    last["seconds"] = round(time.time() - started, 1)
    repo.save_state(sdir, state)
    return dream.Result(last["status"], last.get("summary", ""), last.get("changed", []), {})
