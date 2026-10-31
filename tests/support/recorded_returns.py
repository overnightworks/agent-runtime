"""One owner for recording what a patched callable returns."""

from collections.abc import Callable

import pytest


def record_returns[T](monkeypatch: pytest.MonkeyPatch, target: object, name: str) -> list[T]:
    """Every value ``target.name`` returns from now on, in order; the real callable still runs."""
    recorded: list[T] = []
    real: Callable[[], T] = getattr(target, name)

    def recording() -> T:
        recorded.append(real())
        return recorded[-1]

    monkeypatch.setattr(target, name, recording)
    return recorded
