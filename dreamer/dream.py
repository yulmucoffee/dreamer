"""One dreaming pass: digest new transcript material, let Claude edit a staged
copy of the store, validate, then apply atomically and advance the cursor.

Failure at any point leaves both the memory directory and the cursor as they
were, so the same material is retried on the next pass.
"""

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from dreamer import repo, store, transcripts
from dreamer.config import DreamerConfig
from dreamer.llm import DreamError, make_client, run_loop

SYSTEM_PROMPT = """You maintain the long-term memory of one software project. You run offline, between coding sessions.

Coding agents working in this repository start every session with MEMORY.md already in their context, and can open the other files in the memory directory when a topic is relevant. Your purpose is to make those future sessions waste less effort: less rediscovering how things work, fewer repeated mistakes, fewer questions the user already answered, and no acting on notes that are wrong.

A note is a prediction that it will matter again. What is cheap to rediscover from the code, git history or the screen is not worth its tokens. A wrong or stale note is worse than none: it is injected into every session. When the sessions contradict a note, fix or remove it; when a claim matters and the digest is ambiguous, check it with search_transcripts. Record where a note came from, e.g. (2026-11-08, s:ab12cd34), so later passes can re-check it.

MEMORY.md should stay a short, high-signal index: the facts and rules needed most often, plus one-line links to topic files (links relative to the linking file, e.g. [build](build.md)). A page nothing links to will never be found. The store has hard budgets; writes over budget are rejected, so make room by merging, condensing or deleting.

Make the smallest set of edits that serves the purpose. If nothing in these sessions is worth remembering, change nothing. End with one line summarising what you changed and why."""


@dataclass
class Result:
    status: str  # applied | unchanged | skipped | dry-run
    message: str = ""
    changed: List[str] = field(default_factory=list)
    usage: Dict[str, int] = field(default_factory=dict)


def memory_dir(root: Path, cfg: DreamerConfig) -> Path:
    return root / cfg.memory.dir


def _user_prompt(cfg: DreamerConfig, staging: Path, batch: transcripts.Batch, issues: List[str]) -> str:
    n, total = store.usage(staging)
    m = cfg.memory
    index = (staging / store.INDEX).read_text(encoding="utf-8")
    parts = [
        f"Today: {time.strftime('%Y-%m-%d')}",
        f"Budgets: MEMORY.md <= {m.index_max_lines} lines and {m.index_max_bytes} B; other files <= {m.topic_max_bytes} B each; "
        f"store <= {m.store_max_bytes} B and {m.max_files} files. Current: {n} files, {total} B.",
        f"## Store contents\n{store.listing(staging)}",
        f"## {store.VIRTUAL_ROOT}/{store.INDEX}\n```markdown\n{index}\n```",
    ]
    if issues:
        parts.append("## Known problems in the store\n" + "\n".join(f"- {i}" for i in issues))
    more = " More sessions remain for later passes." if batch.pending else ""
    parts.append(
        f"## New session activity (digest; tool output clipped, thinking omitted).{more}\n{batch.text}"
    )
    return "\n\n".join(parts)


def _ensure_index(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    idx = d / store.INDEX
    if not idx.exists():
        idx.write_text("# Project memory\n", encoding="utf-8")


def run(
    root: Path,
    cfg: DreamerConfig,
    *,
    auto: bool = False,
    force: bool = False,
    dry_run: bool = False,
    client_factory: Callable[[], Any] = make_client,
) -> Result:
    sdir = repo.state_dir(root)
    with repo.lock(sdir):
        state = repo.load_state(sdir)
        if auto and state.get("failures", 0) >= cfg.dream.max_failures:
            return Result("skipped", f"paused after {state['failures']} failed passes; run `dreamer run` manually")

        live = memory_dir(root, cfg)
        batch = transcripts.collect(root, cfg, state["files"], cfg.dream.batch_chars)

        if not batch.text:
            if batch.offsets and not dry_run:  # only non-repo / non-content records were read
                _advance(state, batch)
                repo.save_state(sdir, state)
            return Result("skipped", "no new session activity for this repository")
        if len(batch.text) < cfg.dream.min_new_chars and not force:
            return Result("skipped", f"only {len(batch.text)} chars of new activity (< {cfg.dream.min_new_chars}); waiting for more")

        before_hash = store.tree_hash(live)
        staging = sdir / "staging"
        store.copy_store(live, staging)
        _ensure_index(staging)
        tool = store.MemoryTool(staging, cfg.memory)
        handlers = {
            "memory": tool.execute,
            "search_transcripts": lambda inp: transcripts.search(root, cfg, inp.get("pattern", ""), inp.get("max_results", 8)),
        }
        usage: Dict[str, int] = {}
        started = time.time()
        try:
            client = client_factory()
            messages = [{"role": "user", "content": _user_prompt(cfg, staging, batch, store.validate(staging, cfg.memory))}]
            summary, used = run_loop(client, cfg.model, SYSTEM_PROMPT, messages, handlers, cfg.model.max_turns, usage)
            issues = store.validate(staging, cfg.memory)
            if issues and tool.changed and used < cfg.model.max_turns:
                messages.append({"role": "user", "content": "The store now has these problems; fix them, then end:\n"
                                 + "\n".join(f"- {i}" for i in issues)})
                summary, _ = run_loop(client, cfg.model, SYSTEM_PROMPT, messages, handlers, cfg.model.max_turns - used, usage)
                issues = store.validate(staging, cfg.memory)
            if not (staging / store.INDEX).is_file():
                raise DreamError("MEMORY.md missing after pass")
            if store.tree_hash(live) != before_hash:
                raise DreamError("memory directory was edited during the pass; will retry next time")
        except DreamError as e:
            if not dry_run:
                state["failures"] = state.get("failures", 0) + 1
                state["runs"].append({"at": _now(), "status": "failed", "error": str(e), "usage": usage})
                repo.save_state(sdir, state)
            repo.log(sdir, f"pass failed: {e} usage={usage}")
            raise

        if dry_run:
            preview = (staging / store.INDEX).read_text(encoding="utf-8")
            shutil.rmtree(staging, ignore_errors=True)
            return Result("dry-run", f"{summary}\n\n--- {store.INDEX} preview ---\n{preview}", usage=usage)

        changed: List[str] = []
        if tool.changed or not (live / store.INDEX).exists():
            store.snapshot(live, sdir / "snapshots", json.dumps(state), cfg.dream.snapshots)
            changed = store.apply_store(staging, live)
        shutil.rmtree(staging, ignore_errors=True)

        _advance(state, batch)
        state["failures"] = 0
        state["runs"].append({
            "at": _now(), "status": "applied" if changed else "unchanged", "sessions": batch.sessions,
            "digest_chars": len(batch.text), "changed": changed, "issues": issues, "usage": usage,
            "seconds": round(time.time() - started, 1), "summary": summary[:300],
        })
        repo.save_state(sdir, state)
        repo.log(sdir, f"pass {'applied' if changed else 'unchanged'}: {len(batch.text)} chars, changed={changed} usage={usage}")
        return Result("applied" if changed else "unchanged", summary, changed, usage)


def _advance(state: Dict[str, Any], batch: transcripts.Batch) -> None:
    files = state.setdefault("files", {})
    for k, off in batch.offsets.items():
        files[k] = {"offset": off}
    for k in list(files):  # transcripts removed by Claude Code's retention sweep
        if not Path(k).exists():
            del files[k]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def undo(root: Path, cfg: DreamerConfig) -> Optional[str]:
    """Restore the memory directory and cursor from the latest snapshot, so the
    undone sessions are dreamed again on the next pass."""
    sdir = repo.state_dir(root)
    with repo.lock(sdir):
        snaps = sorted(p for p in (sdir / "snapshots").glob("*") if p.is_dir()) if (sdir / "snapshots").exists() else []
        if not snaps:
            return None
        snap = snaps[-1]
        store.apply_store(snap / "memory", memory_dir(root, cfg))
        if (snap / "state.json").exists():
            state = json.loads((snap / "state.json").read_text(encoding="utf-8"))
            state["failures"] = 0
            repo.save_state(sdir, state)
        shutil.rmtree(snap)
        return snap.name
