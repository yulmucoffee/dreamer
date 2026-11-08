import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


# Keys from the removed preset / multi-provider design. We warn instead of
# silently ignoring them so users with an old dreamer.toml know why their
# settings no longer apply.
_REMOVED_TOP_LEVEL = {"preset"}
_REMOVED_MODEL_KEYS = {"provider", "api_key_env", "base_url", "temperature"}


@dataclass
class MemoryConfig:
    index_file: str = "MEMORY.md"
    topics_dir: str = "memory"
    max_lines: int = 200
    max_bytes: int = 25 * 1024  # 25KB


@dataclass
class ModelConfig:
    # Dreaming quality depends heavily on model capability (salience judgment,
    # verifying notes against evidence), so default to the most capable model.
    model: str = "claude-opus-5-5"
    effort: str = "high"  # low | medium | high | xhigh | max
    max_tokens: int = 64000


@dataclass
class HookConfig:
    hook_type: str = "post-commit"  # post-commit | pre-push | none


@dataclass
class RulesConfig:
    focus_areas: List[str] = field(
        default_factory=lambda: [
            "core_invariants",
            "runtime_gotchas",
            "architectural_decisions",
            "verified_bugfix_rules",
        ]
    )


@dataclass
class DreamerConfig:
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    hook: HookConfig = field(default_factory=HookConfig)
    rules: RulesConfig = field(default_factory=RulesConfig)

    @classmethod
    def load(cls, root_dir: Path) -> "DreamerConfig":
        root = Path(root_dir)
        toml_path = root / "dreamer.toml"
        json_path = root / "dreamer.json"

        data: Dict[str, Any] = {}
        if toml_path.exists():
            try:
                data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[dreamer] Warning: failed to parse {toml_path} ({e}); using defaults.", file=sys.stderr)
        elif json_path.exists():
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[dreamer] Warning: failed to parse {json_path} ({e}); using defaults.", file=sys.stderr)

        cfg = cls()
        if not data:
            return cfg

        removed = sorted(_REMOVED_TOP_LEVEL & data.keys())
        removed += sorted(f"model.{k}" for k in _REMOVED_MODEL_KEYS & data.get("model", {}).keys())
        if removed:
            print(
                f"[dreamer] Warning: ignoring removed config keys: {', '.join(removed)} "
                "(presets and non-Anthropic providers are no longer supported).",
                file=sys.stderr,
            )

        m = data.get("memory", {})
        cfg.memory.index_file = m.get("index_file", cfg.memory.index_file)
        cfg.memory.topics_dir = m.get("topics_dir", cfg.memory.topics_dir)
        cfg.memory.max_lines = m.get("max_lines", cfg.memory.max_lines)
        cfg.memory.max_bytes = m.get("max_bytes", cfg.memory.max_bytes)

        mo = data.get("model", {})
        cfg.model.model = mo.get("model", cfg.model.model)
        cfg.model.effort = mo.get("effort", cfg.model.effort)
        cfg.model.max_tokens = mo.get("max_tokens", cfg.model.max_tokens)

        h = data.get("hook", {})
        cfg.hook.hook_type = h.get("hook_type", cfg.hook.hook_type)

        r = data.get("rules", {})
        cfg.rules.focus_areas = r.get("focus_areas", cfg.rules.focus_areas)

        return cfg
