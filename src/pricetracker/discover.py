"""Finding the same product at other shops.

The tedious half of multi-shop tracking is going and finding each shop's URL by
hand. This searches them instead — but never adds anything on its own.

Everything here runs when you add a shop, not on the daily run. A confirmed
match becomes an ordinary source, so nothing in the hot path knows this module
exists. That also means a search can afford to be slow: it happens once, while
you watch.
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

from parsel import Selector

from .config import _yaml
from .events.models import fold
from .extract import Product, extract_product
from .fetch import Fetcher, FetchError

log = logging.getLogger(__name__)

SHOPS_FILE = Path("shops.yaml")

# How many result links to open per shop. Each is a throttled request, and a
# search page can link to a hundred products.
MAX_CANDIDATES_PER_SHOP = 8

# Below this, a candidate is shown but never pre-ticked.
SUGGEST_THRESHOLD = 0.75

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Words that mean "this is not the bare product you asked for". A kit bundle and
# a body differ by six figures in HUF, so this is the difference that matters.
BUNDLE_WORDS = {
    "kit", "szett", "csomag", "bundle", "objektivvel", "objektivval",
    "vazkit", "vaz kit", "hasznalt", "used", "bemutato", "refurbished",
}


@dataclass
class ShopSearch:
    """How to search one shop and recognise its product links."""

    domain: str
    search: str
    product_path: str
    verified: bool = False
    enabled: bool = True

    def search_url(self, query: str) -> str:
        return self.search.replace("{query}", quote(query.strip()))


@dataclass
class Candidate:
    """A product page that might be the same thing, and why we think so."""

    url: str
    shop: str
    title: str | None = None
    product: Product | None = None
    score: float = 0.0
    reason: str = ""

    @property
    def price(self) -> Decimal | None:
        return self.product.price if self.product else None

    @property
    def currency(self) -> str | None:
        return self.product.currency if self.product else None

    @property
    def suggested(self) -> bool:
        """Whether this is confident enough to pre-tick in the UI."""
        return self.score >= SUGGEST_THRESHOLD


@dataclass
class SearchOutcome:
    candidates: list[Candidate] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)  # (shop, error)


# ---- matching ----------------------------------------------------------


def normalise_model(text: str | None) -> str:
    """Strip a model number to comparable characters.

    'ILCE-6700', 'ilce 6700' and 'ILCE6700' are one code; without this,
    matching depends on each shop's punctuation habits.
    """
    if not text:
        return ""
    return _NON_ALNUM.sub("", text.casefold())


def looks_like_bundle(title: str | None) -> bool:
    """Whether a title advertises a kit, bundle or used unit.

    The expensive mistake this feature could make is quoting a kit-lens bundle
    as though it were the body, so bundles are demoted rather than silently
    ranked alongside.
    """
    folded = fold(title)
    return any(word in folded for word in BUNDLE_WORDS)


def title_similarity(a: str | None, b: str | None) -> float:
    """Accent- and case-insensitive similarity, 0 to 1."""
    left, right = fold(a), fold(b)
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def score_candidate(candidate: Candidate, model: str | None, title: str | None) -> Candidate:
    """Rank a candidate and record, in words, why.

    Model number first because it is the only signal shops agree on; title
    similarity second, for shops that publish no part number.
    """
    wanted = normalise_model(model)
    product = candidate.product
    haystacks = {
        "URL": normalise_model(urlsplit(candidate.url).path),
        "part number": " ".join(normalise_model(c) for c in (product.model_codes if product else [])),
        "title": normalise_model(candidate.title),
    }

    score, reason = 0.0, "no model number, weak title match"
    if wanted:
        for where, hay in haystacks.items():
            if hay and wanted in hay:
                score, reason = 1.0, f"model {model} found in {where}"
                break

    if score == 0.0:
        similarity = title_similarity(candidate.title, title)
        score = similarity
        reason = f"title {similarity:.0%} similar"

    if looks_like_bundle(candidate.title):
        # Still shown — sometimes the kit *is* what you want — but never
        # pre-ticked on the strength of a model number it shares with the body.
        score = min(score, 0.5)
        reason += "; looks like a kit or used item"

    if product is None or product.price is None:
        score = min(score, 0.4)
        reason += "; no price found on the page"

    candidate.score = round(score, 3)
    candidate.reason = reason
    return candidate


# ---- searching ---------------------------------------------------------


def find_product_links(html: str, base_url: str, shop: ShopSearch, model: str | None) -> list[str]:
    """Product links from a search results page, best candidates first.

    Filtering on the model number *before* fetching anything is what keeps this
    cheap: the real eMAG URL for an A7 IV contains `ilce7m4b`, so most
    accessories are ruled out without opening a single page.
    """
    sel = Selector(text=html)
    host = urlsplit(base_url).netloc
    wanted = normalise_model(model)

    matching: list[str] = []
    others: list[str] = []
    seen: set[str] = set()

    for href in sel.css("a::attr(href)").getall():
        absolute = urljoin(base_url, href.strip())
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https") or parts.netloc != host:
            continue
        if shop.product_path not in parts.path:
            continue
        clean = f"{parts.scheme}://{parts.netloc}{parts.path}"
        if clean in seen:
            continue
        seen.add(clean)
        (matching if wanted and wanted in normalise_model(parts.path) else others).append(clean)

    # Links whose slug carries the model number are tried first, so the cap
    # spends its budget on the likeliest pages.
    return (matching + others)[:MAX_CANDIDATES_PER_SHOP]


def search_shop(
    shop: ShopSearch,
    query: str,
    fetcher: Fetcher,
    *,
    model: str | None = None,
    title: str | None = None,
) -> list[Candidate]:
    """Search one shop and return scored candidates."""
    url = shop.search_url(query)
    try:
        page = fetcher.fetch(url)
    except FetchError as exc:
        raise FetchError(f"search failed: {exc}") from exc

    links = find_product_links(page.html, page.url, shop, model)
    if not links:
        return []

    candidates: list[Candidate] = []
    for link in links:
        try:
            detail = fetcher.fetch(link)
        except FetchError as exc:
            log.info("%s: skipping %s (%s)", shop.domain, link, exc)
            continue
        product = extract_product(detail.html)
        candidate = Candidate(
            url=link,
            shop=shop.domain,
            title=product.name if product else None,
            product=product,
        )
        candidates.append(score_candidate(candidate, model, title))

    return candidates


def search_shops(
    shops: list[ShopSearch],
    query: str,
    fetcher: Fetcher,
    *,
    model: str | None = None,
    title: str | None = None,
    skip_domains: set[str] | None = None,
    skip_urls: set[str] | None = None,
) -> SearchOutcome:
    """Search every enabled shop, best matches first.

    One shop being broken or blocked must not lose the others — the same rule
    the daily check follows.

    Pages already tracked are filtered out by URL as well as by domain: domain
    comparison alone is brittle (ports, www prefixes) and offering to add a
    shop that is already on the item is pure noise.
    """
    outcome = SearchOutcome()
    skip = {d.removeprefix("www.").split(":", 1)[0] for d in (skip_domains or set())}
    seen_urls = skip_urls or set()

    for shop in shops:
        if not shop.enabled or shop.domain.removeprefix("www.").split(":", 1)[0] in skip:
            continue
        try:
            found = search_shop(shop, query, fetcher, model=model, title=title)
        except FetchError as exc:
            outcome.failures.append((shop.domain, str(exc)))
            continue
        outcome.candidates.extend(c for c in found if c.url not in seen_urls)

    outcome.candidates.sort(key=lambda c: (-c.score, c.price or Decimal("Infinity")))
    return outcome


# ---- shop configuration ------------------------------------------------

# Only eMAG's pattern comes from a URL actually seen working. The rest are
# informed guesses and are marked unverified so `test-search` gets pointed at
# them before anything relies on them.
DEFAULT_SHOPS = [
    ShopSearch(
        domain="emag.hu",
        search="https://www.emag.hu/search/{query}",
        product_path="/pd/",
        verified=True,
    ),
    ShopSearch(
        domain="tripont.hu",
        search="https://www.tripont.hu/kereses?q={query}",
        product_path="-p",
    ),
    ShopSearch(
        domain="edigital.hu",
        search="https://edigital.hu/search?q={query}",
        product_path="/termek/",
    ),
    ShopSearch(
        domain="ipon.hu",
        search="https://ipon.hu/search?search={query}",
        product_path="/termek/",
    ),
]


def load_shops(path: Path = SHOPS_FILE) -> list[ShopSearch]:
    """Read shops.yaml, falling back to the built-in defaults.

    A file so a wrong search URL can be fixed without rebuilding the image —
    the container ships read-only code and its config lives in a mounted volume.
    """
    if not path.exists():
        return list(DEFAULT_SHOPS)
    with path.open(encoding="utf-8") as fh:
        raw = _yaml().load(fh) or {}
    entries = raw.get("shops") if isinstance(raw, dict) else None
    if not entries:
        return list(DEFAULT_SHOPS)
    return [
        ShopSearch(
            domain=str(e["domain"]),
            search=str(e["search"]),
            product_path=str(e.get("product_path", "/")),
            verified=bool(e.get("verified", False)),
            enabled=bool(e.get("enabled", True)),
        )
        for e in entries
    ]
