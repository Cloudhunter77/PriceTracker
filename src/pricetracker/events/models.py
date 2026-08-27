"""What an event is, once it has been read off a page or a calendar feed."""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal


def fold(text: str | None) -> str:
    """Casefold and strip accents so Hungarian text compares sensibly.

    'Színház', 'szinhaz' and 'SZÍNHÁZ' are one word as far as matching is
    concerned. Without this, keyword matching in Hungarian barely works.
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return unicodedata.normalize("NFKC", stripped).casefold()


@dataclass(slots=True)
class Event:
    """One event, from any source."""

    title: str
    starts_at: datetime
    source: str
    url: str | None = None
    ends_at: datetime | None = None
    venue: str | None = None
    address: str | None = None
    lat: float | None = None
    lon: float | None = None
    price: Decimal | None = None
    currency: str | None = None
    category: str | None = None
    description: str | None = None
    uid: str | None = None
    # Filled in by matching; not part of the event's identity.
    interests: list[str] = field(default_factory=list)
    distance_km: float | None = None

    def __post_init__(self) -> None:
        if self.uid is None:
            self.uid = self.stable_uid()

    def stable_uid(self) -> str:
        """An id that stays the same across runs, so we can tell you once.

        Deliberately built from what a listing page reliably repeats — source,
        title, start day and venue. URLs often carry tracking parameters that
        change between fetches, so they are not part of it.
        """
        parts = [
            fold(self.source),
            fold(self.title),
            self.starts_at.date().isoformat(),
            fold(self.venue),
        ]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]

    @property
    def starts_on(self) -> date:
        return self.starts_at.date()

    @property
    def searchable(self) -> str:
        """Everything a keyword could reasonably match against, folded."""
        return fold(" ".join(filter(None, [self.title, self.category, self.description, self.venue])))

    def to_json(self) -> dict:
        data = asdict(self)
        data["starts_at"] = self.starts_at.isoformat()
        data["ends_at"] = None if self.ends_at is None else self.ends_at.isoformat()
        data["price"] = None if self.price is None else str(self.price)
        return data

    @classmethod
    def from_json(cls, data: dict) -> Event:
        return cls(
            title=data["title"],
            starts_at=datetime.fromisoformat(data["starts_at"]),
            source=data["source"],
            url=data.get("url"),
            ends_at=None if data.get("ends_at") is None else datetime.fromisoformat(data["ends_at"]),
            venue=data.get("venue"),
            address=data.get("address"),
            lat=data.get("lat"),
            lon=data.get("lon"),
            price=None if data.get("price") is None else Decimal(str(data["price"])),
            currency=data.get("currency"),
            category=data.get("category"),
            description=data.get("description"),
            uid=data.get("uid"),
            interests=list(data.get("interests") or []),
            distance_km=data.get("distance_km"),
        )


def as_aware(value: datetime | date | None) -> datetime | None:
    """Normalise to a timezone-aware datetime.

    Feeds mix naive datetimes, all-day dates and proper aware timestamps;
    comparing those to each other raises, so everything is coerced on the way in.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return None
