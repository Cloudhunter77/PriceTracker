"""Polite HTTP fetching of product pages."""

from __future__ import annotations

import logging
import os
import time
import urllib.robotparser
from dataclasses import dataclass
from pathlib import Path
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


# ---- fetching with a real browser --------------------------------------

# Some sites answer a plain HTTP client with a JavaScript challenge rather than
# the page — Cloudflare's "Just a moment…" interstitial. That is not something a
# header or a TLS trick can satisfy: the challenge is a page asking to be
# executed. A real browser engine executes it and carries on.
#
# This is deliberately a thin wrapper around actual Chromium. It does not forge
# fingerprints or solve CAPTCHAs; if executing the challenge honestly is not
# enough for a site, that site is out of reach and the run says so.

BROWSER_PROFILE = Path("data") / "browser"

# Chromium locks its user-data directory, so two processes cannot share one. The
# UI and the scheduler are separate containers against the same volume, so the
# UI's probe gets its own profile rather than failing whenever a daily run
# happens to be in progress.
PROBE_PROFILE = Path("data") / "browser-probe"

# Markers that say we are still looking at an interstitial, not the page.
_CHALLENGE_MARKERS = (
    "just a moment",
    "challenge-platform",
    "cf-chl",
    "checking your browser",
    "enable javascript and cookies",
)

CHALLENGE_TIMEOUT = 30.0


class BrowserFetcher:
    """Fetches pages with real Chromium, for sites that demand JavaScript.

    Interchangeable with Fetcher: same fetch/close/context-manager shape, so
    nothing downstream knows or cares which engine served a page.

    The profile persists between runs. Once a challenge is solved the site's
    clearance cookie is stored, so subsequent daily runs usually load the page
    directly — faster for us and considerably less work for them than solving a
    fresh challenge every morning.
    """

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        timeout: float = 45.0,
        per_domain_delay: float = 2.0,
        profile_dir: Path = BROWSER_PROFILE,
        locale: str = "hu-HU",
        timezone: str = "Europe/Budapest",
        headless: bool = True,
        challenge_timeout: float = CHALLENGE_TIMEOUT,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.per_domain_delay = per_domain_delay
        self.profile_dir = profile_dir
        self.locale = locale
        self.timezone = timezone
        self.headless = headless
        self.challenge_timeout = challenge_timeout
        self._last_request: dict[str, float] = {}
        self._playwright = None
        self._context = None

    # -- lifecycle -------------------------------------------------------

    def _ensure_started(self) -> None:
        if self._context is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise FetchError(
                "this source needs a browser, but Playwright is not installed. "
                "Use the published container image, or `pip install playwright && "
                "playwright install chromium`."
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        launch: dict = {
            "user_data_dir": str(self.profile_dir),
            "headless": self.headless,
            "locale": self.locale,
            "timezone_id": self.timezone,
            "viewport": {"width": 1366, "height": 900},
            # A shared /dev/shm is small in containers and crashes Chromium on
            # heavier pages.
            "args": ["--disable-dev-shm-usage", "--no-sandbox"],
        }
        if self.user_agent:
            launch["user_agent"] = self.user_agent
        executable = os.environ.get("CHROMIUM_PATH")
        if executable:
            launch["executable_path"] = executable
        try:
            self._context = self._playwright.chromium.launch_persistent_context(**launch)
        except Exception as exc:
            self.close()
            raise FetchError(
                f"could not start Chromium: {exc}. If another run is already "
                f"using {self.profile_dir}, wait for it to finish — a browser "
                "profile cannot be shared between two processes."
            ) from exc
        self._context.set_default_timeout(self.timeout * 1000)

    def close(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:  # pragma: no cover - shutdown is best effort
                pass
            self._context = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:  # pragma: no cover
                pass
            self._playwright = None

    def __enter__(self) -> BrowserFetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- fetching --------------------------------------------------------

    def fetch(self, url: str) -> FetchResult:
        """Load a page, waiting out any interstitial before reading it."""
        self._ensure_started()
        assert self._context is not None
        self._throttle(urlsplit(url).netloc.lower())

        page = self._context.new_page()
        try:
            response = page.goto(url, wait_until="domcontentloaded")
            status = response.status if response is not None else 0
            html = self._settle(page)
            return FetchResult(url=page.url, html=html, status_code=status or 200)
        except Exception as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc
        finally:
            try:
                page.close()
            except Exception:  # pragma: no cover
                pass

    def _settle(self, page) -> str:
        """Wait for the real document rather than a fixed sleep.

        A challenge page swaps itself for the real one when it completes, so we
        poll for the markers disappearing and give up cleanly if they don't.
        """
        deadline = time.monotonic() + self.challenge_timeout
        html = page.content()
        while looks_like_challenge(html) and time.monotonic() < deadline:
            page.wait_for_timeout(1000)
            html = page.content()

        if looks_like_challenge(html):
            raise FetchError(
                "still on a JavaScript challenge page after "
                f"{self.challenge_timeout:.0f}s — this site wants more than a real "
                "browser executing its challenge, so it is out of reach"
            )
        return html

    def _throttle(self, host: str) -> None:
        previous = self._last_request.get(host)
        if previous is not None:
            wait = self.per_domain_delay - (time.monotonic() - previous)
            if wait > 0:
                time.sleep(wait)
        self._last_request[host] = time.monotonic()


def looks_like_challenge(html: str) -> bool:
    """Whether this is an interstitial rather than the page we asked for.

    Deliberately checks only the opening of the document: the phrases are
    common enough that a product page mentioning "just a moment" in a review
    should not be mistaken for a challenge.
    """
    head = html[:4000].casefold()
    return any(marker in head for marker in _CHALLENGE_MARKERS)
