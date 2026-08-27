"""The events.yaml config: where you are, what you like, and where to look."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..config import ConfigError, _yaml
from .models import fold

DEFAULT_EVENTS_CONFIG = Path("events.yaml")


class Home(BaseModel):
    """The point that 'nearby' is measured from."""

    model_config = ConfigDict(extra="forbid")

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_km: float = Field(default=15.0, gt=0)
    label: str | None = None


class Interest(BaseModel):
    """A category of thing you want to hear about, and the words that signal it."""

    model_config = ConfigDict(extra="forbid")

    name: str
    keywords: list[str] = Field(min_length=1)
    enabled: bool = True

    @property
    def folded_keywords(self) -> list[str]:
        """Keywords normalised for accent- and case-insensitive matching."""
        return [fold(k) for k in self.keywords if k.strip()]


class SourceConfig(BaseModel):
    """One place to look for events."""

    model_config = ConfigDict(extra="forbid")

    name: str
    url: str
    type: str = "schemaorg"
    enabled: bool = True
    selector: str | None = None

    @field_validator("url")
    @classmethod
    def _http_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"not an http(s) URL: {v!r}")
        return v


class EventsConfig(BaseModel):
    """The whole events.yaml file."""

    model_config = ConfigDict(extra="forbid")

    home: Home
    window_days: int = Field(default=30, gt=0)
    interests: list[Interest] = Field(default_factory=list)
    sources: list[SourceConfig] = Field(default_factory=list)
    geocode: bool = True
    max_per_email: int = Field(default=25, gt=0)

    @model_validator(mode="after")
    def _unique_source_names(self) -> EventsConfig:
        seen = set()
        for source in self.sources:
            if source.name in seen:
                raise ValueError(f"duplicate source name {source.name!r}; names identify events across runs")
            seen.add(source.name)
        return self

    @property
    def active_sources(self) -> list[SourceConfig]:
        return [s for s in self.sources if s.enabled]

    @property
    def active_interests(self) -> list[Interest]:
        return [i for i in self.interests if i.enabled]


def load_events_config(path: Path = DEFAULT_EVENTS_CONFIG) -> EventsConfig:
    """Read and validate events.yaml."""
    if not path.exists():
        raise ConfigError(
            f"No events config at {path}. Copy the example from the README, "
            f"or add a source in the web UI."
        )
    with path.open(encoding="utf-8") as fh:
        raw = _yaml().load(fh)
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level.")
    return EventsConfig.model_validate(raw)


def save_events_config(path: Path, raw: dict) -> None:
    """Write events.yaml back, preserving comments, after validating it."""
    EventsConfig.model_validate(raw)
    yaml = _yaml()
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(raw, fh)


def load_raw(path: Path = DEFAULT_EVENTS_CONFIG) -> dict:
    """The file as a round-trip mapping, for editing without losing comments."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return _yaml().load(fh) or {}
