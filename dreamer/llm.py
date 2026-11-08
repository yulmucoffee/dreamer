"""The dreaming agent loop: Claude edits the staged store through the memory
tool and can check claims against the raw transcripts."""

from typing import Any, Callable, Dict, List, Tuple

from dreamer.config import ModelConfig


class DreamError(RuntimeError):
    """The pass could not produce a trustworthy result; nothing is applied."""


SEARCH_TOOL = {
    "name": "search_transcripts",
    "description": (
        "Search the full, unabridged transcripts of past coding sessions in this repository "
        "(the digest you were given is clipped). Use it to check a claim before you record, "
        "change or delete a note. Returns matching excerpts, newest first."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Case-insensitive regex or literal text."},
            "max_results": {"type": "integer", "description": "1-20, default 8."},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
}

TOOLS = [{"type": "memory_20250818", "name": "memory"}, SEARCH_TOOL]
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def make_client():
    try:
        import anthropic
    except ImportError as e:
        raise DreamError("the 'anthropic' package is not installed") from e
    try:
        # Resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile.
        return anthropic.Anthropic()
    except Exception as e:
        raise DreamError(str(e)) from e


def run_loop(
    client: Any,
    cfg: ModelConfig,
    system: str,
    messages: List[Dict[str, Any]],
    handlers: Dict[str, Callable[[Dict[str, Any]], str]],
    turns: int,
    usage: Dict[str, int],
) -> Tuple[str, int]:
    """Run until the model stops calling tools. Returns (final text, turns used).
    `messages` is extended in place so the caller can continue the conversation."""
    for turn in range(1, turns + 1):
        try:
            with client.beta.messages.stream(
                model=cfg.model,
                max_tokens=cfg.max_tokens,
                output_config={"effort": cfg.effort},
                system=system,
                tools=TOOLS,
                messages=messages,
                # The system prompt, tools and growing history are re-sent every
                # turn; caching makes each later turn mostly cache reads.
                cache_control={"type": "ephemeral"},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            ) as stream:
                resp = stream.get_final_message()
        except DreamError:
            raise
        except Exception as e:
            raise DreamError(f"Claude API error: {e}") from e

        u = getattr(resp, "usage", None)
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            usage[k] = usage.get(k, 0) + int(getattr(u, k, 0) or 0)

        if resp.stop_reason == "max_tokens":
            raise DreamError(f"output truncated at max_tokens={cfg.max_tokens}")
        if resp.stop_reason == "refusal":
            raise DreamError("request was declined (stop_reason=refusal)")

        # Pass the full content back unchanged (thinking blocks included).
        messages.append({"role": "assistant", "content": resp.content})
        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        if not tool_uses:
            text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
            return text, turn

        results = []
        for tu in tool_uses:
            handler = handlers.get(tu.name)
            try:
                if handler is None:
                    raise ValueError(f"unknown tool {tu.name}")
                out, is_error = handler(dict(tu.input or {})), False
            except Exception as e:  # report to the model; it can retry differently
                out, is_error = f"Error: {e}", True
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out, "is_error": is_error})
        messages.append({"role": "user", "content": results})
    raise DreamError(f"pass did not finish within {turns} turns")
