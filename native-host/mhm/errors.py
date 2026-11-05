"""Errors domain services."""

from __future__ import annotations


class HostError(Exception):
    def __init__(self, message: str, code: str = "INVALID_VALUE") -> None:
        super().__init__(message)
        self.code = code
