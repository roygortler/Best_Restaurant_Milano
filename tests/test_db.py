"""Tests for the Postgres layer - these run against a real database.

Set TEST_DATABASE_URL to a throwaway database to run them, e.g.
    TEST_DATABASE_URL=postgresql://roygortle:pw@localhost:5432/best_restaurant_test
They're skipped when it isn't set or the server can't be reached. Every
test starts from empty tables, so never point this at real data.
"""
import os

os.environ.setdefault("GOOGLE_PLACES_API_KEY", "test-key")

import psycopg
import pytest

from app import db
from app.config import get_settings

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


def _reachable(url: str | None) -> bool:
    if not url:
        return False
    try:
        psycopg.connect(url, connect_timeout=2).close()
        return True
    except psycopg.Error:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(TEST_DATABASE_URL),
    reason="TEST_DATABASE_URL not set or Postgres unreachable",
)

_PLACE = {"place_id": "p1", "name": "Trattoria", "rating": 4.5,
          "user_rating_count": 300, "lat": 45.4645, "lon": 9.1902}


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    db._pool = None
    with db.get_pool().connection() as conn:
        conn.execute("TRUNCATE places, areas")
    yield
    db._pool.close()
    db._pool = None
    get_settings.cache_clear()


def _age_row(table: str, key_column: str, key: str, seconds: int) -> None:
    with db.get_pool().connection() as conn:
        conn.execute(
            f"UPDATE {table} SET last_updated = now() - make_interval(secs => %s) "
            f"WHERE {key_column} = %s",
            (seconds, key),
        )


def test_place_round_trip():
    db.upsert_places([_PLACE])

    result = db.get_places(["p1"])

    assert result["p1"]["place"] == _PLACE
    assert result["p1"]["last_updated"] > 0


def test_get_places_omits_missing_ids():
    db.upsert_places([_PLACE])

    assert set(db.get_places(["p1", "nope"])) == {"p1"}


def test_get_places_omits_stale_places():
    db.upsert_places([_PLACE])
    _age_row("places", "place_id", "p1", get_settings().cache_ttl_seconds + 60)

    assert db.get_places(["p1"]) == {}


def test_upsert_place_overwrites_and_refreshes():
    db.upsert_places([_PLACE])
    _age_row("places", "place_id", "p1", get_settings().cache_ttl_seconds + 60)

    db.upsert_places([{**_PLACE, "rating": 4.7}])

    assert db.get_places(["p1"])["p1"]["place"]["rating"] == 4.7


def test_place_without_rating_is_stored():
    # Google omits rating for places with no reviews.
    db.upsert_places([{**_PLACE, "rating": None, "user_rating_count": 0}])

    assert db.get_places(["p1"])["p1"]["place"]["rating"] is None


def test_area_round_trip():
    db.upsert_area("area:1:2", 45.46, 9.19, ["p1", "p2"])

    area = db.get_area("area:1:2")

    assert area["place_ids"] == ["p1", "p2"]
    assert (area["center_lat"], area["center_lon"]) == (45.46, 9.19)


def test_missing_area_is_none():
    assert db.get_area("area:9:9") is None


def test_stale_area_is_none():
    db.upsert_area("area:1:2", 45.46, 9.19, ["p1"])
    _age_row("areas", "area_key", "area:1:2", get_settings().cache_ttl_seconds + 60)

    assert db.get_area("area:1:2") is None


def test_all_areas_includes_stale_ones():
    # The refresh job needs stale areas most of all.
    db.upsert_area("area:1:2", 45.46, 9.19, ["p1"])
    db.upsert_area("area:3:4", 45.50, 9.22, ["p2"])
    _age_row("areas", "area_key", "area:3:4", get_settings().cache_ttl_seconds + 60)

    keys = {area["key"] for area in db.all_areas()}

    assert keys == {"area:1:2", "area:3:4"}
