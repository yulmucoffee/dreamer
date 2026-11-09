"""One dreaming pass: digest new material (agent notes, then transcripts), let
a model edit a staged copy of the store, validate, then apply atomically and
advance the cursor.

Two drivers share every step except who plays the model:
- `run`: the built-in loop on the Claude API.
- `external_*`: any agent in any harness, on any model, drives the pass
  through `dreamer dream ...` commands; no API key needed.

Failure at any point leaves the memory directory, the cursor and the inbox as
they were, so the same material is retried on the next pass.
"""

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from dreamer import notes, repo, store, targets, transcripts
from dreamer.config import DreamerConfig
from dreamer.llm import DreamError, make_client, run_loop

SYSTEM_PROMPT = """You maintain the long-term memory of one software project. You run offline, between coding sessions.

Coding agents working in this repository start every session with MEMORY.md already in their context, and can open the other files in the memory directory when a topic is relevant. Your purpose is to make those future sessions waste less effort: less rediscovering how things work, fewer repeated mistakes, fewer questions the user already answered, and no acting on notes that are wrong.

A note is a prediction that it will matter again. What is cheap to rediscover from the code, git history or the screen is not worth its tokens, and neither is current state that will soon change (the branch being worked on, counts such as how many tests exist, what is in progress). Often the most valuable notes are what took effort to learn and cannot be seen in the code: decisions and preferences the user stated, and how external tools, services and APIs actually behave as found in their documentation or by trial and error (for example a rate limit, or a flag that behaves differently from its docs). Without a note, a future session repeats that research. A wrong or stale note is worse than none: it is injected into every session. When the sessions contradict a note, fix or remove it; when a claim matters and the digest is ambiguous, check it with search_transcripts. Record where a note came from, e.g. (2026-11-08, s:ab12cd34), so later passes can re-check it.

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


EXTERNAL_TTL = 6 * 3600  # an external pass untouched this long is treated as abandoned


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
        f"## New session activity (digest; tool output clipped, reasoning omitted).{more}\n{batch.text}"
    )
    return "\n\n".join(parts)


def _ensure_index(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    idx = d / store.INDEX
    if not idx.exists():
        idx.write_text("# Project memory\n", encoding="utf-8")


def _gather(root: Path, cfg: DreamerConfig, state: Dict[str, Any]) -> transcripts.Batch:
    """Agent notes first (explicitly flagged as salient), then transcripts."""
    note_text, note_files = notes.render(root, cfg.dream.batch_chars)
    batch = transcripts.collect(root, cfg, state["files"], cfg.dream.batch_chars, used=len(note_text))
    if note_text:
        batch.text = note_text + ("\n\n" + batch.text if batch.text else "")
        batch.notes = note_files
    return batch


def _gate(root: Path, cfg: DreamerConfig, sdir: Path, state: Dict[str, Any], batch: transcripts.Batch,
          force: bool, dry_run: bool) -> Optional[Result]:
    if not batch.text:
        if batch.cursors and not dry_run:  # only non-repo / non-content records were read
            _advance(state, batch)
            repo.save_state(sdir, state)
        return Result("skipped", "no new session activity for this repository")
    if len(batch.text) < cfg.dream.min_new_chars and not force:
        return Result("skipped", f"only {len(batch.text)} chars of new activity (< {cfg.dream.min_new_chars}); waiting for more")
    return None


def _stage(root: Path, cfg: DreamerConfig, sdir: Path) -> Path:
    staging = sdir / "staging"
    store.copy_store(memory_dir(root, cfg), staging)
    _ensure_index(staging)
    return staging


def _fail(sdir: Path, state: Dict[str, Any], err: Exception, usage: Dict[str, int], mode: str) -> None:
    state["failures"] = state.get("failures", 0) + 1
    state["runs"].append({"at": _now(), "status": "failed", "mode": mode, "error": str(err), "usage": usage})
    repo.save_state(sdir, state)
    repo.log(sdir, f"{mode} pass failed: {err} usage={usage}")


def _commit(root: Path, cfg: DreamerConfig, sdir: Path, state: Dict[str, Any], batch: transcripts.Batch,
            staging: Path, before_hash: str, summary: str, usage: Dict[str, int], mode: str, started: float) -> Result:
    live = memory_dir(root, cfg)
    if not (staging / store.INDEX).is_file():
        raise DreamError("MEMORY.md missing after pass")
    if store.tree_hash(live) != before_hash:
        raise DreamError("memory directory was edited during the pass; will retry next time")
    issues = store.validate(staging, cfg.memory)
    changed: List[str] = []
    if store.tree_hash(staging) != before_hash or not (live / store.INDEX).exists():
        snap = store.snapshot(live, sdir / "snapshots", json.dumps(state), cfg.dream.snapshots)
        changed = store.apply_store(staging, live)
    else:
        snap = None
    shutil.rmtree(staging, ignore_errors=True)
    if batch.notes:  # keep consumed notes with the snapshot so `undo` can put them back
        if snap is None:
            snap = store.snapshot(live, sdir / "snapshots", json.dumps(state), cfg.dream.snapshots)
        (snap / "inbox").mkdir(exist_ok=True)
        for f in batch.notes:
            if Path(f).exists():
                shutil.move(f, snap / "inbox" / Path(f).name)
    _advance(state, batch)
    state["failures"] = 0
    status = "applied" if changed else "unchanged"
    state["runs"].append({
        "at": _now(), "status": status, "mode": mode, "sessions": batch.sessions, "notes": len(batch.notes),
        "digest_chars": len(batch.text), "changed": changed, "issues": issues, "usage": usage,
        "seconds": round(time.time() - started, 1), "summary": summary[:300],
    })
    repo.save_state(sdir, state)
    synced = targets.sync(root, cfg)
    repo.log(sdir, f"{mode} pass {status}: {len(batch.text)} chars, changed={changed} synced={synced} usage={usage}")
    return Result(status, summary, changed, usage)


def _external_active(sdir: Path) -> bool:
    p = sdir / "external.json"
    return p.exists() and time.time() - p.stat().st_mtime < EXTERNAL_TTL


def run(
    root: Path,
    cfg: DreamerConfig,
    *,
    auto: bool = False,
    force: bool = False,
    dry_run: bool = False,
    client_factory: Callable[[], Any] = make_client,
) -> Result:
    """A pass driven by the built-in Claude API loop."""
    sdir = repo.state_dir(root)
    with repo.lock(sdir):
        if _external_active(sdir):
            return Result("skipped", "an agent-driven pass (`dreamer dream`) is in progress")
        state = repo.load_state(sdir)
        if auto and state.get("failures", 0) >= cfg.dream.max_failures:
            return Result("skipped", f"paused after {state['failures']} failed passes; run `dreamer run` manually")
        batch = _gather(root, cfg, state)
        gated = _gate(root, cfg, sdir, state, batch, force, dry_run)
        if gated:
            return gated

        before_hash = store.tree_hash(memory_dir(root, cfg))
        staging = _stage(root, cfg, sdir)
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
            if dry_run:
                preview = (staging / store.INDEX).read_text(encoding="utf-8")
                shutil.rmtree(staging, ignore_errors=True)
                return Result("dry-run", f"{summary}\n\n--- {store.INDEX} preview ---\n{preview}", usage=usage)
            return _commit(root, cfg, sdir, state, batch, staging, before_hash, summary, usage, "api", started)
        except DreamError as e:
            if not dry_run:
                _fail(sdir, state, e, usage, "api")
            raise


# -- agent-driven passes (any harness, any model) --------------------------------

EXTERNAL_GUIDE = """You are running a memory-consolidation ("dreaming") pass for this repository.
Play the role described under SYSTEM PROMPT, working on the material under USER MESSAGE.

Act only through these commands, run from the repository root. Do not edit files in `{dir}/` directly.
  dreamer dream memory '<json>'        memory tool; paths are under /memories. JSON may instead come on stdin:
                                       dreamer dream memory <<'JSON'
                                       {{"command":"create","path":"/memories/build.md","file_text":"# Build\\n- ...\\n"}}
                                       JSON
      {{"command":"view","path":"/memories"}}          (files: optional "view_range":[start,end])
      {{"command":"create","path":"/memories/<topic>.md","file_text":"..."}}
      {{"command":"str_replace","path":"...","old_str":"<exactly once>","new_str":"..."}}
      {{"command":"insert","path":"...","insert_line":<n>,"insert_text":"..."}}
      {{"command":"delete","path":"..."}}   {{"command":"rename","old_path":"...","new_path":"..."}}
  dreamer dream search '<regex>' [--max N]    search the full, unabridged transcripts of this repository
  dreamer dream finish --summary "<one line>" validate and apply; if it lists problems, fix them and finish again
  dreamer dream abort                         discard this pass; nothing changes
Where the system prompt says search_transcripts, use `dreamer dream search`.
"""


def _ext_path(sdir: Path) -> Path:
    return sdir / "external.json"


def external_start(root: Path, cfg: DreamerConfig, *, force: bool = False, restart: bool = False) -> Result:
    sdir = repo.state_dir(root)
    with repo.lock(sdir):
        if _external_active(sdir) and not restart:
            return Result("skipped", "a pass is already in progress; continue it, or `dreamer dream start --restart`")
        state = repo.load_state(sdir)
        batch = _gather(root, cfg, state)
        gated = _gate(root, cfg, sdir, state, batch, force, False)
        if gated:
            return gated
        staging = _stage(root, cfg, sdir)
        before_hash = store.tree_hash(memory_dir(root, cfg))
        prompt = _user_prompt(cfg, staging, batch, store.validate(staging, cfg.memory))
        repo.atomic_write(_ext_path(sdir), json.dumps({
            "started": time.time(), "before_hash": before_hash, "cursors": batch.cursors, "notes": batch.notes,
            "sessions": batch.sessions, "text_chars": len(batch.text),
        }))
        text = (EXTERNAL_GUIDE.format(dir=cfg.memory.dir) + "\n=== SYSTEM PROMPT ===\n" + SYSTEM_PROMPT
                + "\n\n=== USER MESSAGE ===\n" + prompt + "\n")
        return Result("started", text)


def _ext_load(sdir: Path) -> Dict[str, Any]:
    if not _external_active(sdir):
        raise DreamError("no pass in progress; run `dreamer dream start`")
    os.utime(_ext_path(sdir))  # activity keeps the pass alive
    return json.loads(_ext_path(sdir).read_text(encoding="utf-8"))


def external_memory(root: Path, cfg: DreamerConfig, command: Dict[str, Any]) -> str:
    sdir = repo.state_dir(root)
    _ext_load(sdir)
    return store.MemoryTool(sdir / "staging", cfg.memory).execute(command)


def external_search(root: Path, cfg: DreamerConfig, pattern: str, max_results: int = 8) -> str:
    _ext_load(repo.state_dir(root))
    return transcripts.search(root, cfg, pattern, max_results)


def external_finish(root: Path, cfg: DreamerConfig, summary: str = "", accept: bool = False) -> Result:
    sdir = repo.state_dir(root)
    with repo.lock(sdir):
        ext = _ext_load(sdir)
        staging = sdir / "staging"
        issues = store.validate(staging, cfg.memory)
        if issues and not accept:
            return Result("needs-fix", "Fix these problems, then run `dreamer dream finish` again "
                          "(or `--accept` to apply anyway):\n" + "\n".join(f"- {i}" for i in issues))
        state = repo.load_state(sdir)
        batch = transcripts.Batch(text="x" * ext["text_chars"], cursors=ext["cursors"],
                                  sessions=ext["sessions"], notes=ext["notes"])
        try:
            res = _commit(root, cfg, sdir, state, batch, staging, ext["before_hash"], summary, {}, "agent", ext["started"])
        except DreamError as e:
            _fail(sdir, state, e, {}, "agent")
            _ext_path(sdir).unlink(missing_ok=True)
            raise
        _ext_path(sdir).unlink(missing_ok=True)
        return res


def external_abort(root: Path) -> bool:
    sdir = repo.state_dir(root)
    with repo.lock(sdir):
        existed = _ext_path(sdir).exists()
        _ext_path(sdir).unlink(missing_ok=True)
        shutil.rmtree(sdir / "staging", ignore_errors=True)
        return existed


def _advance(state: Dict[str, Any], batch: transcripts.Batch) -> None:
    files = state.setdefault("files", {})
    for k, entry in batch.cursors.items():
        files[k] = entry
    for k in list(files):  # transcripts removed by retention sweeps or compression
        if not Path(k).exists():
            del files[k]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def undo(root: Path, cfg: DreamerConfig) -> Optional[str]:
    """Restore memory, cursor and consumed notes from the latest snapshot, so
    the undone material is dreamed again on the next pass."""
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
        if (snap / "inbox").is_dir():
            notes.inbox(root).mkdir(parents=True, exist_ok=True)
            for f in (snap / "inbox").iterdir():
                shutil.move(str(f), notes.inbox(root) / f.name)
        shutil.rmtree(snap)
        targets.sync(root, cfg)
        return snap.name
