"""The web UI. Routes are exercised against real files in a temp directory."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pricetracker.store import Reading, Store
from pricetracker.web.app import app
from pricetracker.web.charts import best_per_run, line_chart, sparkline

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)

WATCHLIST = """\
# KEEP THIS COMMENT
defaults:
  currency: HUF
  cooldown_days: 3

items:
  - name: Sony A7 IV
    target_price: 1500000
    sources:
      - url: https://alza.hu/a7   # the good one
"""

EVENTS = """\
home:
  label: Budapest
  lat: 47.4979
  lon: 19.0402
  radius_km: 15
window_days: 30
geocode: false
interests:
  - name: Live music
    keywords: [koncert]
sources:
  - name: port.hu
    type: schemaorg
    url: https://port.hu/programok
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A client working against a throwaway project directory."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "watchlist.yaml").write_text(WATCHLIST, encoding="utf-8")
    (tmp_path / "events.yaml").write_text(EVENTS, encoding="utf-8")
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def with_history(tmp_path):
    """A fortnight of two-shop price history."""
    store = Store(
        history_path=tmp_path / "data" / "history.jsonl",
        state_path=tmp_path / "data" / "alert_state.json",
    )
    rows = []
    for day in range(10, 0, -1):
        for shop, url, base in (
            ("alza.hu", "https://alza.hu/a7", 1_400_000),
            ("emag.hu", "https://emag.hu/a7", 1_460_000),
        ):
            rows.append(
                Reading(
                    checked_at=NOW - timedelta(days=day),
                    item="Sony A7 IV",
                    url=url,
                    shop=shop,
                    price=Decimal(base - day * 9000),
                    currency="HUF",
                    availability="in_stock",
                    method="json-ld",
                )
            )
    store.append(rows)
    return rows


def location(response) -> str:
    return response.headers.get("location", "")


# ---- pages render ------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/items/new", "/events", "/settings"])
def test_pages_render(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "Price Tracker" in response.text


def test_dashboard_lists_the_watchlist(client):
    assert "Sony A7 IV" in client.get("/").text


def test_dashboard_survives_a_missing_watchlist(tmp_path, monkeypatch):
    """An empty project directory must show an empty state, not a 500."""
    monkeypatch.chdir(tmp_path)
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "Add the first item" in response.text


def test_item_detail_shows_history(client, with_history):
    response = client.get("/items/Sony A7 IV", follow_redirects=True)
    assert response.status_code == 200
    assert "<svg" in response.text
    assert "alza.hu" in response.text


def test_unknown_item_redirects_home(client):
    response = client.get("/items/Nope")
    assert response.status_code == 303
    assert location(response).startswith("/?")


# ---- editing -----------------------------------------------------------


def test_add_item(client, tmp_path):
    response = client.post(
        "/items",
        data={"name": "Tripod", "url": "https://example.hu/tripod", "target": "199000",
              "currency": "HUF", "selector": ""},
    )
    assert response.status_code == 303
    text = (tmp_path / "watchlist.yaml").read_text(encoding="utf-8")
    assert "Tripod" in text
    assert "# KEEP THIS COMMENT" in text, "hand-written comments must survive"


def test_add_item_rejects_a_bad_price(client, tmp_path):
    response = client.post(
        "/items",
        data={"name": "X", "url": "https://example.hu/x", "target": "cheap", "currency": "", "selector": ""},
    )
    assert "error=" in location(response)
    assert "X" not in (tmp_path / "watchlist.yaml").read_text(encoding="utf-8")


def test_update_item(client, tmp_path):
    client.post(
        "/items/Sony A7 IV/update",
        data={"target": "1250000", "currency": "HUF", "cooldown_days": "7",
              "drop_alert_pct": "15", "enabled": "on"},
    )
    text = (tmp_path / "watchlist.yaml").read_text(encoding="utf-8")
    assert "1250000" in text
    assert "cooldown_days: 7" in text
    assert "# the good one" in text, "inline comments must survive too"


def test_clearing_a_field_restores_the_default(client, tmp_path):
    """A blank override should fall back to defaults, not store an empty value."""
    client.post(
        "/items/Sony A7 IV/update",
        data={"target": "1500000", "currency": "HUF", "cooldown_days": "", "drop_alert_pct": "", "enabled": "on"},
    )
    assert "cooldown_days:" not in (tmp_path / "watchlist.yaml").read_text(encoding="utf-8").split("items:")[1]


def test_a_rejected_edit_leaves_the_file_untouched(client, tmp_path):
    before = (tmp_path / "watchlist.yaml").read_text(encoding="utf-8")
    response = client.post(
        "/items/Sony A7 IV/update",
        data={"target": "nonsense", "currency": "HUF", "cooldown_days": "", "drop_alert_pct": "", "enabled": "on"},
    )
    assert "error=" in location(response)
    assert (tmp_path / "watchlist.yaml").read_text(encoding="utf-8") == before


def test_add_and_remove_a_shop(client, tmp_path):
    client.post("/items/Sony A7 IV/sources", data={"url": "https://emag.hu/a7", "selector": ".p"})
    assert "emag.hu" in (tmp_path / "watchlist.yaml").read_text(encoding="utf-8")

    client.post("/items/Sony A7 IV/sources/remove", data={"url": "https://emag.hu/a7"})
    assert "emag.hu" not in (tmp_path / "watchlist.yaml").read_text(encoding="utf-8")


def test_duplicate_shop_is_refused(client):
    response = client.post("/items/Sony A7 IV/sources", data={"url": "https://alza.hu/a7", "selector": ""})
    assert "already" in location(response)


def test_the_last_shop_cannot_be_removed(client, tmp_path):
    """An item with no shops is invalid; deleting the item is the real intent."""
    response = client.post("/items/Sony A7 IV/sources/remove", data={"url": "https://alza.hu/a7"})
    assert "error=" in location(response)
    assert "alza.hu" in (tmp_path / "watchlist.yaml").read_text(encoding="utf-8")


def test_delete_item(client, tmp_path):
    response = client.post("/items/Sony A7 IV/delete")
    assert response.status_code == 303
    assert "Sony A7 IV" not in (tmp_path / "watchlist.yaml").read_text(encoding="utf-8")


def test_update_settings(client, tmp_path):
    client.post(
        "/settings",
        data={"currency": "EUR", "cooldown_days": "5", "drop_alert_pct": "20", "alert_on_out_of_stock": "on"},
    )
    text = (tmp_path / "watchlist.yaml").read_text(encoding="utf-8")
    assert "currency: EUR" in text
    assert "alert_on_out_of_stock: true" in text


# ---- events ------------------------------------------------------------


def test_events_page_shows_configured_radius(client):
    assert "15 km" in client.get("/events").text


def test_add_and_remove_an_event_source(client, tmp_path):
    client.post("/events/sources", data={"name": "A38", "url": "https://a38.hu/programok", "kind": "schemaorg"})
    assert "A38" in (tmp_path / "events.yaml").read_text(encoding="utf-8")

    client.post("/events/sources/remove", data={"name": "A38"})
    assert "A38" not in (tmp_path / "events.yaml").read_text(encoding="utf-8")


def test_duplicate_event_source_name_is_refused(client):
    response = client.post("/events/sources", data={"name": "port.hu", "url": "https://x.hu", "kind": "schemaorg"})
    assert "already%20exists" in location(response)


def test_event_settings_update(client, tmp_path):
    client.post(
        "/events/settings",
        data={"lat": "47.5", "lon": "19.1", "radius_km": "25", "window_days": "14"},
    )
    text = (tmp_path / "events.yaml").read_text(encoding="utf-8")
    assert "radius_km: 25" in text
    assert "window_days: 14" in text


def test_invalid_event_settings_are_refused(client, tmp_path):
    before = (tmp_path / "events.yaml").read_text(encoding="utf-8")
    response = client.post(
        "/events/settings", data={"lat": "999", "lon": "19.1", "radius_km": "25", "window_days": "14"}
    )
    assert "error=" in location(response)
    assert (tmp_path / "events.yaml").read_text(encoding="utf-8") == before


def test_events_page_without_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    response = TestClient(app).get("/events")
    assert response.status_code == 200
    assert "events.yaml" in response.text


# ---- probing -----------------------------------------------------------


def test_probe_rejects_a_non_url(client):
    response = client.post("/items/test", data={"url": "not a url", "selector": ""})
    assert "full http(s) URL" in response.text


def test_event_probe_rejects_an_unknown_type(client):
    response = client.post("/events/sources/test", data={"url": "https://x.hu", "kind": "magic"})
    assert "Unknown source type" in response.text


# ---- background jobs ---------------------------------------------------


def test_unknown_job_does_not_error(client):
    response = client.get("/jobs/deadbeef")
    assert response.status_code == 200
    assert "no longer available" in response.text


# ---- charts ------------------------------------------------------------


def test_sparkline_tracks_the_best_price_not_each_shop(with_history):
    """Mixing two shops into one line would show a zig-zag that never happened."""
    best = best_per_run(with_history)
    assert len(best) == 10
    assert all(price == min(p for _, p in best if _ == when) for when, price in best)
    # The cheaper shop is alza; every point should come from it.
    assert all(price < Decimal("1_460_000") for _, price in best)


def test_sparkline_needs_two_points():
    assert sparkline([]) == ""


def test_chart_series_and_legend_agree(with_history):
    chart = line_chart(with_history, "HUF", Decimal("1300000"))
    assert [s.label for s in chart.series] == ["alza.hu", "emag.hu"]
    assert [s.slot for s in chart.series] == [1, 2]
    for series in chart.series:
        assert f"var(--series-{series.slot})" in chart.svg


def test_chart_without_history_says_so():
    chart = line_chart([], "HUF")
    assert chart.series == []
    assert "No price history" in chart.svg


def test_many_shops_fold_into_other(tmp_path):
    """A ninth generated hue is never distinguishable, so the tail is grouped."""
    rows = [
        Reading(checked_at=NOW - timedelta(days=d), item="x", url=f"https://s{i}.hu",
                shop=f"shop{i}.hu", price=Decimal(1000 + i), currency="HUF")
        for i in range(6)
        for d in (2, 1)
    ]
    chart = line_chart(rows, "HUF")
    assert len(chart.series) == 4
    assert chart.series[-1].label == "Other shops"
