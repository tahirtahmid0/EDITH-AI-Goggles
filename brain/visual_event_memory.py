"""Read-only search helpers for EDITH's JSONL visual tracking history.

This module deliberately does not import or modify the SQLite memory system.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
EVENTS_PATH = BASE_DIR / "memory" / "visual_tracking_events.jsonl"


def load_events(path: Path = EVENTS_PATH) -> list[dict[str, Any]]:
    """Load valid event records in their original chronological order.

    Incomplete or malformed lines are skipped so a partially written final
    record cannot prevent EDITH from reading the rest of its visual history.
    """
    events: list[dict[str, Any]] = []
    event_path = Path(path)

    if not event_path.is_file():
        return events

    with event_path.open("r", encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and isinstance(event.get("event"), str):
                events.append(event)

    return events


def get_recent_events(
    limit: int = 10,
    path: Path = EVENTS_PATH,
) -> list[dict[str, Any]]:
    """Return the newest events first."""
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        return []

    if limit == 0:
        return []

    return list(reversed(load_events(path)[-limit:]))


def search_events(
    query: str = "",
    *,
    event_type: str | None = None,
    class_name: str | None = None,
    visual_identity_id: str | None = None,
    session_id: str | None = None,
    limit: int = 20,
    path: Path = EVENTS_PATH,
) -> list[dict[str, Any]]:
    """Search visual events by words and optional exact field filters.

    Text matching is case-insensitive and matches any query word. Exact
    filters are case-insensitive. Results are newest first.
    """
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        return []

    if limit == 0:
        return []

    keywords = [word.lower() for word in query.split() if word.strip()]
    filters = {
        "event": event_type,
        "class_name": class_name,
        "visual_identity_id": visual_identity_id,
        "session_id": session_id,
    }

    matches: list[dict[str, Any]] = []
    for event in reversed(load_events(path)):
        if any(
            expected is not None
            and str(event.get(field, "")).casefold() != str(expected).casefold()
            for field, expected in filters.items()
        ):
            continue

        searchable = json.dumps(event, ensure_ascii=False).casefold()
        if keywords and not any(word in searchable for word in keywords):
            continue

        matches.append(event)
        if len(matches) >= limit:
            break

    return matches
