from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pricetracker.alerts import (
    REASON_DROP,
    REASON_TARGET,
    apply_suspect_guard,
    best_reading,
    evaluate,
)
from pricetracker.config import Item, Source
from pricetracker.store import STATUS_SUSPECT, Reading

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)
URL = "https://shop.example.hu/camera"


def make_item(**kwargs) -> Item:
    defaults = dict(
        name="Camera",
        target_price=Decimal("1000"),
        currency="HUF",
        cooldown_days=3,
        drop_alert_pct=10.0,
        alert_on_out_of_stock=False,
        sources=[Source(url=URL, currency="HUF")],
    )
    defaults.update(kwargs)
    return Item(**defaults)


def make_reading(price, *, at=NOW, url=URL, availability="in_stock", **kwargs) -> Reading:
    return Reading(
        checked_at=at,
        item=kwargs.pop("item", "Camera"),
        url=url,
        shop=kwargs.pop("shop", "example.hu"),
        price=None if price is None else Decimal(str(price)),
        currency=kwargs.pop("currency", "HUF"),
        availability=availability,
        method="json-ld",
        **kwargs,
    )


# ---- picking the best price -------------------------------------------


def test_best_reading_picks_cheapest_across_shops():
    item = make_item()
    readings = [make_reading(1200, shop="a"), make_reading(950, shop="b"), make_reading(1100, shop="c")]
    assert best_reading(item, readings).shop == "b"


def test_out_of_stock_price_is_not_a_buying_signal():
    item = make_item()
    readings = [make_reading(500, shop="sold-out", availability="out_of_stock"), make_reading(1200, shop="b")]
    assert best_reading(item, readings).shop == "b"


def test_out_of_stock_counted_when_opted_in():
    item = make_item(alert_on_out_of_stock=True)
    readings = [make_reading(500, shop="sold-out", availability="out_of_stock")]
    assert best_reading(item, readings).shop == "sold-out"


def test_unknown_availability_is_still_usable():
    item = make_item()
    assert best_reading(item, [make_reading(900, availability=None)]) is not None


def test_wrong_currency_is_ignored():
    item = make_item()
    assert best_reading(item, [make_reading(900, currency="EUR")]) is None


# ---- threshold and drop rules -----------------------------------------


def test_alert_when_price_hits_target(store):
    alert, _ = evaluate(make_item(), [make_reading(999)], store, {}, NOW)
    assert alert is not None
    assert alert.reason == REASON_TARGET
    assert alert.best.price == Decimal("999")


def test_no_alert_above_target(store):
    alert, _ = evaluate(make_item(), [make_reading(1200)], store, {}, NOW)
    assert alert is None


def test_sharp_drop_alerts_even_above_target(store):
    """A 20% cut is worth knowing about even if the target isn't met yet."""
    for days_ago in range(7, 0, -1):
        store.append([make_reading(2000, at=NOW - timedelta(days=days_ago))])

    alert, _ = evaluate(make_item(), [make_reading(1600)], store, {}, NOW)
    assert alert is not None
    assert alert.reason == REASON_DROP
    assert alert.median == Decimal("2000")


def test_small_dip_does_not_alert(store):
    for days_ago in range(7, 0, -1):
        store.append([make_reading(2000, at=NOW - timedelta(days=days_ago))])

    alert, _ = evaluate(make_item(), [make_reading(1950)], store, {}, NOW)
    assert alert is None


def test_no_drop_alert_without_history(store):
    """Nothing to compare against on the first run, so only the target counts."""
    alert, _ = evaluate(make_item(), [make_reading(1600)], store, {}, NOW)
    assert alert is None


# ---- not repeating ourselves ------------------------------------------


def test_same_price_next_day_is_silent(store):
    item = make_item()
    alert, state = evaluate(item, [make_reading(999)], store, {}, NOW)
    assert alert is not None

    tomorrow = NOW + timedelta(days=1)
    alert, state = evaluate(item, [make_reading(999, at=tomorrow)], store, state, tomorrow)
    assert alert is None


def test_further_drop_alerts_immediately(store):
    item = make_item()
    _, state = evaluate(item, [make_reading(999)], store, {}, NOW)

    tomorrow = NOW + timedelta(days=1)
    alert, _ = evaluate(item, [make_reading(950, at=tomorrow)], store, state, tomorrow)
    assert alert is not None


def test_trivial_further_drop_stays_silent(store):
    """A 0.1% wobble is not news."""
    item = make_item()
    _, state = evaluate(item, [make_reading(999)], store, {}, NOW)

    tomorrow = NOW + timedelta(days=1)
    alert, _ = evaluate(item, [make_reading(998, at=tomorrow)], store, state, tomorrow)
    assert alert is None


def test_reminder_after_cooldown(store):
    item = make_item(cooldown_days=3)
    _, state = evaluate(item, [make_reading(999)], store, {}, NOW)

    later = NOW + timedelta(days=3)
    alert, _ = evaluate(item, [make_reading(999, at=later)], store, state, later)
    assert alert is not None


def test_recovery_resets_so_the_next_drop_gets_through(store):
    item = make_item()
    _, state = evaluate(item, [make_reading(999)], store, {}, NOW)
    assert "Camera" in state

    day2 = NOW + timedelta(days=1)
    _, state = evaluate(item, [make_reading(1500, at=day2)], store, state, day2)
    assert "Camera" not in state

    day3 = NOW + timedelta(days=2)
    alert, _ = evaluate(item, [make_reading(999, at=day3)], store, state, day3)
    assert alert is not None, "a price that recovered and dropped again must alert"


# ---- guarding against mis-parsed prices --------------------------------


def test_implausible_drop_is_held_back(store):
    store.append([make_reading(1_000_000, at=NOW - timedelta(days=1))])

    reading = apply_suspect_guard(make_reading(24_990), store)
    assert reading.status == STATUS_SUSPECT
    assert not reading.ok
    assert best_reading(make_item(target_price=Decimal("900000")), [reading]) is None


def test_suspect_price_accepted_once_confirmed(store):
    store.append([make_reading(1_000_000, at=NOW - timedelta(days=2))])

    first = apply_suspect_guard(make_reading(24_990, at=NOW - timedelta(days=1)), store)
    assert first.status == STATUS_SUSPECT
    store.append([first])

    second = apply_suspect_guard(make_reading(24_990), store)
    assert second.status == "ok", "a low price seen twice is real"


def test_ordinary_discount_is_not_suspect(store):
    store.append([make_reading(1000, at=NOW - timedelta(days=1))])
    assert apply_suspect_guard(make_reading(600), store).status == "ok"


def test_first_ever_reading_is_never_suspect(store):
    assert apply_suspect_guard(make_reading(10), store).status == "ok"


def test_suspect_guard_ignores_other_currency(store):
    store.append([make_reading(1_000_000, at=NOW - timedelta(days=1), currency="HUF")])
    assert apply_suspect_guard(make_reading(2000, currency="EUR"), store).status == "ok"


# ---- reporting fields --------------------------------------------------


def test_alert_reports_change_against_previous_run(store):
    store.append([make_reading(1200, at=NOW - timedelta(days=1))])

    alert, _ = evaluate(make_item(), [make_reading(900)], store, {}, NOW)
    assert alert.previous == Decimal("1200")
    assert alert.pct_change == pytest.approx(-25.0)
    assert alert.pct_below_target == pytest.approx(10.0)
