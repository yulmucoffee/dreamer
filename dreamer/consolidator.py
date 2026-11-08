import os
import subprocess
from pathlib import Path
from typing import Optional
from dreamer.memory import MemoryManager, MAX_INDEX_LINES

DREAM_SYSTEM_PROMPT = """You are an Agent Memory Dreaming Engine.
Your job is offline memory consolidation (sleep-time compute) for an AI coding agent, based on Anthropic's "The components of agent memory".

Core Principles:
1. Memory is a prediction of future utility, not a diary of past actions.
2. Never record transient state (e.g., 'modified file X', 'tests currently failing').
3. Only record persistent invariants, non-obvious failure modes, and verified resolution rules.
4. The output index MUST fit within 200 lines (or 25KB). Offload detailed reference material into topic files if needed.

Input:
- Current MEMORY.md
- Recent session trace, git commits, or git diffs

Task:
Refactor and update MEMORY.md:
- Extract newly verified invariants and rules from recent work.
- Resolve any contradictions or outdated entries.
- If content grows beyond 200 lines, summarize the top-level index and suggest topic files to archive.
Output the exact new MEMORY.md content.
"""


class Consolidator:
    def __init__(self, memory_mgr: MemoryManager):
        self.mgr = memory_mgr

    def collect_recent_context(self) -> str:
        ctx_parts = []
        try:
            log_res = subprocess.run(
                ["git", "log", "-n", "5", "--oneline"],
                cwd=self.mgr.root_dir,
                capture_output=True,
                text=True,
            )
            if log_res.returncode == 0 and log_res.stdout.strip():
                ctx_parts.append(f"### Recent Git Commits:\n{log_res.stdout.strip()}")
        except Exception:
            pass

        try:
            diff_res = subprocess.run(
                ["git", "diff", "HEAD~1", "HEAD", "--stat"],
                cwd=self.mgr.root_dir,
                capture_output=True,
                text=True,
            )
            if diff_res.returncode == 0 and diff_res.stdout.strip():
                ctx_parts.append(f"### Recent Changes Overview:\n{diff_res.stdout.strip()}")
        except Exception:
            pass

        return "\n\n".join(ctx_parts)

    def dream(self, session_trace: Optional[str] = None) -> bool:
        self.mgr.ensure_structure()
        current_index = self.mgr.read_index()
        recent_ctx = self.collect_recent_context()

        full_input = f"## Current MEMORY.md:\n```markdown\n{current_index}\n```\n\n"
        if session_trace:
            full_input += f"## Recent Session Trace:\n```\n{session_trace}\n```\n\n"
        if recent_ctx:
            full_input += f"## Git Context:\n{recent_ctx}\n\n"

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            # Fallback heuristic mode: check and enforce 200 line limit
            lines = current_index.splitlines()
            if len(lines) > MAX_INDEX_LINES:
                trimmed = lines[:MAX_INDEX_LINES - 5]
                trimmed.append("\n<!-- Auto-pruned by dreamer: exceeded 200 lines limit. -->")
                self.mgr.write_index("\n".join(trimmed))
            return True

        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=os.environ.get("DREAMER_MODEL", "claude-3-5-haiku-20241022"),
            max_tokens=4096,
            system=DREAM_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": full_input}],
        )

        new_index = resp.content[0].text.strip()
        # Strip markdown fences if wrapped
        if new_index.startswith("```markdown"):
            new_index = new_index[len("```markdown"):].strip()
        if new_index.startswith("```"):
            new_index = new_index[len("```"):].strip()
        if new_index.endswith("```"):
            new_index = new_index[:-3].strip()

        is_valid, lcount, bcount = self.mgr.check_limits(new_index)
        self.mgr.write_index(new_index)
        return is_valid
