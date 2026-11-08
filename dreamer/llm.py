from dreamer.config import ModelConfig


class DreamError(RuntimeError):
    """The dreaming pass could not produce a trustworthy result; nothing should be written."""


def complete(config: ModelConfig, system_prompt: str, user_prompt: str) -> str:
    """Run one consolidation request against Claude and return the text output.

    Raises DreamError on any failure (missing credentials, API error, refusal,
    truncated output) so callers never write a partial or fabricated index.
    """
    try:
        import anthropic
    except ImportError as e:
        raise DreamError("the 'anthropic' package is not installed") from e

    try:
        # Resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile.
        client = anthropic.Anthropic()
        # Stream: the input (index + trace) and output can both be long.
        with client.beta.messages.stream(
            model=config.model,
            max_tokens=config.max_tokens,
            output_config={"effort": config.effort},
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            # On a safety decline, let the API re-run the request on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            response = stream.get_final_message()
    except anthropic.APIError as e:
        raise DreamError(f"Claude API error: {e}") from e
    except Exception as e:  # e.g. no credentials configured
        raise DreamError(str(e)) from e

    if response.stop_reason == "max_tokens":
        raise DreamError(f"output truncated at max_tokens={config.max_tokens}")
    if response.stop_reason == "refusal":
        raise DreamError("request was declined (stop_reason=refusal)")

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise DreamError("model returned no text")
    return text
