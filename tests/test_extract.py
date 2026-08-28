from __future__ import annotations

from decimal import Decimal

import pytest

from pricetracker.extract import (
    ExtractionError,
    _normalise_availability,
    _parse_price,
    extract_all,
    extract_offers,
    extract_price,
)


def test_json_ld_offer(fixture_html):
    result = extract_price(fixture_html("jsonld_product.html"))
    assert result.method == "json-ld"
    assert result.price == Decimal("1299900")
    assert result.currency == "HUF"
    assert result.in_stock is True


def test_json_ld_inside_graph_with_list_of_offers(fixture_html):
    result = extract_price(fixture_html("jsonld_graph.html"))
    assert result.method == "json-ld"
    assert result.price == Decimal("2499.00")
    assert result.currency == "EUR"
    assert result.in_stock is False


def test_microdata(fixture_html):
    result = extract_price(fixture_html("microdata_product.html"))
    assert result.method == "microdata"
    assert result.price == Decimal("2499.99")
    assert result.currency == "USD"
    assert result.in_stock is True


def test_opengraph(fixture_html):
    result = extract_price(fixture_html("opengraph_product.html"))
    assert result.method == "opengraph"
    assert result.price == Decimal("1699.00")
    assert result.currency == "GBP"
    assert result.in_stock is True


def test_selector_fallback_for_shops_without_metadata(fixture_html):
    html = fixture_html("selector_only.html")
    with pytest.raises(ExtractionError):
        extract_price(html)

    result = extract_price(html, ".termek-ar .ar-vegleges")
    assert result.method == "selector"
    assert result.price == Decimal("389900")
    assert result.currency == "HUF"


def test_selector_wins_over_metadata(fixture_html):
    """An explicit selector is a deliberate override and must take priority."""
    result = extract_price(fixture_html("jsonld_product.html"), ".accessory")
    assert result.method == "selector"
    assert result.price == Decimal("24990")


def test_xpath_selector_supported(fixture_html):
    result = extract_price(fixture_html("selector_only.html"), '//span[@class="ar-vegleges"]')
    assert result.price == Decimal("389900")


def test_extract_all_reports_every_method(fixture_html):
    results = extract_all(fixture_html("microdata_product.html"))
    assert set(results) == {"microdata"}

    results = extract_all(fixture_html("microdata_product.html"), ".visible-price")
    assert set(results) == {"selector", "microdata"}


def test_page_without_price_raises(fixture_html):
    with pytest.raises(ExtractionError):
        extract_price(fixture_html("no_price.html"))


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1 299 900 Ft", Decimal("1299900")),
        ("1.299.900 Ft", Decimal("1299900")),
        ("$1,299.00", Decimal("1299.00")),
        ("1 299,00 €", Decimal("1299.00")),
        ("389.900 Ft", Decimal("389900")),
        ("2 499,99 zł", Decimal("2499.99")),
    ],
)
def test_display_price_strings(text, expected):
    assert _parse_price(text) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        # Structured metadata is a plain number and must parse exactly, not by
        # guessing whether "1299.00" means 1299 or 129900.
        ("1299.00", Decimal("1299.00")),
        ("1299", Decimal("1299")),
        (2499.5, Decimal("2499.5")),
        (1299, Decimal("1299")),
        ("0", None),
        ("", None),
        (None, None),
        ("out of stock", None),
    ],
)
def test_metadata_values(value, expected):
    assert _parse_price(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://schema.org/InStock", "in_stock"),
        ("InStock", "in_stock"),
        ("instock", "in_stock"),
        ("http://schema.org/LimitedAvailability", "in_stock"),
        ("https://schema.org/OutOfStock", "out_of_stock"),
        ("PreOrder", "out_of_stock"),
        ("BackOrder", "out_of_stock"),
        (None, None),
        ("", None),
    ],
)
def test_availability_normalisation(value, expected):
    assert _normalise_availability(value) == expected


def test_broken_json_ld_does_not_break_other_methods():
    html = """<html><head>
      <script type="application/ld+json">{ this is not json </script>
      <meta property="og:price:amount" content="99.90">
      <meta property="og:price:currency" content="EUR">
    </head><body></body></html>"""
    result = extract_price(html)
    assert result.method == "opengraph"
    assert result.price == Decimal("99.90")


def test_marketplace_page_quotes_the_cheapest_seller():
    """eMAG-style pages list every seller as its own Offer; taking the first
    would quote a price you could have beaten on the same page."""
    html = """<html><script type="application/ld+json">
    {"@type": "Product", "name": "Sony A7 IV",
     "offers": [
       {"@type": "Offer", "price": 870641, "priceCurrency": "HUF"},
       {"@type": "Offer", "price": 849900, "priceCurrency": "HUF"},
       {"@type": "Offer", "price": 899000, "priceCurrency": "HUF"}
     ]}
    </script></html>"""
    result = extract_price(html)
    assert result.price == Decimal("849900")
    assert result.currency == "HUF"


def test_product_offer_beats_a_cheaper_unrelated_offer():
    """A cheap bare Offer is usually an accessory, so the Product's own offer
    wins even when it costs more."""
    html = """<html><script type="application/ld+json">
    [{"@type": "Product", "name": "Camera",
      "offers": {"@type": "Offer", "price": 748999, "priceCurrency": "HUF"}},
     {"@type": "Offer", "price": 4990, "priceCurrency": "HUF"}]
    </script></html>"""
    assert extract_price(html).price == Decimal("748999")


# ---- price-comparison pages --------------------------------------------

from pathlib import Path  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_every_named_shop_is_read_cheapest_first():
    offers = extract_offers(fixture("aggregator_arukereso.html"))
    assert [o.seller for o in offers] == ["Tripont   Foto", "Fotoplus", "eMAG"]
    assert [o.price for o in offers] == [Decimal("513120"), Decimal("526000"), Decimal("579900")]


def test_an_offer_with_no_seller_is_left_out():
    """An unattributable price is not a shop. Reporting it as one would invite a
    click on a listing that names nowhere to buy it."""
    offers = extract_offers(fixture("aggregator_arukereso.html"))
    assert Decimal("540000") not in [o.price for o in offers]


def test_stock_and_links_come_through_when_the_page_gives_them():
    offers = {o.seller: o for o in extract_offers(fixture("aggregator_arukereso.html"))}
    assert offers["eMAG"].availability == "out_of_stock"
    assert offers["Fotoplus"].url == "https://www.fotoplus.hu/sony-a6700-vaz"
    assert offers["eMAG"].url is None


def test_a_page_without_seller_markup_yields_no_offers():
    assert extract_offers(fixture("aggregator_no_sellers.html")) == []


def test_the_market_low_is_still_readable_from_an_aggregate_offer():
    """`lowPrice` is the "513 120 Ft-tól" figure — the only number a comparison
    page without seller markup actually promises."""
    found = extract_price(fixture("aggregator_no_sellers.html"))
    assert found.price == Decimal("513120")
    assert found.currency == "HUF"


def test_an_ordinary_product_page_lists_no_sellers():
    """extract_offers must not mistake a shop's own single offer for a market."""
    assert extract_offers(fixture("jsonld_product.html")) == []
