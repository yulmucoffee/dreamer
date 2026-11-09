import argparse
import sys
import time
from pathlib import Path

import json

from dreamer import capture, dream, engines, install, mcp, notes, personal, repo, store, targets, transcripts
from dreamer.config import EXAMPLE_TOML, DreamerConfig
from dreamer.llm import DreamError


def _print(quiet: bool, sdir: Path, msg: str, err: bool = False) -> None:
    if quiet:
        repo.log(sdir, msg)
    else:
        print(f"[dreamer] {msg}", file=sys.stderr if err else sys.stdout)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="dreamer", description="Offline memory consolidation for coding agents")
    sub = parser.add_subparsers(dest="command")

    def add(name, help):
        p = sub.add_parser(name, help=help)
        p.add_argument("--repo-dir", default=".", help="repository (default: current directory)")
        return p

    p = add("init", "set up shared memory for the harnesses you use in this repository")
    p.add_argument("--harness", default="", help="comma list of claude,codex,cursor,gemini,agents "
                   "(default: detected from installed CLIs; agents = AGENTS.md only, for Copilot, Windsurf, Amp, ...)")
    p.add_argument("--no-hooks", action="store_true", help="don't add harness hooks (session capture / auto dreaming)")
    p.add_argument("--git-hook", action="store_true", help="also dream after each commit (harness-independent)")
    p.add_argument("--with-config", action="store_true", help="write a dreamer.toml with the defaults")
    p.add_argument("--personal", action="store_true", help="also share your personal memory (~/.dreamer/personal) "
                   "with this repository, through local uncommitted files only")

    p = add("personal", "turn personal memory on or off for this repository, or show it")
    p.add_argument("action", choices=["on", "off", "show"])
    p.add_argument("--harness", default="", help="on: comma list (default: the ones dreamer init set up)")

    p = add("mcp", "serve memory over MCP (stdio) for chat apps and other MCP clients")
    p.add_argument("--print-config", action="store_true", help="print the client config snippet and exit")

    p = add("capture", "record a harness hook event from stdin (used by installed hooks)")
    p.add_argument("source", choices=["cursor", "generic"])

    p = add("note", "record an observation for the next dreaming pass (any harness)")
    p.add_argument("--personal", dest="personal_scope", action="store_true", help="act on your personal memory itself (notes from chat apps), not a project")
    p.add_argument("text", nargs="?", help="note text (default: read stdin)")
    p.add_argument("--source", default="", help="who wrote it, e.g. codex, cursor, gemini")

    p = add("dream", "run a pass yourself as the model, from any harness; no API key needed")
    p.add_argument("--personal", dest="personal_scope", action="store_true", help="act on your personal memory itself (notes from chat apps), not a project")
    p.add_argument("action", choices=["start", "memory", "search", "finish", "abort"])
    p.add_argument("arg", nargs="?", help="memory: JSON command (default: stdin); search: regex")
    p.add_argument("--max", type=int, default=8, help="search: max results")
    p.add_argument("--summary", default="", help="finish: one line on what changed and why")
    p.add_argument("--accept", action="store_true", help="finish: apply despite reported problems")
    p.add_argument("--force", action="store_true", help="start: even below the new-activity threshold")
    p.add_argument("--restart", action="store_true", help="start: discard a pass already in progress")

    add("sync", "refresh the memory copies injected into instruction files")

    p = add("run", "run a dreaming pass over new session transcripts")
    p.add_argument("--personal", dest="personal_scope", action="store_true", help="act on your personal memory itself (notes from chat apps), not a project")
    p.add_argument("--auto", action="store_true", help="hook mode: respect the threshold and failure backoff")
    p.add_argument("--engine", default="", help="auto | api | claude-code | codex | cursor | none (default: config)")
    p.add_argument("--force", action="store_true", help="dream even below the new-activity threshold")
    p.add_argument("--dry-run", action="store_true", help="show the result without applying or advancing the cursor")
    p.add_argument("--batches", type=int, default=1, help="process up to N batches (to drain a backlog)")
    p.add_argument("--wait", type=int, default=0, help="seconds to wait first (lets transcripts flush)")
    p.add_argument("--quiet", action="store_true", help="log to the state dir instead of printing")

    add("check", "check budgets, links and unreachable pages")
    add("status", "show backlog, store usage and recent passes")
    add("undo", "restore memory (and cursor) from before the last applied pass")
    add("uninstall", "remove the CLAUDE.md block and hooks; keep memory files")
    p = add("config", "print an example dreamer.toml")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1

    if args.command == "mcp":
        repo_dir = Path(args.repo_dir).resolve()
        if args.print_config:
            print(mcp.client_config(repo_dir if args.repo_dir != "." else None))
            return 0
        mcp.serve(repo_dir)
        return 0

    root = repo.find_root(Path(args.repo_dir))
    if getattr(args, "personal_scope", False):
        root = personal.home()
        personal.ensure_store()
    cfg = personal.adapt(root, DreamerConfig.load(root))
    mem = root / cfg.memory.dir

    if args.command == "init":
        harnesses = [h.strip() for h in args.harness.split(",") if h.strip()] or None
        bad = [h for h in harnesses or [] if h not in install.HARNESSES]
        if bad:
            print(f"[dreamer] unknown harness(es): {', '.join(bad)} (choose from {', '.join(install.HARNESSES)})", file=sys.stderr)
            return 2
        for line in install.init(root, cfg, harnesses=harnesses, git_hook=args.git_hook,
                                 hooks=not args.no_hooks, with_config=args.with_config):
            print(f"[dreamer] {line}")
        if args.personal:
            for line in personal.enable(root, harnesses or install.detect_harnesses(root)):
                print(f"[dreamer] {line}")
        files = " ".join([f"{cfg.memory.dir}/"] + [targets.FILES[k] for k in targets.installed(root)])
        print(f"[dreamer] Ready. Commit {files} to share the project memory.")
        return 0

    if args.command == "personal":
        if args.action == "on":
            kinds = [h.strip() for h in args.harness.split(",") if h.strip()] or install.detect_harnesses(root)
            lines = personal.enable(root, kinds)
        elif args.action == "off":
            lines = personal.disable(root)
        else:
            d = personal.store_dir()
            n, total = store.usage(d)
            lines = [f"{'enabled' if personal.enabled(root) else 'not enabled'} for this repository "
                     f"(harnesses: {', '.join(personal.harnesses(root)) or '-'})",
                     f"store {d}: {n} files, {total} B"]
            idx = d / store.INDEX
            if idx.exists():
                lines.append(idx.read_text(encoding="utf-8").rstrip())
        for line in lines:
            print(f"[dreamer] {line}" if not line.startswith("#") and "\n" not in line else line)
        return 0

    if args.command == "capture":
        try:
            print(capture.handle(args.source, sys.stdin.read(), Path.cwd()))
        except Exception:  # a hook must never break the harness
            print(json.dumps({"continue": True}))
        return 0

    if args.command == "note":
        text = args.text if args.text is not None else sys.stdin.read()
        try:
            path = notes.add(root, text, args.source)
        except ValueError as e:
            print(f"[dreamer] {e}", file=sys.stderr)
            return 2
        print(f"[dreamer] noted ({path.name}); it will be consolidated on the next dreaming pass")
        return 0

    if args.command == "sync":
        changed = targets.sync(root, cfg)
        print(f"[dreamer] {'updated ' + ', '.join(changed) if changed else 'injected copies are up to date'}")
        return 0

    if args.command == "dream":
        return _dream(args, root, cfg)

    if args.command == "uninstall":
        if personal.enabled(root):
            for line in personal.disable(root):
                print(f"[dreamer] {line}")
        for line in install.uninstall(root):
            print(f"[dreamer] {line}")
        return 0

    if args.command == "config":
        print(EXAMPLE_TOML, end="")
        return 0

    if args.command == "check":
        issues = store.validate(mem, cfg.memory) if mem.is_dir() else [f"{mem} does not exist; run `dreamer init`"]
        n, total = store.usage(mem)
        idx = mem / store.INDEX
        if idx.exists():
            t = idx.read_text(encoding="utf-8")
            print(f"[dreamer] {store.INDEX}: {len(t.splitlines())}/{cfg.memory.index_max_lines} lines, "
                  f"{len(t.encode())}/{cfg.memory.index_max_bytes} B (injected every session)")
        print(f"[dreamer] store: {n}/{cfg.memory.max_files} files, {total}/{cfg.memory.store_max_bytes} B")
        if personal.enabled(root):
            pd = personal.store_dir()
            pn, ptotal = store.usage(pd)
            pc = personal.config()
            print(f"[dreamer] personal store ({pd}): {pn}/{pc.max_files} files, {ptotal}/{pc.store_max_bytes} B")
            if pd.is_dir():
                issues = issues + [f"personal: {i}" for i in store.validate(pd, pc)]
        stale = targets.stale(root, cfg)
        if stale:
            issues = issues + [f"injected copy out of date in {', '.join(stale)} (run `dreamer sync`)"]
        for i in issues:
            print(f"  - {i}")
        print(f"[dreamer] {'PASSED' if not issues else 'ISSUES FOUND'}")
        return 1 if issues else 0

    sdir = repo.state_dir(root)

    if args.command == "status":
        state = repo.load_state(sdir)
        files = transcripts.candidate_files(root, cfg)
        backlog = sum(max(0, f.stat().st_size - int(state["files"].get(str(f), {}).get("offset", 0)))
                      for _, f in files if not state["files"].get(str(f), {}).get("ctx", {}).get("foreign"))
        n, total = store.usage(mem)
        by_src = {}
        for src, _ in files:
            by_src[src] = by_src.get(src, 0) + 1
        print(f"[dreamer] transcripts: {by_src or 'none found'}, {backlog} raw bytes not yet dreamed; "
              f"notes waiting: {len(notes.pending(root))}")
        print(f"[dreamer] injected into: {', '.join(targets.FILES[k] for k in targets.installed(root)) or 'nothing'}")
        print(f"[dreamer] store: {n} files, {total} B; consecutive failures: {state.get('failures', 0)}")
        for r in state.get("runs", [])[-5:]:
            u = r.get("usage", {})
            print(f"  {r['at']} {r['status']:9} in={u.get('input_tokens', 0)} cached={u.get('cache_read_input_tokens', 0)} "
                  f"out={u.get('output_tokens', 0)} {r.get('error') or r.get('summary', '')}"[:200])
        return 0

    if args.command == "undo":
        name = dream.undo(root, cfg)
        print(f"[dreamer] {'restored snapshot ' + name if name else 'no snapshot to restore'}")
        return 0 if name else 1

    if args.command == "run":
        if args.wait:
            time.sleep(args.wait)
        code = 0
        for _ in range(max(1, args.batches)):
            try:
                res = engines.run(root, cfg, engine=args.engine, auto=args.auto, force=args.force, dry_run=args.dry_run)
            except repo.Busy:
                _print(args.quiet, sdir, "another pass is running; skipped")
                break
            except DreamError as e:
                _print(args.quiet, sdir, f"pass aborted, memory unchanged: {e}", err=True)
                code = 1
                break
            _print(args.quiet, sdir, f"{res.status}: {res.message}".strip())
            if res.status in ("skipped", "dry-run"):
                break
        if not args.dry_run:
            targets.sync(root, cfg)  # also picks up hand edits to MEMORY.md
        # Notes left for personal memory (chat apps, other repositories) are
        # consolidated by a personal-only pass, triggered from opted-in repos.
        if code == 0 and not args.dry_run and not personal.is_root(root) and personal.enabled(root) \
                and notes.pending(personal.home()):
            home = personal.home()
            pcfg = DreamerConfig.load(home) if (home / "dreamer.toml").exists() else DreamerConfig()
            if not (home / "dreamer.toml").exists():
                pcfg.dream, pcfg.model = cfg.dream, cfg.model
            pcfg = personal.adapt(home, pcfg)
            try:
                res = engines.run(home, pcfg, engine=args.engine, auto=args.auto)
                _print(args.quiet, sdir, f"personal: {res.status}: {res.message}".strip())
            except (repo.Busy, DreamError) as e:
                _print(args.quiet, sdir, f"personal pass skipped: {e}", err=True)
            targets.sync(root, cfg)
        return code

    return 1


def _dream(args, root: Path, cfg: DreamerConfig) -> int:
    try:
        if args.action == "start":
            res = dream.external_start(root, cfg, force=args.force, restart=args.restart)
            print(res.message if res.status == "started" else f"[dreamer] {res.status}: {res.message}")
            return 0 if res.status in ("started", "skipped") else 1
        if args.action == "memory":
            raw = args.arg if args.arg is not None else sys.stdin.read()
            try:
                command = json.loads(raw)
            except ValueError as e:
                print(f"Error: invalid JSON ({e})")
                return 1
            print(dream.external_memory(root, cfg, command))
            return 0
        if args.action == "search":
            print(dream.external_search(root, cfg, args.arg or "", args.max))
            return 0
        if args.action == "finish":
            res = dream.external_finish(root, cfg, summary=args.summary, accept=args.accept)
            if res.status == "needs-fix":
                print(res.message)
                return 3
            print(f"[dreamer] {res.status}: changed {res.changed or 'nothing'}")
            return 0
        if args.action == "abort":
            print(f"[dreamer] {'pass discarded' if dream.external_abort(root) else 'no pass in progress'}")
            return 0
    except (store.ToolError, DreamError) as e:
        print(f"Error: {e}")
        return 1
    except repo.Busy:
        print("Error: another pass is running")
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
