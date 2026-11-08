from pathlib import Path
from typing import List, Tuple

MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25 * 1024  # 25KB


class MemoryManager:
    def __init__(self, root_dir: Path):
        self.root_dir = Path(root_dir)
        self.memory_file = self.root_dir / "MEMORY.md"
        self.topics_dir = self.root_dir / "memory"

    def ensure_structure(self):
        self.topics_dir.mkdir(parents=True, exist_ok=True)
        if not self.memory_file.exists():
            default_template = (
                "# Project Memory Index\n\n"
                "<!-- Top 200 lines limit: High-level invariants, rules, and topic index only. -->\n\n"
                "## 1. Core Invariants\n"
                "- Build & Test commands, non-obvious runtime constraints.\n\n"
                "## 2. Verified Rules & Patterns\n"
                "- Tested failure modes and their permanent fixes.\n\n"
                "## 3. Topic Pointers\n"
                "- Details archived in `memory/<topic>.md`.\n"
            )
            self.memory_file.write_text(default_template, encoding="utf-8")

    def read_index(self) -> str:
        if not self.memory_file.exists():
            return ""
        return self.memory_file.read_text(encoding="utf-8")

    def write_index(self, content: str):
        self.memory_file.write_text(content, encoding="utf-8")

    def check_limits(self, content: str) -> Tuple[bool, int, int]:
        lines = content.splitlines()
        line_count = len(lines)
        byte_count = len(content.encode("utf-8"))
        is_valid = line_count <= MAX_INDEX_LINES and byte_count <= MAX_INDEX_BYTES
        return is_valid, line_count, byte_count

    def archive_topic(self, topic_name: str, content: str) -> Path:
        self.topics_dir.mkdir(parents=True, exist_ok=True)
        topic_path = self.topics_dir / f"{topic_name}.md"
        topic_path.write_text(content, encoding="utf-8")
        return topic_path
