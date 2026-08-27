"""Turning venue addresses into coordinates, and measuring distance.

Scraped events give a street address, not a position, so addresses are geocoded
via OpenStreetMap's Nominatim. Its usage policy allows one request per second
with an identifying User-Agent, so results are cached on disk permanently —
an address does not move, and a cached lookup costs nothing on later runs.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from urllib.parse import urlencode

from ..fetch import Fetcher, FetchError
from .models import fold

log = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
GEOCACHE_FILE = Path("data") / "geocache.json"

# Nominatim asks for at most one request a second.
_POLITE_DELAY = 1.1


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in kilometres."""
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


class Geocoder:
    """Address to coordinates, with a permanent on-disk cache.

    Failures are cached too: a venue string that Nominatim cannot resolve will
    not resolve tomorrow either, and re-asking every run would be rude.
    """

    def __init__(
        self,
        cache_path: Path = GEOCACHE_FILE,
        user_agent: str | None = None,
        fetcher: Fetcher | None = None,
        enabled: bool = True,
    ) -> None:
        self.cache_path = cache_path
        self.enabled = enabled
        self._cache = self._load()
        self._fetcher = fetcher
        self._owns_fetcher = fetcher is None
        self._user_agent = user_agent or "PriceTracker/0.1 (+https://github.com/Cloudhunter77/PriceTracker)"
        self._dirty = False

    def _load(self) -> dict[str, list[float] | None]:
        if not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def save(self) -> None:
        if not self._dirty:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(self._cache, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._dirty = False

    def close(self) -> None:
        self.save()
        if self._owns_fetcher and self._fetcher is not None:
            self._fetcher.close()
            self._fetcher = None

    def __enter__(self) -> Geocoder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def locate(self, *parts: str | None) -> tuple[float, float] | None:
        """Best-effort coordinates for an address or venue name."""
        query = ", ".join(dict.fromkeys(p.strip() for p in parts if p and p.strip()))
        if not query:
            return None
        key = fold(query)
        if key in self._cache:
            cached = self._cache[key]
            return (cached[0], cached[1]) if cached else None
        if not self.enabled:
            return None

        result = self._lookup(query)
        self._cache[key] = list(result) if result else None
        self._dirty = True
        return result

    def _lookup(self, query: str) -> tuple[float, float] | None:
        if self._fetcher is None:
            self._fetcher = Fetcher(user_agent=self._user_agent, per_domain_delay=_POLITE_DELAY)
        url = f"{NOMINATIM_URL}?{urlencode({'q': query, 'format': 'json', 'limit': 1})}"
        try:
            response = self._fetcher.fetch(url)
            payload = json.loads(response.html)
        except (FetchError, ValueError) as exc:
            log.debug("geocoding %r failed: %s", query, exc)
            return None
        if not payload:
            return None
        try:
            return float(payload[0]["lat"]), float(payload[0]["lon"])
        except (KeyError, IndexError, TypeError, ValueError):
            return None
