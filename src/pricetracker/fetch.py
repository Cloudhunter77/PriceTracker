"""Polite HTTP fetching of product pages."""

from __future__ import annotations

import logging
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3


class FetchError(Exception):
    """A product page could not be retrieved."""


@dataclass(slots=True)
class FetchResult:
    url: str
    html: str
    status_code: int


class Fetcher:
    """Fetches pages with a realistic browser-ish header set, retries on
    transient failures, and a per-domain delay so we never hammer one shop.

    Shops serve different markup to obvious bots, so we send the headers a real
    browser sends while keeping an identifying User-Agent.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float = 20.0,
        per_domain_delay: float = 2.0,
        respect_robots: bool = False,
    ) -> None:
        self.user_agent = user_agent
        self.per_domain_delay = per_domain_delay
        self.respect_robots = respect_robots
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,hu;q=0.8",
                "Cache-Control": "no-cache",
                "Upgrade-Insecure-Requests": "1",
            },
        )

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def fetch(self, url: str) -> FetchResult:
        """Retrieve a page, retrying transient errors with exponential backoff."""
        host = urlsplit(url).netloc.lower()
        allowed = self._robots_allows(url)
        if allowed is False:
            if self.respect_robots:
                raise FetchError("blocked by robots.txt (respect_robots is on)")
            log.info("robots.txt disallows %s; fetching anyway (respect_robots is off)", url)

        last_error: str = "unknown error"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._throttle(host)
            try:
                response = self._client.get(url)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code < 400:
                    return FetchResult(
                        url=str(response.url),
                        html=response.text,
                        status_code=response.status_code,
                    )
                last_error = f"HTTP {response.status_code}"
                if response.status_code not in RETRY_STATUSES:
                    break
            if attempt < MAX_ATTEMPTS:
                backoff = 2.0**attempt
                log.debug("retry %s/%s for %s in %.0fs (%s)", attempt, MAX_ATTEMPTS, url, backoff, last_error)
                time.sleep(backoff)

        raise FetchError(last_error)

    def _throttle(self, host: str) -> None:
        """Keep at least per_domain_delay seconds between hits on one host."""
        previous = self._last_request.get(host)
        if previous is not None:
            wait = self.per_domain_delay - (time.monotonic() - previous)
            if wait > 0:
                time.sleep(wait)
        self._last_request[host] = time.monotonic()

    def _robots_allows(self, url: str) -> bool | None:
        """True/False per robots.txt, or None when it can't be determined."""
        parts = urlsplit(url)
        root = f"{parts.scheme}://{parts.netloc}"
        if root not in self._robots:
            self._robots[root] = self._load_robots(parts)
        parser = self._robots[root]
        if parser is None:
            return None
        return parser.can_fetch(self.user_agent, url)

    def _load_robots(self, parts) -> urllib.robotparser.RobotFileParser | None:
        robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
        try:
            response = self._client.get(robots_url)
            if response.status_code >= 400:
                return None
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(response.text.splitlines())
            return parser
        except httpx.HTTPError:
            return None
