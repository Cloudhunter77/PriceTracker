"""Which events are worth mentioning: soon enough, close enough, relevant."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pricetracker.events.config import EventsConfig
from pricetracker.events.matching import match_interests, select, within_window
from pricetracker.events.models import Event, fold

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

# A38 Hajó is ~3km from the Budapest centre point; Vienna is far away.
A38 = (47.4757, 19.0603)
VIENNA = (48.2082, 16.3738)


def make_config(**overrides) -> EventsConfig:
    data = {
        "home": {"lat": 47.4979, "lon": 19.0402, "radius_km": 15},
        "window_days": 30,
        "interests": [
            {"name": "Live music", "keywords": ["koncert", "concert", "jazz"]},
            {"name": "Culture", "keywords": ["színház", "kiállítás"]},
        ],
    }
    data.update(overrides)
    return EventsConfig.model_validate(data)


def make_event(title="Koncert", *, days=3, coords=A38, **kwargs) -> Event:
    lat, lon = coords if coords else (None, None)
    return Event(
        title=title,
        starts_at=NOW + timedelta(days=days),
        source=kwargs.pop("source", "test"),
        lat=lat,
        lon=lon,
        **kwargs,
    )


# ---- accent-insensitive keyword matching -------------------------------


@pytest.mark.parametrize(
    "title",
    ["Színház ma este", "Szinhaz ma este", "SZÍNHÁZ MA ESTE", "szìnhàz ma este"],
)
def test_hungarian_accents_and_case_are_ignored(title):
    """'Színház' and 'szinhaz' have to be one word or matching barely works."""
    assert match_interests(make_event(title), make_config().interests) == ["Culture"]


def test_keyword_with_accents_matches_unaccented_text():
    config = EventsConfig.model_validate(
        {"home": {"lat": 47.5, "lon": 19.0}, "interests": [{"name": "T", "keywords": ["tánc"]}]}
    )
    assert match_interests(make_event("Kortars tanc"), config.interests) == ["T"]


def test_matches_against_description_and_venue():
    event = make_event("Estély", description="Élő jazz zenekar")
    assert match_interests(event, make_config().interests) == ["Live music"]


def test_an_event_can_match_several_interests():
    event = make_event("Koncert és kiállítás")
    assert match_interests(event, make_config().interests) == ["Live music", "Culture"]


def test_irrelevant_event_matches_nothing():
    assert match_interests(make_event("Focimeccs"), make_config().interests) == []


def test_fold_handles_none_and_empty():
    assert fold(None) == ""
    assert fold("") == ""


# ---- the date window ---------------------------------------------------


def test_event_inside_the_window_counts():
    assert within_window(make_event(days=10), NOW, 30)


def test_event_beyond_the_window_does_not():
    assert not within_window(make_event(days=45), NOW, 30)


def test_finished_event_does_not():
    assert not within_window(make_event(days=-5), NOW, 30)


def test_event_earlier_today_still_counts():
    """Something that started at 18:00 is still worth seeing at 20:00."""
    assert within_window(make_event(days=0), NOW, 30)


def test_multi_day_festival_still_counts_after_it_starts():
    event = make_event(days=-2)
    event.ends_at = NOW + timedelta(days=2)
    assert within_window(event, NOW, 30)


# ---- distance ----------------------------------------------------------


def test_nearby_event_is_kept():
    selected = select([make_event(coords=A38)], make_config(), NOW)
    assert len(selected) == 1
    assert selected[0].distance_km == pytest.approx(2.9, abs=0.2)


def test_far_away_event_is_dropped():
    assert select([make_event("Koncert", coords=VIENNA)], make_config(), NOW) == []


def test_radius_is_configurable():
    config = make_config(home={"lat": 47.4979, "lon": 19.0402, "radius_km": 300})
    assert len(select([make_event("Koncert", coords=VIENNA)], config, NOW)) == 1


def test_event_with_unknown_location_is_kept_not_dropped():
    """Sources are city listings, so an unresolvable venue is a formatting
    problem, not evidence the event is in another country."""
    selected = select([make_event(coords=None)], make_config(), NOW)
    assert len(selected) == 1
    assert selected[0].distance_km is None


# ---- select() as a whole -----------------------------------------------


def test_select_annotates_and_sorts_soonest_first():
    events = [
        make_event("Jazz koncert", days=10),
        make_event("Kiállítás megnyitó", days=2),
        make_event("Focimeccs", days=1),
        make_event("Koncert Bécsben", days=3, coords=VIENNA),
        make_event("Régi koncert", days=-10),
    ]
    selected = select(events, make_config(), NOW)

    assert [e.title for e in selected] == ["Kiállítás megnyitó", "Jazz koncert"]
    assert selected[0].interests == ["Culture"]
    assert all(e.distance_km is not None for e in selected)


def test_no_interests_configured_keeps_everything_nearby():
    config = make_config(interests=[])
    selected = select([make_event("Focimeccs"), make_event("Koncert")], config, NOW)
    assert len(selected) == 2
