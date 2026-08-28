from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from pricetracker.config import (
    ConfigError,
    Source,
    Watchlist,
    append_item,
    load_watchlist,
)

SAMPLE = """
defaults:
  currency: HUF
  cooldown_days: 5
  drop_alert_pct: 15

items:
  - name: Sony A7 IV
    target_price: 850000
    sources:
      - url: https://www.alza.hu/sony-a7-iv
      - url: https://www.emag.hu/sony-a7-iv
        selector: .product-new-price

  - name: Travel tripod
    target_price: 299.99
    currency: EUR
    cooldown_days: 1
    sources:
      - url: https://example.de/tripod
"""


def write(tmp_path, text):
    path = tmp_path / "watchlist.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_flow_down_to_items_and_sources(tmp_path):
    config = load_watchlist(write(tmp_path, SAMPLE))
    camera, tripod = config.items

    assert camera.currency == "HUF"
    assert camera.cooldown_days == 5
    assert camera.drop_alert_pct == 15
    assert all(s.currency == "HUF" for s in camera.sources)

    # Per-item settings override the defaults.
    assert tripod.currency == "EUR"
    assert tripod.cooldown_days == 1
    assert tripod.drop_alert_pct == 15
    assert tripod.sources[0].currency == "EUR"


def test_shop_name_derived_from_url(tmp_path):
    config = load_watchlist(write(tmp_path, SAMPLE))
    assert [s.display_name for s in config.items[0].sources] == ["alza.hu", "emag.hu"]


def test_find_is_case_insensitive(tmp_path):
    config = load_watchlist(write(tmp_path, SAMPLE))
    assert config.find("sony a7 iv") is not None
    assert config.find("nope") is None


def test_missing_file_explains_itself(tmp_path):
    with pytest.raises(ConfigError, match="No watchlist"):
        load_watchlist(tmp_path / "nope.yaml")


@pytest.mark.parametrize(
    "bad",
    [
        "items:\n  - name: x\n    target_price: 10\n    sources: []\n",  # no sources
        "items:\n  - name: x\n    target_price: -5\n    sources:\n      - url: https://a.com\n",
        "items:\n  - name: x\n    target_price: 10\n    sources:\n      - url: ftp://a.com\n",
        "items:\n  - name: x\n    target_price: 10\n    currency: HUFF\n"
        "    sources:\n      - url: https://a.com\n",
        "items:\n  - name: x\n    target_price: 10\n    typo_field: 1\n"
        "    sources:\n      - url: https://a.com\n",
    ],
)
def test_invalid_configs_are_rejected(tmp_path, bad):
    with pytest.raises(ValidationError):
        load_watchlist(write(tmp_path, bad))


def test_disabled_items_are_skipped(tmp_path):
    config = load_watchlist(
        write(
            tmp_path,
            "items:\n"
            "  - name: off\n    target_price: 1\n    enabled: false\n"
            "    sources:\n      - url: https://a.com\n"
            "  - name: on\n    target_price: 1\n"
            "    sources:\n      - url: https://b.com\n",
        )
    )
    assert [i.name for i in config.active_items] == ["on"]


# ---- `pricetracker add` ------------------------------------------------


def test_add_creates_the_file(tmp_path):
    path = tmp_path / "watchlist.yaml"
    append_item(path, name="Camera", url="https://a.com/x", target_price=Decimal("1000"), currency="EUR")

    config = load_watchlist(path)
    assert config.items[0].name == "Camera"
    assert config.items[0].target_price == Decimal("1000")
    assert config.items[0].currency == "EUR"


def test_add_second_shop_to_existing_item(tmp_path):
    path = write(tmp_path, SAMPLE)
    append_item(path, name="sony a7 iv", url="https://foto.hu/a7iv", target_price=Decimal("850000"))

    config = load_watchlist(path)
    assert len(config.items) == 2, "must attach to the existing item, not create a duplicate"
    assert [s.url for s in config.items[0].sources][-1] == "https://foto.hu/a7iv"


def test_add_rejects_a_duplicate_url(tmp_path):
    path = write(tmp_path, SAMPLE)
    with pytest.raises(ConfigError, match="already tracked"):
        append_item(path, name="Sony A7 IV", url="https://www.alza.hu/sony-a7-iv", target_price=Decimal("1"))


def test_add_preserves_comments_and_existing_content(tmp_path):
    path = write(tmp_path, "# my shopping list\n" + SAMPLE)
    append_item(path, name="New thing", url="https://a.com/x", target_price=Decimal("50"))

    text = path.read_text(encoding="utf-8")
    assert "# my shopping list" in text, "hand-written comments must survive an edit"
    assert ".product-new-price" in text
    assert len(load_watchlist(path).items) == 3


def test_add_validates_before_writing(tmp_path):
    path = write(tmp_path, SAMPLE)
    original = path.read_text(encoding="utf-8")
    with pytest.raises((ValidationError, ValueError)):
        append_item(path, name="Bad", url="not-a-url", target_price=Decimal("10"))
    assert path.read_text(encoding="utf-8") == original, "a rejected edit must not touch the file"


def test_empty_watchlist_is_valid():
    assert Watchlist.model_validate({}).items == []


# ---- how a source is fetched and read ----------------------------------


def test_a_source_is_a_plain_http_product_page_by_default():
    source = Source(url="https://shop.example/x")
    assert source.render == "http"
    assert source.type == "product"
    assert source.needs_browser is False


def test_browser_rendering_is_opt_in_per_source():
    source = Source(url="https://www.arukereso.hu/p1", render="browser", type="aggregator")
    assert source.needs_browser is True
    assert source.type == "aggregator"


@pytest.mark.parametrize("field,value", [("render", "chrome"), ("type", "listing")])
def test_an_unknown_mode_is_rejected_at_load_time(field, value):
    """A typo here would otherwise fail silently at 8am, days later."""
    with pytest.raises(ValidationError, match=field):
        Source(url="https://shop.example/x", **{field: value})
