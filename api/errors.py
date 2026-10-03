"""
Consistent JSON errors for the API.

Every API failure has the same shape, so the frontend can show a useful message:

    {"error": {"code": "validation_error",
               "message": "Check the highlighted fields.",
               "fields": {"company.name": "Enter the company name."},
               "retryable": false}}

Stack traces go to the server log only, never to the browser.
"""

from __future__ import annotations

import logging
from typing import Optional

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 fields: Optional[dict[str, str]] = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.fields = fields or {}
        self.retryable = retryable

    def to_response(self):
        body = {"error": {"code": self.code, "message": self.message, "retryable": self.retryable}}
        if self.fields:
            body["error"]["fields"] = self.fields
        return jsonify(body), self.status


class ValidationError(ApiError):
    def __init__(self, fields: dict[str, str], message: str = "Check the highlighted fields."):
        super().__init__(422, "validation_error", message, fields=fields)


HTTP_MESSAGES = {
    400: "The request couldn't be read.",
    404: "That endpoint doesn't exist.",
    405: "That method isn't allowed on this endpoint.",
    413: "The request is too large.",
    415: "Send the request body as JSON.",
}


def _is_api_request() -> bool:
    return request.path.startswith("/api/") or (request.path == "/blog" and request.method == "POST")


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(ApiError)
    def handle_api_error(err: ApiError):
        if err.status >= 500:
            log.error("API error %s on %s: %s", err.code, request.path, err.message)
        return err.to_response()

    @app.errorhandler(HTTPException)
    def handle_http_error(err: HTTPException):
        if not _is_api_request():
            return err
        message = HTTP_MESSAGES.get(err.code, err.description or "Request failed.")
        return ApiError(err.code or 500, err.name.lower().replace(" ", "_"), message).to_response()

    @app.errorhandler(Exception)
    def handle_unexpected(err: Exception):
        log.exception("Unhandled error on %s %s", request.method, request.path)
        if not _is_api_request():
            return "Something went wrong on the server. Details were written to the server log.", 500
        return ApiError(
            500, "server_error",
            "Something went wrong on the server. Details were written to the server log.",
            retryable=True,
        ).to_response()
