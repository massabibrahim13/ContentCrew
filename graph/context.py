"""
Runtime context: the services every node can use, passed by LangGraph alongside
the state.

State is data that is saved in checkpoints (the request, research, the draft).
Context is the live machinery a node needs to do its work (settings, database,
event emitter, language model). It is created fresh for every run and never
saved, so secrets and connections stay out of the checkpoint files.

A node receives it as `runtime.context`:

    def my_node(state: ContentCrewState, runtime: Runtime[WorkflowContext]):
        with runtime.context.emit.node(...) as step: ...
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from config import Settings
from llm.provider import LLMConfigurationError, describe_llm_error
from services.events import WorkflowEmitter
from storage.database import Database

log = logging.getLogger(__name__)


class StepFailed(Exception):
    """
    A workflow step can't continue. `message` is shown to the user as-is, so it
    must be plain language with no internals. `retryable` says whether running
    the same step again could work (e.g. after adding an API key).
    """

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.message = message
        self.retryable = retryable


@dataclass
class WorkflowContext:
    session_id: str
    settings: Settings
    db: Database
    emit: WorkflowEmitter
    llm_factory: Callable[[], Any]
    _llm: Any = field(default=None, repr=False)
    _llm_error: Optional[str] = field(default=None, repr=False)

    def optional_llm(self) -> Any:
        """The chat model, or None if it isn't configured (steps that can work without it)."""
        if self._llm is None and self._llm_error is None:
            try:
                self._llm = self.llm_factory()
            except LLMConfigurationError as exc:
                self._llm_error = str(exc)
        return self._llm

    def require_llm(self, who: str) -> Any:
        """The chat model, or a StepFailed explaining how to set it up."""
        llm = self.optional_llm()
        if llm is None:
            raise StepFailed(
                f"The {who} needs a language model, which isn't set up yet. "
                f"{self._llm_error} Restart the app after saving .env, then select Retry.",
                retryable=True,
            )
        return llm


def llm_step_failed(exc: Exception, who: str) -> StepFailed:
    """Turn a failed model call into a user-safe StepFailed."""
    log.warning("%s model call failed: %s", who, type(exc).__name__, exc_info=exc)
    return StepFailed(f"{who}: {describe_llm_error(exc)} Select Retry to try again.", retryable=True)
