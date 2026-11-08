import argparse
import sys
from pathlib import Path
from dreamer.memory import MemoryManager
from dreamer.consolidator import Consolidator

HOOK_TEMPLATE = """#!/usr/bin/env bash
# dreamer post-commit hook: runs background memory dreaming
if command -v dreamer >/dev/null 2>&1; then
    (dreamer run >/dev/null 2>&1 &)
fi
"""


def main():
    parser = argparse.ArgumentParser(
        prog="dreamer",
        description="Sleep-time compute & memory consolidation engine for coding agents (Anthropic agent memory architecture)",
    )
    subparsers = parser.add_subparsers(dest="command", help="Sub-commands")

    # init
    init_p = subparsers.add_parser("init", help="Initialize MEMORY.md and install git post-commit hook")
    init_p.add_argument("--repo-dir", default=".", help="Target repository directory (default: current dir)")

    # run
    run_p = subparsers.add_parser("run", help="Run the dreaming pass (consolidate recent commits/traces into MEMORY.md)")
    run_p.add_argument("--repo-dir", default=".", help="Target repository directory (default: current dir)")
    run_p.add_argument("--trace-file", default=None, help="Optional agent session trace log file (e.g. .jsonl)")

    # check
    check_p = subparsers.add_parser("check", help="Check if MEMORY.md adheres to the 200 lines / 25KB limit")
    check_p.add_argument("--repo-dir", default=".", help="Target repository directory (default: current dir)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    repo_dir = Path(args.repo_dir).resolve()
    mgr = MemoryManager(repo_dir)

    if args.command == "init":
        mgr.ensure_structure()
        hook_path = repo_dir / ".git" / "hooks" / "post-commit"
        if (repo_dir / ".git").exists():
            hook_path.parent.mkdir(parents=True, exist_ok=True)
            hook_path.write_text(HOOK_TEMPLATE, encoding="utf-8")
            hook_path.chmod(0o755)
            print(f"[dreamer] Initialized MEMORY.md and installed post-commit hook in {repo_dir}")
        else:
            print(f"[dreamer] Initialized MEMORY.md in {repo_dir} (not a git repo, hook skipped)")

    elif args.command == "check":
        content = mgr.read_index()
        is_valid, lcount, bcount = mgr.check_limits(content)
        print(f"[dreamer] MEMORY.md lines: {lcount}/200, size: {bcount} bytes / 25KB")
        if not is_valid:
            print("[dreamer] Warning: MEMORY.md exceeds recommended limits!")
            sys.exit(1)
        else:
            print("[dreamer] OK: MEMORY.md fits within context injection limits.")

    elif args.command == "run":
        trace_data = None
        if args.trace_file and Path(args.trace_file).exists():
            trace_data = Path(args.trace_file).read_text(encoding="utf-8")
        consolidator = Consolidator(mgr)
        success = consolidator.dream(session_trace=trace_data)
        if success:
            print("[dreamer] Dream completed successfully. MEMORY.md updated.")
        else:
            print("[dreamer] Dream finished with warnings (limits exceeded).")


if __name__ == "__main__":
    main()
