"""Structured errors returned by every tool: {code, message, details}."""
from __future__ import annotations


class ToolError(Exception):
    def __init__(self, code: str, message: str, details: object | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        out = {"ok": False, "code": self.code, "message": self.message, "spoken_summary": self.message}
        if self.details is not None:
            out["details"] = self.details
        return out


# Codes used across servers
NOT_FOUND = "NOT_FOUND"
FORBIDDEN = "FORBIDDEN"
INVALID_INPUT = "INVALID_INPUT"
VALIDATION_FAILED = "VALIDATION_FAILED"
DB_RULE = "DB_RULE"            # a schema trigger/constraint refused the write
AMBIGUOUS = "AMBIGUOUS"
