"""Price history and alert state, kept as plain files in the repo.

History lives in an append-only JSON Lines file so it stays diffable in git,
survives the ephemeral CI runner that writes it, and loads straight into pandas
if you ever want to chart it. The volume is a handful of lines a day.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

DATA_DIR = Path("data")
HISTORY_FILE = DATA_DIR / "history.jsonl"
STATE_FILE = DATA_DIR / "alert_state.json"
DEBUG_DIR = DATA_DIR / "debug"

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_SUSPECT = "suspect"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class Reading:
    """The outcome of checking one source once."""

    checked_at: datetime
    item: str
    url: str
    shop: str
    status: str = STATUS_OK
    price: Decimal | None = None
    currency: str | None = None
    availability: str | None = None
    method: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK and self.price is not None

    @property
    def in_stock(self) -> bool | None:
        if self.availability is None:
            return None
        return self.availability == "in_stock"

    def to_json(self) -> dict:
        data = asdict(self)
        data["checked_at"] = self.checked_at.astimezone(timezone.utc).isoformat()
        data["price"] = None if self.price is None else str(self.price)
        return data

    @classmethod
    def from_json(cls, data: dict) -> Reading:
        return cls(
            checked_at=datetime.fromisoformat(data["checked_at"]),
            item=data["item"],
            url=data["url"],
            shop=data.get("shop", ""),
            status=data.get("status", STATUS_OK),
            price=None if data.get("price") is None else Decimal(str(data["price"])),
            currency=data.get("currency"),
            availability=data.get("availability"),
            method=data.get("method"),
            error=data.get("error"),
        )


@dataclass(slots=True)
class AlertState:
    """What we last told the user about an item, so we don't repeat ourselves."""

    price: Decimal
    at: datetime
    reason: str

    def to_json(self) -> dict:
        return {
            "price": str(self.price),
            "at": self.at.astimezone(timezone.utc).isoformat(),
            "reason": self.reason,
        }

    @classmethod
    def from_json(cls, data: dict) -> AlertState:
        return cls(
            price=Decimal(str(data["price"])),
            at=datetime.fromisoformat(data["at"]),
            reason=data.get("reason", "target"),
        )


@dataclass
class Store:
    """File-backed price history and alert state."""

    history_path: Path = HISTORY_FILE
    state_path: Path = STATE_FILE
    debug_dir: Path = DEBUG_DIR
    _readings: list[Reading] | None = field(default=None, init=False, repr=False)

    # ---- history -------------------------------------------------------

    def readings(self) -> list[Reading]:
        """All recorded readings, oldest first. Cached after the first read."""
        if self._readings is None:
            self._readings = self._load_history()
        return self._readings

    def _load_history(self) -> list[Reading]:
        if not self.history_path.exists():
            return []
        out: list[Reading] = []
        with self.history_path.open(encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(Reading.from_json(json.loads(line)))
                except (ValueError, KeyError) as exc:
                    # One corrupt line must not throw away the whole history.
                    print(f"warning: skipping {self.history_path}:{line_no}: {exc}")
        out.sort(key=lambda r: r.checked_at)
        return out

    def append(self, readings: list[Reading]) -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        with self.history_path.open("a", encoding="utf-8") as fh:
            for reading in readings:
                fh.write(json.dumps(reading.to_json(), ensure_ascii=False) + "\n")
        if self._readings is not None:
            self._readings.extend(readings)

    def for_item(self, item_name: str) -> list[Reading]:
        return [r for r in self.readings() if r.item == item_name]

    def last_ok_for_url(self, url: str) -> Reading | None:
        """Most recent successful reading for one source, for the parse guard."""
        for reading in reversed(self.readings()):
            if reading.url == url and reading.ok:
                return reading
        return None

    def last_reading_for_url(self, url: str) -> Reading | None:
        for reading in reversed(self.readings()):
            if reading.url == url:
                return reading
        return None

    def previous_best(self, item_name: str, currency: str) -> Decimal | None:
        """Cheapest price seen for an item in its most recent prior run."""
        prior = [r for r in self.for_item(item_name) if r.ok and r.currency == currency]
        if not prior:
            return None
        latest_run = max(r.checked_at for r in prior).date()
        same_run = [r.price for r in prior if r.checked_at.date() == latest_run]
        return min(same_run) if same_run else None

    def median_best(
        self,
        item_name: str,
        currency: str,
        days: int = 7,
        now: datetime | None = None,
    ) -> Decimal | None:
        """Median of the daily best prices over the N days before `now`.

        Compared against a median rather than yesterday's price so that a single
        odd reading doesn't set off a false 'sharp drop' alert.
        """
        cutoff = (now or utcnow()) - timedelta(days=days)
        per_day: dict[object, Decimal] = {}
        for reading in self.for_item(item_name):
            if not reading.ok or reading.currency != currency or reading.checked_at < cutoff:
                continue
            day = reading.checked_at.date()
            assert reading.price is not None
            if day not in per_day or reading.price < per_day[day]:
                per_day[day] = reading.price
        if not per_day:
            return None
        return Decimal(str(statistics.median(sorted(per_day.values()))))

    # ---- alert state ---------------------------------------------------

    def load_state(self) -> dict[str, AlertState]:
        if not self.state_path.exists():
            return {}
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except ValueError:
            return {}
        state: dict[str, AlertState] = {}
        for name, data in raw.items():
            try:
                state[name] = AlertState.from_json(data)
            except (ValueError, KeyError):
                continue
        return state

    def save_state(self, state: dict[str, AlertState]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {name: value.to_json() for name, value in sorted(state.items())}
        self.state_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    # ---- debugging -----------------------------------------------------

    def save_debug_html(self, url: str, html: str) -> Path:
        """Keep the page that failed to parse, so the cause is diagnosable."""
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in url)[-120:]
        path = self.debug_dir / f"{utcnow():%Y%m%dT%H%M%S}-{safe}.html"
        path.write_text(html, encoding="utf-8")
        return path
