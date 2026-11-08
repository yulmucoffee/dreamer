import re
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional
from dreamer.config import MemoryConfig


class MemoryManager:
    def __init__(self, root_dir: Path, config: MemoryConfig):
        self.root_dir = Path(root_dir)
        self.config = config
        self.memory_file = self.root_dir / self.config.index_file
        self.topics_dir = self.root_dir / self.config.topics_dir

    def ensure_structure(self):
        self.topics_dir.mkdir(parents=True, exist_ok=True)
        if not self.memory_file.exists():
            default_template = (
                f"# Project Memory Index ({self.config.index_file})\n\n"
                f"<!-- Strict limit: max {self.config.max_lines} lines / {self.config.max_bytes // 1024}KB -->\n\n"
                "## 1. Core Invariants\n"
                "- Required package managers, build flags, and non-obvious runtime constraints.\n\n"
                "## 2. Verified Rules & Patterns\n"
                "- Permanent bugfixes and anti-patterns discovered during development.\n\n"
                "## 3. Topic Pointers\n"
                f"- Detailed architecture notes and ADRs are stored in `{self.config.topics_dir}/<topic>.md`.\n"
            )
            self.memory_file.write_text(default_template, encoding="utf-8")

    def read_index(self) -> str:
        if not self.memory_file.exists():
            return ""
        return self.memory_file.read_text(encoding="utf-8")

    def write_index(self, content: str):
        self.memory_file.write_text(content, encoding="utf-8")

    def check_limits(self, content: Optional[str] = None) -> Tuple[bool, int, int]:
        if content is None:
            content = self.read_index()
        lines = content.splitlines()
        line_count = len(lines)
        byte_count = len(content.encode("utf-8"))
        is_valid = line_count <= self.config.max_lines and byte_count <= self.config.max_bytes
        return is_valid, line_count, byte_count

    def list_topics(self) -> List[Path]:
        if not self.topics_dir.exists():
            return []
        return sorted(list(self.topics_dir.glob("*.md")))

    def check_pointers(self) -> List[Dict[str, Any]]:
        """Verify that all markdown links pointing to topics actually exist."""
        content = self.read_index()
        pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
        matches = pattern.findall(content)

        issues = []
        for text, link in matches:
            if link.startswith(self.config.topics_dir) or link.endswith(".md"):
                target = (self.root_dir / link).resolve()
                if not target.exists():
                    issues.append({"text": text, "link": link, "status": "missing"})
        return issues

    def prune_and_truncate(self, content: str) -> str:
        """If content exceeds max_lines, safely prune older trailing lines."""
        lines = content.splitlines()
        if len(lines) <= self.config.max_lines:
            return content

        truncated = lines[: self.config.max_lines - 4]
        truncated.append("\n<!-- Auto-pruned by dreamer: exceeded max line limit. Archive details to topics. -->")
        return "\n".join(truncated)
