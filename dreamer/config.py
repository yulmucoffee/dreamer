import json
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


@dataclass
class MemoryConfig:
    dir: str = "memory"  # repo-relative; holds MEMORY.md (injected) + topic files
    # MEMORY.md is loaded into every session, so its budget is the main
    # recurring token cost. ~10KB is roughly 2.5k tokens.
    index_max_lines: int = 120
    index_max_bytes: int = 10 * 1024
    topic_max_bytes: int = 12 * 1024
    # Hard ceiling for the whole store so it cannot grow without bound over
    # months; the dreaming pass has to merge or prune to make room.
    store_max_bytes: int = 256 * 1024
    max_files: int = 60


@dataclass
class ModelConfig:
    model: str = "claude-opus-5-5"
    effort: str = "high"  # low | medium | high | xhigh | max
    max_tokens: int = 32000  # per turn
    max_turns: int = 24  # per dreaming pass; exceeding it aborts without changes


@dataclass
class DreamConfig:
    # Skip the API call entirely until at least this much new digested
    # transcript material has accumulated.
    min_new_chars: int = 3000
    # Upper bound on digested transcript text sent per pass (~15k tokens).
    # A larger backlog is processed over several passes, oldest first.
    batch_chars: int = 60000
    # After this many consecutive failed passes, automatic runs pause until a
    # manual `dreamer run`.
    max_failures: int = 3
    snapshots: int = 20


@dataclass
class TranscriptsConfig:
    # Claude Code config dir; empty means $CLAUDE_CONFIG_DIR or ~/.claude.
    claude_dir: str = ""


@dataclass
class DreamerConfig:
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    dream: DreamConfig = field(default_factory=DreamConfig)
    transcripts: TranscriptsConfig = field(default_factory=TranscriptsConfig)

    @classmethod
    def load(cls, root_dir: Path) -> "DreamerConfig":
        root = Path(root_dir)
        toml_path = root / "dreamer.toml"
        json_path = root / "dreamer.json"

        data: Dict[str, Any] = {}
        src = None
        if toml_path.exists():
            src = toml_path
            try:
                data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
            except Exception as e:
                _warn(f"failed to parse {toml_path} ({e}); using defaults.")
        elif json_path.exists():
            src = json_path
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
            except Exception as e:
                _warn(f"failed to parse {json_path} ({e}); using defaults.")

        cfg = cls()
        unknown = []
        for section, value in data.items():
            target = getattr(cfg, section, None) if section in _SECTIONS else None
            if target is None or not isinstance(value, dict):
                unknown.append(section)
                continue
            names = {f.name: f for f in fields(target)}
            for key, v in value.items():
                f = names.get(key)
                if f is None:
                    unknown.append(f"{section}.{key}")
                elif not isinstance(v, type(getattr(target, key))):
                    _warn(f"{src}: {section}.{key} should be {type(getattr(target, key)).__name__}; ignored.")
                else:
                    setattr(target, key, v)
        if unknown:
            _warn(f"{src}: ignoring unknown or removed config keys: {', '.join(sorted(unknown))}")
        return cfg


_SECTIONS = {"memory", "model", "dream", "transcripts"}


def _warn(msg: str) -> None:
    print(f"[dreamer] Warning: {msg}", file=sys.stderr)


EXAMPLE_TOML = """# dreamer.toml - all keys optional; defaults shown.

[memory]
dir = "memory"             # MEMORY.md (injected every session) + topic files
index_max_lines = 120
index_max_bytes = 10240    # recurring per-session token cost lives here
topic_max_bytes = 12288
store_max_bytes = 262144   # hard ceiling for the whole store
max_files = 60

[model]
model = "claude-opus-5-5"
effort = "high"            # low | medium | high | xhigh | max
max_tokens = 32000
max_turns = 24

[dream]
min_new_chars = 3000       # below this much new material, no API call is made
batch_chars = 60000        # max digested transcript text per pass
max_failures = 3           # auto runs pause after this many failures in a row
snapshots = 20

[transcripts]
claude_dir = ""            # default: $CLAUDE_CONFIG_DIR or ~/.claude
"""
