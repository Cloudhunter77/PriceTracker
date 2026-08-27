"""Price history charts, rendered as inline SVG.

Server-rendered rather than drawn by a JS library: no build step, no CDN, and
the page works offline. Colours come from the validated categorical palette and
are referenced as CSS variables so light and dark modes each get their own steps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..format import format_price

# Categorical slots 1-3, validated all-pairs for CVD in both modes.
# Past three shops a chart folds the rest into "Other" rather than inventing hues.
SERIES_SLOTS = 3

WIDTH = 720
HEIGHT = 260
PAD_LEFT = 64
PAD_RIGHT = 96  # room for the direct label at the end of each line
PAD_TOP = 16
PAD_BOTTOM = 32


@dataclass
class Series:
    """One shop's prices over time."""

    label: str
    points: list[tuple[datetime, Decimal]]
    slot: int


def build_series(readings) -> list[Series]:
    """Group successful readings by shop, newest last.

    Only the first few shops get their own colour; the rest are grouped, because
    a generated ninth hue is never distinguishable from the other eight.
    """
    by_shop: dict[str, list[tuple[datetime, Decimal]]] = {}
    for reading in readings:
        if reading.ok and reading.price is not None:
            by_shop.setdefault(reading.shop, []).append((reading.checked_at, reading.price))

    ordered = sorted(by_shop.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    series = []
    for index, (shop, points) in enumerate(ordered[:SERIES_SLOTS]):
        series.append(Series(label=shop, points=sorted(points), slot=index + 1))
    if len(ordered) > SERIES_SLOTS:
        rest: list[tuple[datetime, Decimal]] = []
        for _, points in ordered[SERIES_SLOTS:]:
            rest.extend(points)
        series.append(Series(label="Other shops", points=sorted(rest), slot=SERIES_SLOTS + 1))
    return series


def best_per_run(readings) -> list[tuple[datetime, Decimal]]:
    """The cheapest price across all shops, once per check.

    A sparkline drawn from raw readings would zig-zag between shops and read as
    a price trend that never happened; what actually matters over time is the
    best price available on each day.
    """
    per_run: dict[datetime, Decimal] = {}
    for reading in readings:
        if reading.ok and reading.price is not None:
            when = reading.checked_at
            if when not in per_run or reading.price < per_run[when]:
                per_run[when] = reading.price
    return sorted(per_run.items())


def sparkline(readings, width: int = 120, height: int = 28) -> str:
    """A tiny trend of the best price per run. One series, so no legend."""
    prices = best_per_run(readings)
    if len(prices) < 2:
        return ""
    values = [float(p) for _, p in prices]
    low, high = min(values), max(values)
    span = (high - low) or 1.0
    step = width / (len(values) - 1)
    points = " ".join(
        f"{i * step:.1f},{height - 2 - ((v - low) / span) * (height - 4):.1f}"
        for i, v in enumerate(values)
    )
    last_x = width
    last_y = height - 2 - ((values[-1] - low) / span) * (height - 4)
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'role="img" aria-label="Price trend, {len(values)} readings">'
        f'<polyline points="{points}" fill="none" stroke="var(--series-1)" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
        f'<circle cx="{last_x - 1:.1f}" cy="{last_y:.1f}" r="2.5" fill="var(--series-1)"/></svg>'
    )


@dataclass
class Chart:
    """A rendered chart plus the series it drew, so a legend can match it."""

    svg: str
    series: list[Series]


def line_chart(readings, currency: str | None, target=None) -> Chart:
    """A full price-history chart with axes, a target line and direct labels."""
    series = build_series(readings)
    if not series or all(len(s.points) < 1 for s in series):
        return Chart(svg='<p class="muted">No price history yet — run a check.</p>', series=[])

    all_points = [(t, v) for s in series for (t, v) in s.points]
    times = [t.timestamp() for t, _ in all_points]
    values = [float(v) for _, v in all_points]
    if target is not None:
        values.append(float(target))

    t_min, t_max = min(times), max(times)
    t_span = (t_max - t_min) or 1.0
    v_min, v_max = min(values), max(values)
    # A little headroom so lines never graze the frame.
    pad = (v_max - v_min) * 0.12 or (v_max * 0.05 or 1.0)
    v_min, v_max = v_min - pad, v_max + pad
    v_span = (v_max - v_min) or 1.0

    def x(ts: float) -> float:
        return PAD_LEFT + (ts - t_min) / t_span * (WIDTH - PAD_LEFT - PAD_RIGHT)

    def y(value: float) -> float:
        return PAD_TOP + (1 - (value - v_min) / v_span) * (HEIGHT - PAD_TOP - PAD_BOTTOM)

    parts: list[str] = [
        f'<svg class="chart" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" '
        f'aria-label="Price history by shop">'
    ]

    # Recessive gridlines with value labels.
    for frac in (0.0, 0.5, 1.0):
        value = v_min + frac * v_span
        gy = y(value)
        parts.append(
            f'<line x1="{PAD_LEFT}" y1="{gy:.1f}" x2="{WIDTH - PAD_RIGHT}" y2="{gy:.1f}" '
            f'class="grid"/>'
            f'<text x="{PAD_LEFT - 8}" y="{gy + 4:.1f}" class="axis" text-anchor="end">'
            f"{_short_money(value, currency)}</text>"
        )

    # Right-edge labels are collected and placed together at the end: two
    # series ending at a similar price would otherwise print on top of each other.
    edge_labels: list[tuple[float, str, str]] = []

    if target is not None:
        ty = y(float(target))
        parts.append(
            f'<line x1="{PAD_LEFT}" y1="{ty:.1f}" x2="{WIDTH - PAD_RIGHT}" y2="{ty:.1f}" '
            f'class="target"/>'
        )
        edge_labels.append((ty, "target", "axis target-label"))

    first_label = datetime.fromtimestamp(t_min).strftime("%-d %b")
    last_label = datetime.fromtimestamp(t_max).strftime("%-d %b")
    parts.append(
        f'<text x="{x(t_min):.1f}" y="{HEIGHT - 10}" class="axis" text-anchor="start">'
        f"{first_label}</text>"
    )
    # With a single day of history both ends carry the same date; drawing it
    # twice just collides with itself.
    if last_label != first_label:
        parts.append(
            f'<text x="{x(t_max):.1f}" y="{HEIGHT - 10}" class="axis" text-anchor="end">'
            f"{last_label}</text>"
        )

    for s in series:
        colour = f"var(--series-{min(s.slot, 8)})"
        pts = [(x(t.timestamp()), y(float(v))) for t, v in s.points]
        if len(pts) > 1:
            path = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
            parts.append(
                f'<polyline points="{path}" fill="none" stroke="{colour}" stroke-width="2" '
                f'stroke-linecap="round" stroke-linejoin="round"/>'
            )
        last_x, last_y = pts[-1]
        # A ring in the surface colour keeps overlapping markers readable.
        parts.append(
            f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="4" fill="{colour}" '
            f'stroke="var(--surface)" stroke-width="2"/>'
        )
        # Direct labels, not a number on every point. Also the relief the
        # palette's contrast warning requires.
        edge_labels.append((last_y, s.label, "series-label"))

    for label_y, text, css in _place_labels(edge_labels):
        parts.append(
            f'<text x="{WIDTH - PAD_RIGHT + 9:.1f}" y="{label_y + 4:.1f}" class="{css}">'
            f"{_escape(text)}</text>"
        )

    parts.append("</svg>")
    return Chart(svg="".join(parts), series=series)


LABEL_GAP = 15.0


def _place_labels(labels: list[tuple[float, str, str]]) -> list[tuple[float, str, str]]:
    """Nudge overlapping right-edge labels apart, keeping their order.

    Anchoring each label to its own line is only readable while the lines are
    far apart; when prices converge the labels have to be spread by hand.
    """
    ordered = sorted(labels, key=lambda item: item[0])
    placed: list[tuple[float, str, str]] = []
    for y_pos, text, css in ordered:
        if placed and y_pos - placed[-1][0] < LABEL_GAP:
            y_pos = placed[-1][0] + LABEL_GAP
        placed.append((y_pos, text, css))

    # If spreading pushed the last label past the bottom, shift the whole
    # stack up rather than letting it escape the chart.
    overflow = placed[-1][0] - (HEIGHT - PAD_BOTTOM) if placed else 0
    if overflow > 0:
        placed = [(y_pos - overflow, text, css) for y_pos, text, css in placed]
    return placed


def _short_money(value: float, currency: str | None) -> str:
    """Axis labels need to be short; 1 299 900 Ft becomes 1.3M."""
    magnitude = abs(value)
    if magnitude >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if magnitude >= 10_000:
        return f"{value / 1000:.0f}k"
    return format_price(Decimal(str(round(value, 2))), currency)


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )
