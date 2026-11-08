import argparse
import sys
import time
from pathlib import Path

from dreamer import dream, install, repo, store, transcripts
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

    p = add("init", "set up memory, the CLAUDE.md import and the SessionEnd hook for this repository")
    p.add_argument("--git-hook", action="store_true", help="also dream after each commit")
    p.add_argument("--cursor", action="store_true", help="also add a Cursor rule pointing at the memory")
    p.add_argument("--with-config", action="store_true", help="write a dreamer.toml with the defaults")

    p = add("run", "run a dreaming pass over new session transcripts")
    p.add_argument("--auto", action="store_true", help="hook mode: respect the threshold and failure backoff")
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

    root = repo.find_root(Path(args.repo_dir))
    cfg = DreamerConfig.load(root)
    mem = root / cfg.memory.dir

    if args.command == "init":
        for line in install.init(root, cfg, git_hook=args.git_hook, cursor=args.cursor, with_config=args.with_config):
            print(f"[dreamer] {line}")
        print(f"[dreamer] Ready. Memory: {mem}. Commit {cfg.memory.dir}/ and CLAUDE.md to share it.")
        return 0

    if args.command == "uninstall":
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
        for i in issues:
            print(f"  - {i}")
        print(f"[dreamer] {'PASSED' if not issues else 'ISSUES FOUND'}")
        return 1 if issues else 0

    sdir = repo.state_dir(root)

    if args.command == "status":
        state = repo.load_state(sdir)
        files = transcripts.candidate_files(root, cfg)
        backlog = sum(max(0, f.stat().st_size - int(state["files"].get(str(f), {}).get("offset", 0))) for f in files)
        n, total = store.usage(mem)
        print(f"[dreamer] transcripts: {len(files)} files, {backlog} raw bytes not yet dreamed")
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
                res = dream.run(root, cfg, auto=args.auto, force=args.force, dry_run=args.dry_run)
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
        return code

    return 1


if __name__ == "__main__":
    sys.exit(main())
