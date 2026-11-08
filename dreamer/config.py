import os
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Any

try:
    import tomllib  # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None


@dataclass
class MemoryConfig:
    index_file: str = "MEMORY.md"
    topics_dir: str = "memory"
    max_lines: int = 200
    max_bytes: int = 25 * 1024  # 25KB
    archive_threshold: int = 180  # Start archiving when approaching this line count


@dataclass
class ModelConfig:
    provider: str = "auto"  # auto | anthropic | openai | ollama | heuristic
    model: str = "claude-3-5-haiku-20241022"
    api_key_env: str = "ANTHROPIC_API_KEY"
    base_url: Optional[str] = None
    temperature: float = 0.2


@dataclass
class HookConfig:
    hook_type: str = "post-commit"  # post-commit | pre-push | none
    run_background: bool = True
    quiet: bool = True


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
    ignore_files: List[str] = field(
        default_factory=lambda: [
            "*.lock",
            "package-lock.json",
            "pnpm-lock.yaml",
            "node_modules/**",
            "dist/**",
            "build/**",
            ".git/**",
        ]
    )


@dataclass
class DreamerConfig:
    preset: str = "vibe"  # vibe | strict | local | custom
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    hook: HookConfig = field(default_factory=HookConfig)
    rules: RulesConfig = field(default_factory=RulesConfig)

    @classmethod
    def from_preset(cls, preset_name: str) -> "DreamerConfig":
        cfg = cls(preset=preset_name)
        if preset_name == "vibe":
            # Maximum automation, silent background, auto model/heuristic fallback
            cfg.model.provider = "auto"
            cfg.hook.hook_type = "post-commit"
            cfg.hook.run_background = True
            cfg.hook.quiet = True
            cfg.memory.max_lines = 200
        elif preset_name == "strict":
            # Hard adherence, strict warnings
            cfg.model.provider = "anthropic"
            cfg.model.model = "claude-3-5-sonnet-20241022"
            cfg.hook.hook_type = "post-commit"
            cfg.hook.run_background = False
            cfg.hook.quiet = False
            cfg.memory.max_lines = 200
        elif preset_name == "local":
            # 100% offline via Ollama or heuristic
            cfg.model.provider = "ollama"
            cfg.model.model = "llama3.1"
            cfg.model.base_url = "http://localhost:11434/v1"
            cfg.hook.hook_type = "post-commit"
            cfg.hook.run_background = True
        return cfg

    @classmethod
    def load(cls, root_dir: Path) -> "DreamerConfig":
        root = Path(root_dir)
        toml_path = root / "dreamer.toml"
        json_path = root / "dreamer.json"

        data: Dict[str, Any] = {}
        if toml_path.exists() and tomllib:
            try:
                data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        elif json_path.exists():
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        if not data:
            return cls.from_preset("vibe")

        preset = data.get("preset", "custom")
        cfg = cls.from_preset(preset)

        if "memory" in data:
            m = data["memory"]
            cfg.memory.index_file = m.get("index_file", cfg.memory.index_file)
            cfg.memory.topics_dir = m.get("topics_dir", cfg.memory.topics_dir)
            cfg.memory.max_lines = m.get("max_lines", cfg.memory.max_lines)
            cfg.memory.max_bytes = m.get("max_bytes", cfg.memory.max_bytes)
            cfg.memory.archive_threshold = m.get("archive_threshold", cfg.memory.archive_threshold)

        if "model" in data:
            mo = data["model"]
            cfg.model.provider = mo.get("provider", cfg.model.provider)
            cfg.model.model = mo.get("model", cfg.model.model)
            cfg.model.api_key_env = mo.get("api_key_env", cfg.model.api_key_env)
            cfg.model.base_url = mo.get("base_url", cfg.model.base_url)
            cfg.model.temperature = mo.get("temperature", cfg.model.temperature)

        if "hook" in data:
            h = data["hook"]
            cfg.hook.hook_type = h.get("hook_type", cfg.hook.hook_type)
            cfg.hook.run_background = h.get("run_background", cfg.hook.run_background)
            cfg.hook.quiet = h.get("quiet", cfg.hook.quiet)

        if "rules" in data:
            r = data["rules"]
            cfg.rules.focus_areas = r.get("focus_areas", cfg.rules.focus_areas)
            cfg.rules.ignore_files = r.get("ignore_files", cfg.rules.ignore_files)

        return cfg
