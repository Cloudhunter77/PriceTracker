"""Pull a price out of a product page.

The strategy is to lean on the structured product metadata that shops already
publish for Google Shopping, rather than writing a scraper per site. Methods are
tried in order of trustworthiness and the first hit wins:

    1. selector   - an explicit CSS/XPath override from the watchlist
    2. json-ld    - schema.org Product/Offer in a <script type="ld+json"> block
    3. microdata  - itemprop="price" markup
    4. opengraph  - og:price:amount / product:price:amount meta tags

Whichever method produced a price is recorded alongside it, so it is obvious
when a source is relying on a fragile hand-written selector.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator

import extruct
from parsel import Selector
from price_parser import Price

log = logging.getLogger(__name__)

METHODS = ("selector", "json-ld", "microdata", "opengraph")

IN_STOCK = "in_stock"
OUT_OF_STOCK = "out_of_stock"

# schema.org availability values that mean "you can buy it right now".
_IN_STOCK_TOKENS = {"instock", "instoreonly", "onlineonly", "limitedavailability"}

# A plain machine-readable number, as structured metadata is supposed to use.
_STRICT_NUMBER = re.compile(r"^\s*\d+(?:\.\d+)?\s*$")

_SYMBOL_TO_ISO = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "ft": "HUF",
    "huf": "HUF",
    "kč": "CZK",
    "zł": "PLN",
    "lei": "RON",
    "chf": "CHF",
    "¥": "JPY",
}


class ExtractionError(Exception):
    """No price could be found on the page."""


@dataclass(slots=True)
class Extraction:
    """A price found on a page, and how it was found."""

    price: Decimal
    method: str
    raw: str
    currency: str | None = None
    availability: str | None = None

    @property
    def in_stock(self) -> bool | None:
        """True/False when the page said so, None when it didn't say."""
        if self.availability is None:
            return None
        return self.availability == IN_STOCK


def extract_price(html: str, selector: str | None = None) -> Extraction:
    """Return the first price found, trying each method in order.

    Raises ExtractionError when every method comes up empty.
    """
    results = extract_all(html, selector)
    for method in METHODS:
        if method in results:
            return results[method]
    raise ExtractionError(
        "no price found (no JSON-LD, microdata or Open Graph price on the page). "
        "Run `pricetracker test-url <url>` and add a `selector:` for this source."
    )


def extract_all(html: str, selector: str | None = None) -> dict[str, Extraction]:
    """Run every method and return whatever each one found.

    Used by `test-url` to show which methods work for a shop, and by
    extract_price to pick the best one.
    """
    sel = Selector(text=html)
    found: dict[str, Extraction] = {}

    for method, extractor in (
        ("selector", lambda: _from_selector(sel, selector)),
        ("json-ld", lambda: _from_json_ld(html)),
        ("microdata", lambda: _from_microdata(html)),
        ("opengraph", lambda: _from_opengraph(sel)),
    ):
        try:
            result = extractor()
        except Exception as exc:  # a broken page must not kill the whole run
            log.debug("%s extraction failed: %s", method, exc)
            continue
        if result is not None:
            found[method] = result

    availability = _page_availability(html, sel)
    for result in found.values():
        if result.availability is None:
            result.availability = availability
    return found


def _from_selector(sel: Selector, selector: str | None) -> Extraction | None:
    if not selector:
        return None
    query = sel.xpath(selector) if selector.startswith(("/", "(")) else sel.css(selector)
    for node in query:
        # Prefer a machine-readable content/value attribute over display text.
        for attr in ("content", "data-price", "value"):
            raw = node.attrib.get(attr)
            price = _parse_price(raw) if raw else None
            if price is not None:
                return Extraction(price=price, method="selector", raw=raw, currency=_currency_of(raw))
        text = " ".join(node.css("::text").getall()).strip()
        price = _parse_price(text)
        if price is not None:
            return Extraction(price=price, method="selector", raw=text, currency=_currency_of(text))
    return None


def _from_json_ld(html: str) -> Extraction | None:
    data = extruct.extract(html, syntaxes=["json-ld"], errors="ignore").get("json-ld", [])
    nodes = list(_walk(data))

    # A Product's own offer is the most trustworthy thing on the page; a bare
    # Offer node elsewhere may belong to a related/accessory product.
    offers = [
        offer
        for node in nodes
        if _has_type(node, "Product")
        for offer in _walk(node.get("offers"))
    ]
    offers += [node for node in nodes if _has_type(node, "Offer", "AggregateOffer")]

    for offer in offers:
        for key in ("price", "lowPrice"):
            price = _parse_price(offer.get(key))
            if price is not None:
                return Extraction(
                    price=price,
                    method="json-ld",
                    raw=str(offer.get(key)),
                    currency=_normalise_currency(offer.get("priceCurrency")),
                    availability=_normalise_availability(offer.get("availability")),
                )
    return None


def _from_microdata(html: str) -> Extraction | None:
    data = extruct.extract(html, syntaxes=["microdata"], errors="ignore").get("microdata", [])
    for node in _walk(data):
        props = node.get("properties")
        if not isinstance(props, dict):
            continue
        for key in ("price", "lowPrice"):
            price = _parse_price(props.get(key))
            if price is not None:
                return Extraction(
                    price=price,
                    method="microdata",
                    raw=str(props.get(key)),
                    currency=_normalise_currency(props.get("priceCurrency")),
                    availability=_normalise_availability(props.get("availability")),
                )
    return None


def _from_opengraph(sel: Selector) -> Extraction | None:
    amount = _meta(sel, "og:price:amount", "product:price:amount")
    if amount is None:
        return None
    price = _parse_price(amount)
    if price is None:
        return None
    currency = _meta(sel, "og:price:currency", "product:price:currency")
    return Extraction(
        price=price,
        method="opengraph",
        raw=amount,
        currency=_normalise_currency(currency) or _currency_of(amount),
        availability=_normalise_availability(_meta(sel, "og:availability", "product:availability")),
    )


def _meta(sel: Selector, *names: str) -> str | None:
    for name in names:
        for attr in ("property", "name"):
            value = sel.css(f'meta[{attr}="{name}"]::attr(content)').get()
            if value and value.strip():
                return value.strip()
    return None


def _page_availability(html: str, sel: Selector) -> str | None:
    """Availability stated anywhere on the page, for methods that didn't find it."""
    meta = _meta(sel, "og:availability", "product:availability", "availability")
    if meta:
        return _normalise_availability(meta)
    for node in _walk(extruct.extract(html, syntaxes=["json-ld"], errors="ignore").get("json-ld", [])):
        result = _normalise_availability(node.get("availability"))
        if result:
            return result
    itemprop = sel.css('[itemprop="availability"]::attr(href), [itemprop="availability"]::attr(content)').get()
    return _normalise_availability(itemprop)


def _walk(data: Any) -> Iterator[dict]:
    """Yield every dict nested anywhere inside data.

    Structured metadata nests unpredictably — @graph wrappers, lists of offers,
    offers inside offers — so we flatten it and look at every node.
    """
    if isinstance(data, dict):
        yield data
        for value in data.values():
            if isinstance(value, (dict, list)):
                yield from _walk(value)
    elif isinstance(data, list):
        for value in data:
            yield from _walk(value)


def _has_type(node: dict, *wanted: str) -> bool:
    raw = node.get("@type") or node.get("type")
    if raw is None:
        return False
    values = raw if isinstance(raw, list) else [raw]
    names = {str(v).rsplit("/", 1)[-1].casefold() for v in values}
    return any(w.casefold() in names for w in wanted)


def _parse_price(value: Any) -> Decimal | None:
    """Turn a metadata value or display string into a Decimal.

    Structured metadata is meant to be a plain number, so parse that strictly —
    it avoids the "1.299" ambiguity (is that 1299 or 1.299?) that a fuzzy parser
    has to guess at. Anything messier goes to price-parser, which knows how to
    read "1 299 900 Ft" and "$1,299.00".
    """
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        candidate = Decimal(str(value))
        return candidate if candidate > 0 else None
    if isinstance(value, (list, tuple)):
        for item in value:
            price = _parse_price(item)
            if price is not None:
                return price
        return None
    if not isinstance(value, str):
        return None

    text = value.strip()
    if _STRICT_NUMBER.match(text):
        try:
            candidate = Decimal(text)
        except InvalidOperation:
            return None
        return candidate if candidate > 0 else None

    amount = Price.fromstring(text).amount
    return amount if amount is not None and amount > 0 else None


def _currency_of(text: str | None) -> str | None:
    """Best-effort ISO code from a display string like '1 299 900 Ft'."""
    if not text:
        return None
    symbol = Price.fromstring(text).currency
    return _normalise_currency(symbol)


def _normalise_currency(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    token = value.strip()
    if len(token) == 3 and token.isalpha():
        return token.upper()
    return _SYMBOL_TO_ISO.get(token.casefold())


def _normalise_availability(value: Any) -> str | None:
    """Map a schema.org availability value to in_stock / out_of_stock."""
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, dict):
        value = value.get("@id") or value.get("name")
    if not isinstance(value, str) or not value.strip():
        return None
    token = value.strip().rsplit("/", 1)[-1].replace(" ", "").replace("_", "").casefold()
    if not token:
        return None
    return IN_STOCK if token in _IN_STOCK_TOKENS else OUT_OF_STOCK
