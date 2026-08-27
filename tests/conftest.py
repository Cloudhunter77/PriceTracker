from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def fixture_html():
    """Read a fixture page."""
    return _read


@pytest.fixture
def fixture_text():
    """Read a fixture that isn't HTML (an .ics feed, say)."""
    return _read


@pytest.fixture
def store(tmp_path):
    from pricetracker.store import Store

    return Store(
        history_path=tmp_path / "history.jsonl",
        state_path=tmp_path / "state.json",
        debug_dir=tmp_path / "debug",
    )


@pytest.fixture
def event_store(tmp_path):
    from pricetracker.events.store import EventStore

    return EventStore(events_path=tmp_path / "events.jsonl", seen_path=tmp_path / "seen.json")
