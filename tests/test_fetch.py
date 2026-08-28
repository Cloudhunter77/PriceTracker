"""The browser fetcher, without launching a browser.

Chromium itself is not the interesting part — the interesting part is knowing
when we are still looking at an interstitial rather than the page we asked for,
and giving up cleanly when the interstitial never lifts.
"""

from __future__ import annotations

import pytest

from pricetracker.fetch import BrowserFetcher, FetchError, looks_like_challenge

CHALLENGE = """<!doctype html><html><head><title>Just a moment...</title>
<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script>
</head><body>Checking your browser before accessing arukereso.hu</body></html>"""

PRODUCT = """<!doctype html><html><head><title>Sony A6700</title></head>
<body><script type="application/ld+json">{"@type":"Product"}</script></body></html>"""


def test_a_challenge_page_is_recognised():
    assert looks_like_challenge(CHALLENGE) is True


def test_a_real_page_is_not():
    assert looks_like_challenge(PRODUCT) is False


def test_a_review_saying_just_a_moment_is_not_a_challenge():
    """The markers are ordinary English. Only the top of the document counts,
    so a product page whose body quotes one is still a product page."""
    page = PRODUCT.replace("</body>", "<p>" + "x" * 5000 + " just a moment </p></body>")
    assert looks_like_challenge(page) is False


class FakePage:
    """A page that shows the challenge for a few polls, then the real thing."""

    def __init__(self, sequence: list[str]):
        self.sequence = sequence
        self.url = "https://www.arukereso.hu/p1"
        self.waits = 0

    def content(self) -> str:
        return self.sequence[min(self.waits, len(self.sequence) - 1)]

    def wait_for_timeout(self, ms: int) -> None:
        self.waits += 1


def test_it_waits_for_the_challenge_to_resolve():
    """A fixed sleep would either be too short on a slow day or waste time on a
    fast one, so it polls until the real document appears."""
    page = FakePage([CHALLENGE, CHALLENGE, PRODUCT])
    html = BrowserFetcher()._settle(page)
    assert "Sony A6700" in html
    assert page.waits == 2


def test_it_gives_up_rather_than_returning_the_interstitial():
    """Returning the challenge HTML would surface as 'no price found', which
    would send us hunting for a selector that could never exist."""
    fetcher = BrowserFetcher(challenge_timeout=0.2)
    page = FakePage([CHALLENGE])
    with pytest.raises(FetchError, match="out of reach"):
        fetcher._settle(page)


def test_a_page_that_is_already_the_real_one_is_not_waited_on():
    page = FakePage([PRODUCT])
    assert "Sony A6700" in BrowserFetcher()._settle(page)
    assert page.waits == 0


def test_the_profile_persists_between_runs():
    """Cloudflare's clearance cookie lives in the profile. Throwing it away each
    run would mean re-solving a challenge every morning, for them and for us."""
    from pricetracker.fetch import BROWSER_PROFILE

    assert BrowserFetcher().profile_dir == BROWSER_PROFILE
    assert BROWSER_PROFILE.parts[0] == "data"


def test_a_missing_playwright_says_what_to_install(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("no playwright")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(FetchError, match="playwright install chromium"):
        BrowserFetcher()._ensure_started()
