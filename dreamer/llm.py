import os
import json
import urllib.request
import urllib.error
from typing import Optional
from dreamer.config import ModelConfig


class BaseLLMProvider:
    def complete(self, system_prompt: str, user_prompt: str) -> str:
        raise NotImplementedError


class HeuristicProvider(BaseLLMProvider):
    """Fallback offline provider when no API key or LLM service is available."""

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        # Heuristic extraction: extract existing MEMORY.md block if present
        marker = "## Current MEMORY.md:\n```markdown\n"
        if marker in user_prompt:
            start = user_prompt.find(marker) + len(marker)
            end = user_prompt.find("```", start)
            if end != -1:
                return user_prompt[start:end].strip()
        return "# Project Memory Index\n\n- (Auto-maintained by dreamer offline mode)\n"


class AnthropicProvider(BaseLLMProvider):
    def __init__(self, config: ModelConfig):
        self.config = config

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        api_key = os.environ.get(self.config.api_key_env)
        if not api_key:
            raise ValueError(f"Environment variable '{self.config.api_key_env}' is not set.")

        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=self.config.model,
            max_tokens=4096,
            temperature=self.config.temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return resp.content[0].text.strip()


class OpenAICompatibleProvider(BaseLLMProvider):
    def __init__(self, config: ModelConfig):
        self.config = config

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        api_key = os.environ.get(self.config.api_key_env, "ollama")
        base_url = self.config.base_url or "https://api.openai.com/v1"
        url = f"{base_url.rstrip('/')}/chat/completions"

        payload = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=60) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            return res_data["choices"][0]["message"]["content"].strip()


def get_provider(config: ModelConfig) -> BaseLLMProvider:
    provider_name = config.provider.lower()

    if provider_name == "auto":
        if os.environ.get("ANTHROPIC_API_KEY"):
            return AnthropicProvider(config)
        elif os.environ.get("OPENAI_API_KEY"):
            config.api_key_env = "OPENAI_API_KEY"
            if "claude" in config.model:
                config.model = "gpt-4o-mini"
            return OpenAICompatibleProvider(config)
        else:
            return HeuristicProvider()

    elif provider_name == "anthropic":
        return AnthropicProvider(config)
    elif provider_name in ("openai", "openrouter", "groq"):
        return OpenAICompatibleProvider(config)
    elif provider_name == "ollama":
        if not config.base_url:
            config.base_url = "http://localhost:11434/v1"
        return OpenAICompatibleProvider(config)
    elif provider_name == "heuristic":
        return HeuristicProvider()
    else:
        return HeuristicProvider()
