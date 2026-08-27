"""Recording events we've found, and remembering which ones you've been told about."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..store import DATA_DIR
from .models import Event

EVENTS_FILE = DATA_DIR / "events.jsonl"
SEEN_FILE = DATA_DIR / "events_seen.json"


@dataclass
class EventStore:
    """File-backed event history and 'already mentioned' state.

    Same shape as the price Store: an append-only JSONL of everything found, and
    a small state file recording what has already been emailed, so an event is
    only ever reported once.
    """

    events_path: Path = EVENTS_FILE
    seen_path: Path = SEEN_FILE
    _events: list[Event] | None = field(default=None, init=False, repr=False)

    def events(self) -> list[Event]:
        if self._events is None:
            self._events = self._load()
        return self._events

    def _load(self) -> list[Event]:
        if not self.events_path.exists():
            return []
        out: list[Event] = []
        with self.events_path.open(encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(Event.from_json(json.loads(line)))
                except (ValueError, KeyError) as exc:
                    print(f"warning: skipping {self.events_path}:{line_no}: {exc}")
        return out

    def append(self, events: list[Event]) -> None:
        if not events:
            return
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event.to_json(), ensure_ascii=False) + "\n")
        if self._events is not None:
            self._events.extend(events)

    def upcoming(self, now: datetime | None = None) -> list[Event]:
        """Everything recorded that hasn't happened yet, soonest first, deduplicated."""
        now = now or datetime.now(timezone.utc)
        latest: dict[str, Event] = {}
        for event in self.events():
            if (event.ends_at or event.starts_at) >= now:
                latest[event.uid or event.stable_uid()] = event  # later record wins
        return sorted(latest.values(), key=lambda e: (e.starts_at, e.title))

    # ---- what you've already been told ---------------------------------

    def load_seen(self) -> dict[str, str]:
        """Event uid -> ISO timestamp of when we first reported it."""
        if not self.seen_path.exists():
            return {}
        try:
            data = json.loads(self.seen_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def save_seen(self, seen: dict[str, str]) -> None:
        self.seen_path.parent.mkdir(parents=True, exist_ok=True)
        self.seen_path.write_text(
            json.dumps(dict(sorted(seen.items())), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def prune_seen(self, seen: dict[str, str], now: datetime, keep_days: int = 120) -> dict[str, str]:
        """Forget entries older than keep_days so the file doesn't grow forever."""
        cutoff = now.timestamp() - keep_days * 86400
        kept = {}
        for uid, stamp in seen.items():
            try:
                if datetime.fromisoformat(stamp).timestamp() >= cutoff:
                    kept[uid] = stamp
            except ValueError:
                continue
        return kept
