"""
Input validation helpers.

Every value from the browser is validated and cleaned on the server, even if
the frontend already checked it: frontend checks are for convenience, these
are for correctness and safety.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from flask import request

from api.errors import ApiError, ValidationError

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def json_body() -> dict:
    """The request body as a dict, or a clear 4xx error."""
    if not request.is_json:
        raise ApiError(415, "unsupported_media_type",
                       "Send the request body as JSON (Content-Type: application/json).")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_json", "The request body must be a JSON object.")
    return data


class Validator:
    """Collects every field error so the user sees them all at once."""

    def __init__(self) -> None:
        self.errors: dict[str, str] = {}

    def fail(self, field: str, message: str) -> None:
        self.errors.setdefault(field, message)

    def raise_if_errors(self) -> None:
        if self.errors:
            raise ValidationError(self.errors)

    # -- field types ---------------------------------------------------------

    def text(self, value: Any, field: str, label: str, *, required: bool = False,
             min_len: int = 0, max_len: int = 500, multiline: bool = False) -> Optional[str]:
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                self.fail(field, f"Enter {label}.")
            return None
        if not isinstance(value, str):
            self.fail(field, f"{label.capitalize()} must be text.")
            return None

        cleaned = _CONTROL_CHARS.sub("", value).strip()
        if not multiline:
            cleaned = re.sub(r"\s+", " ", cleaned)
        else:
            cleaned = re.sub(r"\n{3,}", "\n\n", cleaned.replace("\r\n", "\n"))

        if len(cleaned) < min_len:
            self.fail(field, f"{label.capitalize()} needs at least {min_len} characters.")
            return None
        if len(cleaned) > max_len:
            self.fail(field, f"{label.capitalize()} can be at most {max_len} characters.")
            return None
        return cleaned

    def url(self, value: Any, field: str, *, required: bool = False) -> Optional[str]:
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                self.fail(field, "Enter a web address.")
            return None
        if not isinstance(value, str):
            self.fail(field, "Enter a web address, like example.com.")
            return None

        candidate = value.strip()
        if not _SCHEME.match(candidate):
            candidate = "https://" + candidate
        try:
            parsed = urlparse(candidate)
            host = parsed.hostname or ""
        except ValueError:
            host, parsed = "", None

        valid = (
            parsed is not None
            and parsed.scheme in {"http", "https"}
            and "." in host
            and not any(ch.isspace() for ch in candidate)
            and len(candidate) <= 300
        )
        if not valid:
            self.fail(field, "Enter a valid web address, like example.com.")
            return None
        return candidate.rstrip("/") if parsed.path in {"", "/"} else candidate

    def string_list(self, value: Any, field: str, label: str, *, min_items: int = 0,
                    max_items: int = 10, item_max: int = 80) -> list[str]:
        if value is None:
            value = []
        if not isinstance(value, list):
            self.fail(field, f"{label.capitalize()} must be a list.")
            return []

        items: list[str] = []
        seen: set[str] = set()
        for raw in value:
            if not isinstance(raw, str):
                self.fail(field, f"Each item in {label} must be text.")
                return []
            item = re.sub(r"\s+", " ", _CONTROL_CHARS.sub("", raw)).strip()
            if not item or item.lower() in seen:
                continue
            if len(item) > item_max:
                self.fail(field, f"Keep each item in {label} under {item_max} characters.")
                return []
            seen.add(item.lower())
            items.append(item)

        if len(items) < min_items:
            self.fail(field, f"Add at least {'one' if min_items == 1 else min_items} {label}.")
        elif len(items) > max_items:
            self.fail(field, f"{label.capitalize()} can have at most {max_items} items.")
        return items

    def choice(self, value: Any, field: str, choices: Iterable[str]) -> Optional[str]:
        allowed = list(choices)
        if value not in allowed:
            self.fail(field, f"Choose one of: {', '.join(allowed)}.")
            return None
        return value

    def identifier(self, value: Any, field: str, label: str) -> Optional[str]:
        """IDs we generate are 32 hex characters (uuid4().hex)."""
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
            self.fail(field, f"{label.capitalize()} is missing or invalid.")
            return None
        return value
