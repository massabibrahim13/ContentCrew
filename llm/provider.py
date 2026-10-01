"""
Language-model factory.

Agents ask for a model with `get_chat_model(settings)` instead of importing a
provider SDK themselves, so switching providers is a one-file change.
The provider package is imported lazily: the app starts (and Phase 2 works)
even before an API key is configured.
"""

from __future__ import annotations

from config import Settings


class LLMConfigurationError(RuntimeError):
    """The model can't be created because configuration is missing or invalid."""


SUPPORTED_PROVIDERS = {"groq", "openai"}


def get_chat_model(settings: Settings, temperature: float = 0.3, timeout: float = 90.0):
    """
    groq    free tier, no credit card (https://console.groq.com). The default.
    openai  paid; supported if you already have a key.
    """
    provider = settings.llm_provider
    if provider not in SUPPORTED_PROVIDERS:
        raise LLMConfigurationError(
            f"LLM_PROVIDER '{provider}' isn't supported. Use one of: {', '.join(sorted(SUPPORTED_PROVIDERS))}."
        )
    if not settings.llm_model:
        raise LLMConfigurationError("LLM_MODEL is empty. Set it in .env (see .env.example).")

    if provider == "groq":
        if not settings.groq_api_key:
            raise LLMConfigurationError(
                "GROQ_API_KEY is empty. Get a free key at console.groq.com and add it to .env."
            )
        try:
            from langchain_groq import ChatGroq
        except ImportError as exc:
            raise LLMConfigurationError(
                "The 'langchain-groq' package isn't installed. Run: pip install -r requirements.txt"
            ) from exc
        # The free tier allows a limited number of tokens per minute. Extra retries let the
        # SDK wait out a short rate limit (it honours the server's retry-after) instead of failing.
        return ChatGroq(model=settings.llm_model, api_key=settings.groq_api_key,
                        temperature=temperature, timeout=timeout, max_retries=6)

    if not settings.openai_api_key:
        raise LLMConfigurationError("OPENAI_API_KEY is empty. Set it in .env (see .env.example).")
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise LLMConfigurationError("The 'langchain-openai' package isn't installed.") from exc
    return ChatOpenAI(model=settings.llm_model, api_key=settings.openai_api_key,
                      temperature=temperature, timeout=timeout, max_retries=2)


def describe_llm_error(exc: Exception) -> str:
    """A plain-language explanation of a failed model call (no keys or raw responses)."""
    name = type(exc).__name__
    if isinstance(exc, LLMConfigurationError):
        return str(exc)
    if name == "AuthenticationError":
        return "The language model rejected the API key. Check the key in .env."
    if name == "PermissionDeniedError":
        return "The API key doesn't have access to that model. Check LLM_MODEL in .env."
    if name == "NotFoundError":
        return "The model in LLM_MODEL wasn't found. Check the model name in .env."
    if name == "RateLimitError":
        return "The free tier's rate limit was reached. Wait a minute, then try again."
    if name == "BadRequestError":
        return "The language model couldn't complete the request (often a formatting hiccup)."
    if name in {"InternalServerError", "ServiceUnavailableError"}:
        return "The language model service had a temporary problem."
    if name in {"APITimeoutError", "Timeout", "TimeoutError"}:
        return "The language model took too long to respond."
    if name in {"APIConnectionError", "ConnectionError"}:
        return "Couldn't reach the language model. Check the internet connection."
    if name in {"OutputParserException", "ValidationError"}:
        return "The language model's answer wasn't in the expected format."
    return "The language model request failed."
