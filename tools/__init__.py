"""
Tools: the external capabilities agents can use.

A tool does one concrete thing (search the web, read a page, count keywords,
publish a post) and knows nothing about the workflow. Every tool returns a
`ToolResult` with one of four statuses, so nodes can handle every case the same
way and the UI can say exactly what happened:

    ok              the tool ran and returned data
    empty           the tool ran but found nothing
    not_configured  the tool needs credentials that aren't set; no data is invented
    error           the tool failed; `message` says why in plain words
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["ok", "empty", "not_configured", "error"]


@dataclass
class ToolResult:
    status: Status
    message: str
    data: Any = field(default=None)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @classmethod
    def success(cls, message: str, data: Any) -> "ToolResult":
        return cls("ok", message, data)

    @classmethod
    def empty(cls, message: str, data: Any = None) -> "ToolResult":
        return cls("empty", message, data)

    @classmethod
    def not_configured(cls, message: str) -> "ToolResult":
        return cls("not_configured", message, None)

    @classmethod
    def error(cls, message: str) -> "ToolResult":
        return cls("error", message, None)
