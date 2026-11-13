"""Builders of the wire dicts an app and an agent exchange about a member."""

from __future__ import annotations

import json
from typing import Any

from agents.member import PlaceWindow


def window_from(wire: dict[str, Any]) -> PlaceWindow:
    return PlaceWindow.model_validate_json(json.dumps(wire))


def entry(seq: int, **fields: Any) -> dict[str, Any]:
    return {"seq": seq, "kind": "said", "name": "ada", "text": "hello", "own": False} | fields


def talked_over_wire(**fields: Any) -> dict[str, Any]:
    return {
        "id": "t-1",
        "playing": True,
        "speech": [{"name": "ada", "text": "wait"}],
    } | fields


def talked_window_wire(**fields: Any) -> dict[str, Any]:
    return {
        "generation": "g",
        "starts_at": 1,
        "entries": [entry(1)],
        "talked_over": talked_over_wire(),
    } | fields


def written_wire(**fields: Any) -> dict[str, Any]:
    return {"written": True} | fields


def speak_wire(**fields: Any) -> dict[str, Any]:
    return {"place": {"kind": "room", "id": "r-1"}, "text": "Shall we start?"} | fields
