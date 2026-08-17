from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_html():
    def _read(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return _read


@pytest.fixture
def store(tmp_path):
    from pricetracker.store import Store

    return Store(
        history_path=tmp_path / "history.jsonl",
        state_path=tmp_path / "state.json",
        debug_dir=tmp_path / "debug",
    )
