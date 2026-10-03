"""
Structured answers from the model, robust to provider quirks.

The agents ask the model for typed answers (a Pydantic schema). Providers offer
different ways to get them, and models differ in which ones they follow:

    json_schema       the provider constrains the reply to the schema (Groq's gpt-oss models)
    function_calling  the schema is a tool the model must call (most models)
    plain text        we ask for JSON in the prompt and parse it ourselves (last resort)

`ask_structured()` tries them in a sensible order for the model in use and
falls through to the next one only when the reply had the wrong *format*
(e.g. Groq's "Tool choice is required, but model did not call a tool").
Other failures (bad key, rate limit, network) are raised at once: another
method wouldn't help, and the step reports them in plain words.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional, Sequence

from langchain_core.messages import BaseMessage, HumanMessage

log = logging.getLogger(__name__)

# Exceptions that mean "the reply wasn't in the requested shape", by class name, so
# this works whichever SDK raised it (groq, openai, langchain, pydantic).
FORMAT_ERRORS = {"BadRequestError", "OutputParserException", "ValidationError", "ValueError",
                 "JSONDecodeError"}
FORMAT_ERROR_HINTS = ("tool_use_failed", "json_validate_failed", "did not call a tool", "failed_generation",
                      "response_format", "json_schema", "schema")


def _is_format_error(exc: Exception) -> bool:
    name = type(exc).__name__
    if name not in FORMAT_ERRORS:
        return False
    if name == "BadRequestError":            # 400s also cover e.g. "context too long": only retry format ones
        text = str(exc).lower()
        return any(hint in text for hint in FORMAT_ERROR_HINTS)
    return True


def methods_for(llm: Any) -> list[Optional[str]]:
    """The order to try. None = the model wrapper's own default method."""
    kind = type(llm).__name__
    model = str(getattr(llm, "model_name", "") or "")
    if kind == "ChatGroq":
        if model.startswith("openai/gpt-oss"):
            return ["json_schema", "function_calling"]
        return ["function_calling", "json_schema"]
    return [None]


def _text_of(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, list):
        content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    return re.sub(r"<think>.*?</think>", "", str(content), flags=re.DOTALL).strip()


def _first_json_object(text: str) -> dict:
    """The first complete {...} in the text (models sometimes add a sentence or a code fence)."""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("The reply contained no JSON object.")


def _from_plain_text(llm: Any, schema, messages: Sequence[BaseMessage]):
    instruction = (
        "\n\nReply with only one JSON object, no other text, that matches this JSON schema:\n"
        + json.dumps(schema.model_json_schema())
    )
    *rest, last = list(messages)
    prompt = HumanMessage(f"{last.content}{instruction}")
    return schema.model_validate(_first_json_object(_text_of(llm.invoke([*rest, prompt]))))


def ask_structured(llm: Any, schema, messages: Sequence[BaseMessage]):
    """A validated `schema` instance from the model, or the error that explains why not."""
    first_error: Optional[Exception] = None
    for method in methods_for(llm):
        try:
            runnable = llm.with_structured_output(schema) if method is None \
                else llm.with_structured_output(schema, method=method)
            answer = runnable.invoke(list(messages))
            if answer is None:
                raise ValueError("The model returned an empty structured answer.")
            return answer
        except Exception as exc:
            if not _is_format_error(exc):
                raise
            log.info("Structured answer via %s failed for %s (%s); trying the next way",
                     method or "default", schema.__name__, type(exc).__name__)
            first_error = first_error or exc
    try:
        return _from_plain_text(llm, schema, messages)
    except Exception as exc:
        if not _is_format_error(exc):
            raise
        raise first_error or exc
