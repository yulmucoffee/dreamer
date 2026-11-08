import subprocess
from pathlib import Path
from typing import Optional
from dreamer.config import DreamerConfig
from dreamer.memory import MemoryManager
from dreamer.llm import get_provider


def build_system_prompt(config: DreamerConfig) -> str:
    focus_str = ", ".join(config.rules.focus_areas)
    return f"""You are an Agent Memory Dreaming Engine.
Your job is offline memory consolidation (sleep-time compute) for an AI coding agent, based on Anthropic's "The components of agent memory".

Core Principles:
1. Memory is a prediction of future utility, not a diary of past actions.
2. Never record transient state (e.g., 'modified file X', 'tests currently failing').
3. Only record persistent invariants, non-obvious failure modes, and verified resolution rules.
4. The output index MUST fit within {config.memory.max_lines} lines (or {config.memory.max_bytes // 1024}KB).
5. Offload detailed reference material into topic files (`{config.memory.topics_dir}/<topic>.md`) if needed.

Current Focus Areas:
{focus_str}

Task:
Refactor and update `{config.memory.index_file}`:
- Extract newly verified invariants and rules from recent work.
- Resolve any contradictions or outdated entries.
- Maintain a clean, scannable index with pointers to deep topic files.
Output ONLY the markdown content of the new index.
"""


class Consolidator:
    def __init__(self, memory_mgr: MemoryManager, config: DreamerConfig):
        self.mgr = memory_mgr
        self.config = config

    def collect_recent_context(self, commit_count: int = 5) -> str:
        ctx_parts = []
        try:
            log_res = subprocess.run(
                ["git", "log", f"-n{commit_count}", "--oneline"],
                cwd=self.mgr.root_dir,
                capture_output=True,
                text=True,
            )
            if log_res.returncode == 0 and log_res.stdout.strip():
                ctx_parts.append(f"### Recent Git Commits:\n{log_res.stdout.strip()}")
        except Exception:
            pass

        try:
            # Build diff excluding ignored files
            cmd = ["git", "diff", "HEAD~1", "HEAD", "--stat"]
            diff_res = subprocess.run(
                cmd,
                cwd=self.mgr.root_dir,
                capture_output=True,
                text=True,
            )
            if diff_res.returncode == 0 and diff_res.stdout.strip():
                ctx_parts.append(f"### Recent Changes Overview:\n{diff_res.stdout.strip()}")
        except Exception:
            pass

        return "\n\n".join(ctx_parts)

    def dream(self, session_trace: Optional[str] = None, dry_run: bool = False) -> str:
        self.mgr.ensure_structure()
        current_index = self.mgr.read_index()
        recent_ctx = self.collect_recent_context()

        full_input = f"## Current {self.config.memory.index_file}:\n```markdown\n{current_index}\n```\n\n"
        if session_trace:
            full_input += f"## Recent Session Trace:\n```\n{session_trace}\n```\n\n"
        if recent_ctx:
            full_input += f"## Git Context:\n{recent_ctx}\n\n"

        system_prompt = build_system_prompt(self.config)
        provider = get_provider(self.config.model)

        try:
            new_index = provider.complete(system_prompt, full_input)
        except Exception as e:
            # Graceful fallback: do not crash the developer's git hook
            print(f"[dreamer] LLM generation error ({e}), falling back to safe compaction.", flush=True)
            new_index = self.mgr.prune_and_truncate(current_index)

        # Strip markdown fences if wrapped
        if new_index.startswith("```markdown"):
            new_index = new_index[len("```markdown"):].strip()
        if new_index.startswith("```"):
            new_index = new_index[len("```"):].strip()
        if new_index.endswith("```"):
            new_index = new_index[:-3].strip()

        # Enforce hard line limit safeguard
        new_index = self.mgr.prune_and_truncate(new_index)

        if not dry_run:
            self.mgr.write_index(new_index)

        return new_index
