"""Watchlist configuration: schema, loading, and defaults resolution."""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from ruamel.yaml import YAML

DEFAULT_WATCHLIST = Path("watchlist.yaml")

# A real browser's User-Agent. The first version of this sent an identifying
# "PriceTracker/0.1" string, and nine consecutive days of production runs came
# back HTTP 403 from every shop — retail sites routinely reject any non-browser
# UA outright. One request per product per day is not a load anyone notices;
# looking like a bot is what got refused. Override it in watchlist.yaml under
# `defaults.user_agent` if you'd rather identify the tool.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)


def _yaml() -> YAML:
    """A round-trip YAML handler that preserves comments and formatting."""
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


class ConfigError(Exception):
    """Raised when the watchlist file is missing or malformed."""


class Defaults(BaseModel):
    """Settings applied to every item unless the item overrides them."""

    model_config = ConfigDict(extra="forbid")

    currency: str = "HUF"
    cooldown_days: int = Field(default=3, ge=0)
    drop_alert_pct: float = Field(default=10.0, ge=0)
    alert_on_out_of_stock: bool = False
    respect_robots: bool = False
    request_timeout: float = Field(default=20.0, gt=0)
    per_domain_delay: float = Field(default=2.0, ge=0)
    user_agent: str = DEFAULT_USER_AGENT

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        return _validate_currency(v)


class Source(BaseModel):
    """One shop's product page for an item."""

    model_config = ConfigDict(extra="forbid")

    url: str
    selector: str | None = None
    # Resolved from the item/defaults when not set explicitly.
    currency: str | None = None
    label: str | None = None

    @field_validator("url")
    @classmethod
    def _http_url(cls, v: str) -> str:
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"not an http(s) URL: {v!r}")
        return v

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str | None) -> str | None:
        return None if v is None else _validate_currency(v)

    @property
    def domain(self) -> str:
        """Hostname without a leading www., used for display and throttling."""
        host = urlsplit(self.url).netloc.lower()
        if ":" in host:
            host = host.split(":", 1)[0]
        return host.removeprefix("www.")

    @property
    def display_name(self) -> str:
        return self.label or self.domain


class Item(BaseModel):
    """A thing you want to buy, watched across one or more shops."""

    model_config = ConfigDict(extra="forbid")

    name: str
    target_price: Decimal = Field(gt=0)
    sources: list[Source] = Field(min_length=1)
    enabled: bool = True
    # Manufacturer part number, e.g. ILCE-6700B. Used to find the same product
    # at other shops; shops agree on these far more than they do on titles.
    model: str | None = None
    # None means "inherit from defaults"; resolved by Watchlist.
    currency: str | None = None
    cooldown_days: int | None = Field(default=None, ge=0)
    drop_alert_pct: float | None = Field(default=None, ge=0)
    alert_on_out_of_stock: bool | None = None

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str | None) -> str | None:
        return None if v is None else _validate_currency(v)


class Watchlist(BaseModel):
    """The whole config file: shared defaults plus the items to track."""

    model_config = ConfigDict(extra="forbid")

    defaults: Defaults = Field(default_factory=Defaults)
    items: list[Item] = Field(default_factory=list)

    @model_validator(mode="after")
    def _apply_defaults(self) -> Watchlist:
        """Push defaults down into items and sources so the rest of the code
        never has to ask 'was this set?' — every field is resolved here."""
        for item in self.items:
            if item.currency is None:
                item.currency = self.defaults.currency
            if item.cooldown_days is None:
                item.cooldown_days = self.defaults.cooldown_days
            if item.drop_alert_pct is None:
                item.drop_alert_pct = self.defaults.drop_alert_pct
            if item.alert_on_out_of_stock is None:
                item.alert_on_out_of_stock = self.defaults.alert_on_out_of_stock
            for source in item.sources:
                if source.currency is None:
                    source.currency = item.currency
        return self

    @property
    def active_items(self) -> list[Item]:
        return [i for i in self.items if i.enabled]

    def find(self, name: str) -> Item | None:
        """Look an item up by name, case-insensitively."""
        lowered = name.casefold()
        for item in self.items:
            if item.name.casefold() == lowered:
                return item
        return None


def _validate_currency(value: str) -> str:
    code = value.strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValueError(f"currency must be a 3-letter code like HUF or EUR, got {value!r}")
    return code


def load_watchlist(path: Path = DEFAULT_WATCHLIST) -> Watchlist:
    """Read and validate the watchlist file."""
    if not path.exists():
        raise ConfigError(
            f"No watchlist at {path}. Create one, or run "
            f"`pricetracker add <url> --target <price> --name <name>`."
        )
    with path.open(encoding="utf-8") as fh:
        raw = _yaml().load(fh)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level.")
    return Watchlist.model_validate(raw)


def append_item(
    path: Path,
    *,
    name: str,
    url: str,
    target_price: Decimal,
    currency: str | None = None,
    selector: str | None = None,
) -> None:
    """Add a source to the watchlist, creating the file or the item as needed.

    Uses a round-trip YAML load so hand-written comments and formatting in the
    file survive the edit.
    """
    yaml = _yaml()
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            raw = yaml.load(fh) or {}
    else:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level.")
    raw.setdefault("items", [])

    source: dict[str, str] = {"url": url}
    if selector:
        source["selector"] = selector

    for existing in raw["items"]:
        if str(existing.get("name", "")).casefold() == name.casefold():
            urls = {str(s.get("url")) for s in existing.get("sources", [])}
            if url in urls:
                raise ConfigError(f"{url} is already tracked under item {name!r}.")
            existing.setdefault("sources", []).append(source)
            break
    else:
        item: dict[str, object] = {"name": name, "target_price": to_yaml_number(target_price)}
        if currency:
            item["currency"] = currency.upper()
        item["sources"] = [source]
        raw["items"].append(item)

    # Validate before writing so a bad edit can never corrupt a working file.
    Watchlist.model_validate(raw)
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(raw, fh)


@contextmanager
def edit_watchlist(path: Path = DEFAULT_WATCHLIST):
    """Edit watchlist.yaml as a plain mapping, safely.

    Yields the round-trip mapping so comments and formatting survive. The result
    is validated before anything is written, so a bad edit leaves the working
    file untouched rather than corrupting it.
    """
    raw = {}
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            raw = _yaml().load(fh) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level.")
    raw.setdefault("items", [])

    yield raw

    Watchlist.model_validate(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        _yaml().dump(raw, fh)


def find_raw_item(raw: dict, name: str) -> dict | None:
    """The mapping for one item inside a round-trip watchlist."""
    for item in raw.get("items", []):
        if str(item.get("name", "")).casefold() == name.casefold():
            return item
    return None


def to_yaml_number(value: Decimal) -> int | float:
    """ruamel has no Decimal representer, so store a plain number."""
    return int(value) if value == value.to_integral_value() else float(value)
