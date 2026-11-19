"""Progress callbacks shared by the long-running operations."""

from __future__ import annotations

from collections.abc import Callable

EventCallback = Callable[[str], None]


def silent(_message: str) -> None:
    pass
