import argparse
import sys
import json
from pathlib import Path
from dreamer.config import DreamerConfig
from dreamer.memory import MemoryManager
from dreamer.consolidator import Consolidator

HOOK_TEMPLATE = """#!/usr/bin/env bash
# dreamer post-commit hook: runs background memory dreaming
if command -v dreamer >/dev/null 2>&1; then
    (dreamer run --quiet >/dev/null 2>&1 &)
fi
"""

CURSORRULES_TEMPLATE = """# Cursor Agent Rules
- Always consult `MEMORY.md` before making architectural assumptions.
- Adhere strictly to the Core Invariants and Verified Rules in `MEMORY.md`.
- Never pollute `MEMORY.md` with temporary git diffs or unfinished task status.
- Refer to `memory/<topic>.md` for deep context on specific subsystems.
"""

CLAUDE_MD_TEMPLATE = """# Claude Code Agent Rules
- Memory Architecture: This repo uses `dreamer` based on Anthropic's "The components of agent memory".
- Primary Index: Always read `MEMORY.md` first. It is capped at 200 lines and contains high-level invariants.
- No Transient State: Do not write temporary diffs or progress logs to memory.
- Deep Topics: Deep architectural decisions and notes belong in `memory/<topic>.md`.
"""

EXAMPLE_TOML_CONFIG = """# dreamer.toml - Agent Memory Configuration

# Presets: "vibe" (zero-config, silent background), "strict" (hard verification), "local" (offline), "custom"
preset = "vibe"

[memory]
index_file = "MEMORY.md"
topics_dir = "memory"
max_lines = 200
max_bytes = 25600
archive_threshold = 180

[model]
# Provider: "auto", "anthropic", "openai", "ollama", "heuristic"
provider = "auto"
model = "claude-3-5-haiku-20241022"
api_key_env = "ANTHROPIC_API_KEY"
# base_url = "http://localhost:11434/v1" # for Ollama or custom endpoints
temperature = 0.2

[hook]
hook_type = "post-commit"
run_background = true
quiet = true

[rules]
focus_areas = [
    "core_invariants",
    "runtime_gotchas",
    "architectural_decisions",
    "verified_bugfix_rules"
]
ignore_files = [
    "*.lock",
    "node_modules/**",
    "dist/**",
    "build/**"
]
"""


def main():
    parser = argparse.ArgumentParser(
        prog="dreamer",
        description="Lightweight, extensible dreaming & memory consolidation engine for coding agents",
    )
    subparsers = parser.add_subparsers(dest="command", help="Sub-commands")

    # init
    init_p = subparsers.add_parser("init", help="Initialize agent memory and hooks for current repository")
    init_p.add_argument("--repo-dir", default=".", help="Target repository directory (default: current dir)")
    init_p.add_argument(
        "--preset",
        choices=["vibe", "strict", "local", "custom"],
        default="vibe",
        help="Setup preset (default: vibe - zero-friction background)",
    )
    init_p.add_argument(
        "--agent",
        choices=["claude", "cursor", "all", "none"],
        default="all",
        help="Generate assistant guidance files (default: all - CLAUDE.md & .cursorrules)",
    )
    init_p.add_argument("--with-config", action="store_true", help="Generate a customized dreamer.toml config file")

    # run
    run_p = subparsers.add_parser("run", help="Run the dreaming consolidation pass")
    run_p.add_argument("--repo-dir", default=".", help="Target repository directory (default: current dir)")
    run_p.add_argument("--trace-file", default=None, help="Path to optional agent session trace log (.jsonl)")
    run_p.add_argument("--commits", type=int, default=5, help="Number of recent commits to analyze (default: 5)")
    run_p.add_argument("--dry-run", action="store_true", help="Print preview without writing files")
    run_p.add_argument("--quiet", action="store_true", help="Suppress non-error output (for git hooks)")

    # check
    check_p = subparsers.add_parser("check", help="Check memory index line/byte limits and broken links")
    check_p.add_argument("--repo-dir", default=".", help="Target repository directory (default: current dir)")

    # config
    config_p = subparsers.add_parser("config", help="Manage dreamer configuration")
    config_sub = config_p.add_subparsers(dest="config_command", help="Config commands")
    config_init = config_sub.add_parser("init", help="Create a dreamer.toml template in current directory")
    config_init.add_argument("--repo-dir", default=".", help="Target repository directory")
    config_show = config_sub.add_parser("show", help="Show active configuration values")
    config_show.add_argument("--repo-dir", default=".", help="Target repository directory")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    repo_dir = Path(getattr(args, "repo_dir", ".")).resolve()
    config = DreamerConfig.load(repo_dir)

    if args.command == "init":
        if args.preset != "vibe":
            config = DreamerConfig.from_preset(args.preset)

        mgr = MemoryManager(repo_dir, config.memory)
        mgr.ensure_structure()

        # Install git hook
        git_dir = repo_dir / ".git"
        if git_dir.exists() and config.hook.hook_type != "none":
            hook_file = git_dir / "hooks" / config.hook.hook_type
            hook_file.parent.mkdir(parents=True, exist_ok=True)
            hook_file.write_text(HOOK_TEMPLATE, encoding="utf-8")
            hook_file.chmod(0o755)
            print(f"[dreamer] Installed git {config.hook.hook_type} hook.")

        # Create Agent templates
        if args.agent in ("claude", "all"):
            claude_file = repo_dir / "CLAUDE.md"
            if not claude_file.exists():
                claude_file.write_text(CLAUDE_MD_TEMPLATE, encoding="utf-8")
                print("[dreamer] Created CLAUDE.md")

        if args.agent in ("cursor", "all"):
            cursor_file = repo_dir / ".cursorrules"
            if not cursor_file.exists():
                cursor_file.write_text(CURSORRULES_TEMPLATE, encoding="utf-8")
                print("[dreamer] Created .cursorrules")

        if args.with_config or args.preset == "custom":
            cfg_file = repo_dir / "dreamer.toml"
            if not cfg_file.exists():
                cfg_file.write_text(EXAMPLE_TOML_CONFIG, encoding="utf-8")
                print("[dreamer] Created dreamer.toml")

        print(f"[dreamer] Setup complete (Preset: {args.preset}) in {repo_dir}")

    elif args.command == "check":
        mgr = MemoryManager(repo_dir, config.memory)
        content = mgr.read_index()
        is_valid, lcount, bcount = mgr.check_limits(content)
        issues = mgr.check_pointers()
        topics = mgr.list_topics()

        print(f"[dreamer] Index File: {config.memory.index_file}")
        print(f"[dreamer] Lines: {lcount}/{config.memory.max_lines} lines")
        print(f"[dreamer] Size: {bcount} bytes (max {config.memory.max_bytes} bytes)")
        print(f"[dreamer] Topics: {len(topics)} topic files archived in `{config.memory.topics_dir}/`")

        if issues:
            print(f"[dreamer] Warning: {len(issues)} broken topic pointer(s) detected:")
            for issue in issues:
                print(f"  - [{issue['text']}]({issue['link']}) -> File not found!")

        if not is_valid:
            print("[dreamer] Status: FAILED (Exceeds recommended injection limits)")
            sys.exit(1)
        else:
            print("[dreamer] Status: PASSED (Healthy)")

    elif args.command == "run":
        mgr = MemoryManager(repo_dir, config.memory)
        consolidator = Consolidator(mgr, config)

        trace_data = None
        if args.trace_file and Path(args.trace_file).exists():
            trace_data = Path(args.trace_file).read_text(encoding="utf-8")

        new_index = consolidator.dream(session_trace=trace_data, dry_run=args.dry_run)

        if args.dry_run:
            print("--- [Dry Run Preview: Updated MEMORY.md] ---")
            print(new_index)
        elif not args.quiet:
            print(f"[dreamer] Dream completed. {config.memory.index_file} successfully consolidated.")

    elif args.command == "config":
        if args.config_command == "init":
            cfg_file = repo_dir / "dreamer.toml"
            cfg_file.write_text(EXAMPLE_TOML_CONFIG, encoding="utf-8")
            print(f"[dreamer] Created config template at {cfg_file}")
        elif args.config_command == "show":
            print(f"[dreamer] Active Preset: {config.preset}")
            print(f"  - Memory Index: {config.memory.index_file} (max {config.memory.max_lines} lines)")
            print(f"  - Model Provider: {config.model.provider} ({config.model.model})")
            print(f"  - Git Hook: {config.hook.hook_type} (bg: {config.hook.run_background})")
            print(f"  - Focus Areas: {', '.join(config.rules.focus_areas)}")


if __name__ == "__main__":
    main()
