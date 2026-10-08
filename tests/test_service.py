"""Tests for the request pipeline - fakeredis for the cache, an in-memory
fake for Postgres, and places_client.search_restaurants monkeypatched so nothing
touches the network. The real Postgres layer is covered in test_db.py.
"""
import json
import os
import time

os.environ.setdefault("GOOGLE_PLACES_API_KEY", "test-key")

import fakeredis
import psycopg
import pytest

from app import cache, db, places_client, service
from app.config import get_settings

USER_LAT, USER_LON = 45.4642, 9.1900

_FAKE_PLACES = [
    {"place_id": "near-ok", "name": "Near OK", "rating": 4.0,
     "user_rating_count": 300, "lat": 45.4645, "lon": 9.1902},
    {"place_id": "far-great", "name": "Far Great", "rating": 4.8,
     "user_rating_count": 900, "lat": 45.4750, "lon": 9.2000},
]


class FakeDB:
    """Same interface as app.db, backed by dicts. Doesn't model staleness -
    that's tested against real Postgres in test_db.py."""

    def __init__(self):
        self.areas = {}
        self.places = {}

    def get_area(self, area_key):
        return self.areas.get(area_key)

    def get_places(self, place_ids):
        return {pid: self.places[pid] for pid in place_ids if pid in self.places}

    def upsert_places(self, places):
        for place in places:
            self.places[place["place_id"]] = {"place": dict(place), "last_updated": time.time()}

    def upsert_area(self, area_key, center_lat, center_lon, place_ids):
        self.areas[area_key] = {"place_ids": list(place_ids), "center_lat": center_lat,
                                "center_lon": center_lon, "last_updated": time.time()}


@pytest.fixture(autouse=True)
def fake_stores(monkeypatch):
    get_settings.cache_clear()
    cache._client = fakeredis.FakeRedis(decode_responses=True)

    fake_db = FakeDB()
    for name in ("get_area", "get_places", "upsert_places", "upsert_area"):
        monkeypatch.setattr(db, name, getattr(fake_db, name))
    return fake_db


def _install_fake_search(monkeypatch) -> list:
    """Replace search_restaurants with a fake; returns a list that records each call."""
    calls = []

    def fake_search_restaurants(lat, lon):
        calls.append((lat, lon))
        return [dict(p) for p in _FAKE_PLACES]

    monkeypatch.setattr(places_client, "search_restaurants", fake_search_restaurants)
    return calls


def _redis_down():
    server = fakeredis.FakeServer()
    server.connected = False
    cache._client = fakeredis.FakeRedis(server=server, decode_responses=True)


def _postgres_down(monkeypatch):
    def fail(*args, **kwargs):
        raise psycopg.OperationalError("connection refused")

    for name in ("get_area", "get_places", "upsert_places", "upsert_area"):
        monkeypatch.setattr(db, name, fail)


# --- Google tier ---------------------------------------------------------

def test_cache_miss_calls_api_and_fills_both_stores(monkeypatch, fake_stores):
    calls = _install_fake_search(monkeypatch)

    results = service.find_best_restaurants(USER_LAT, USER_LON)

    assert len(calls) == 1
    assert {r.place_id for r in results} == {"near-ok", "far-great"}
    assert cache.get_cached_area(USER_LAT, USER_LON) == ["near-ok", "far-great"]
    assert cache.get_cached_place("near-ok")["name"] == "Near OK"

    area = fake_stores.get_area(cache.area_bucket_key(USER_LAT, USER_LON))
    assert area["place_ids"] == ["near-ok", "far-great"]
    assert (area["center_lat"], area["center_lon"]) == cache.bucket_center(USER_LAT, USER_LON)
    assert set(fake_stores.places) == {"near-ok", "far-great"}


def test_no_restaurants_found_returns_empty_list(monkeypatch):
    monkeypatch.setattr(places_client, "search_restaurants", lambda lat, lon: [])

    assert service.find_best_restaurants(USER_LAT, USER_LON) == []


# --- Redis tier ----------------------------------------------------------

def test_second_request_in_same_area_uses_cache(monkeypatch):
    calls = _install_fake_search(monkeypatch)

    service.find_best_restaurants(USER_LAT, USER_LON)
    # ~1m away - same grid cell, so this must not hit the API again.
    service.find_best_restaurants(USER_LAT + 0.00001, USER_LON + 0.00001)

    assert len(calls) == 1


def test_results_are_ranked_and_limited(monkeypatch):
    _install_fake_search(monkeypatch)

    results = service.find_best_restaurants(USER_LAT, USER_LON, limit=1)

    assert len(results) == 1
    all_results = service.find_best_restaurants(USER_LAT, USER_LON)
    assert all_results[0].final_score >= all_results[1].final_score
    assert results[0].place_id == all_results[0].place_id


# --- Postgres tier -------------------------------------------------------

def test_redis_wiped_serves_from_postgres_without_api_call(monkeypatch):
    calls = _install_fake_search(monkeypatch)
    service.find_best_restaurants(USER_LAT, USER_LON)

    cache.get_client().flushall()
    results = service.find_best_restaurants(USER_LAT, USER_LON)

    assert len(calls) == 1
    assert {r.place_id for r in results} == {"near-ok", "far-great"}


def test_postgres_hit_refills_redis(monkeypatch):
    _install_fake_search(monkeypatch)
    service.find_best_restaurants(USER_LAT, USER_LON)
    cache.get_client().flushall()

    service.find_best_restaurants(USER_LAT, USER_LON)

    assert cache.get_cached_area(USER_LAT, USER_LON) == ["near-ok", "far-great"]
    assert cache.get_cached_place("far-great")["name"] == "Far Great"


def test_redis_refill_keeps_postgres_timestamp(monkeypatch, fake_stores):
    # Data that's 6 days old in Postgres must not become "fresh" in Redis.
    _install_fake_search(monkeypatch)
    service.find_best_restaurants(USER_LAT, USER_LON)
    six_days_ago = time.time() - 6 * 24 * 3600
    for entry in fake_stores.places.values():
        entry["last_updated"] = six_days_ago
    for entry in fake_stores.areas.values():
        entry["last_updated"] = six_days_ago
    cache.get_client().flushall()

    service.find_best_restaurants(USER_LAT, USER_LON)

    raw_area = json.loads(cache.get_client().get(cache.area_bucket_key(USER_LAT, USER_LON)))
    raw_place = json.loads(cache.get_client().get("place:near-ok"))
    assert raw_area["last_updated"] == six_days_ago
    assert raw_place["last_updated"] == six_days_ago


def test_place_missing_from_redis_falls_back_to_postgres(monkeypatch):
    calls = _install_fake_search(monkeypatch)
    service.find_best_restaurants(USER_LAT, USER_LON)

    cache.get_client().delete("place:near-ok")
    service.find_best_restaurants(USER_LAT, USER_LON)

    assert len(calls) == 1


def test_place_missing_from_both_stores_triggers_refetch(monkeypatch, fake_stores):
    calls = _install_fake_search(monkeypatch)
    service.find_best_restaurants(USER_LAT, USER_LON)

    cache.get_client().delete("place:near-ok")
    del fake_stores.places["near-ok"]
    service.find_best_restaurants(USER_LAT, USER_LON)

    assert len(calls) == 2


# --- Stores down ---------------------------------------------------------

def test_redis_down_serves_from_postgres(monkeypatch):
    calls = _install_fake_search(monkeypatch)
    service.find_best_restaurants(USER_LAT, USER_LON)

    _redis_down()
    results = service.find_best_restaurants(USER_LAT, USER_LON)

    assert len(calls) == 1
    assert len(results) == 2


def test_redis_down_on_cold_start_still_answers_from_api(monkeypatch, fake_stores):
    calls = _install_fake_search(monkeypatch)
    _redis_down()

    results = service.find_best_restaurants(USER_LAT, USER_LON)

    assert len(calls) == 1
    assert len(results) == 2
    assert set(fake_stores.places) == {"near-ok", "far-great"}


def test_postgres_down_falls_back_to_api(monkeypatch):
    calls = _install_fake_search(monkeypatch)
    _postgres_down(monkeypatch)

    results = service.find_best_restaurants(USER_LAT, USER_LON)

    assert len(calls) == 1
    assert len(results) == 2
    # Redis still got the results even though Postgres couldn't.
    assert cache.get_cached_area(USER_LAT, USER_LON) == ["near-ok", "far-great"]


def test_both_stores_down_still_answers_from_api(monkeypatch):
    calls = _install_fake_search(monkeypatch)
    _redis_down()
    _postgres_down(monkeypatch)

    results = service.find_best_restaurants(USER_LAT, USER_LON)

    assert len(calls) == 1
    assert len(results) == 2


def test_places_api_error_propagates(monkeypatch):
    def failing_search(lat, lon):
        raise places_client.PlacesAPIError("quota exceeded")

    monkeypatch.setattr(places_client, "search_restaurants", failing_search)

    with pytest.raises(places_client.PlacesAPIError):
        service.find_best_restaurants(USER_LAT, USER_LON)


# --- Dense-area scoring --------------------------------------------------

def _spy_on_ranking(monkeypatch) -> dict:
    """Record the keyword args find_best_restaurants passes to rank_restaurants."""
    seen = {}
    real_rank = service.rank_restaurants

    def spy(candidates, **kwargs):
        seen.update(kwargs)
        return real_rank(candidates, **kwargs)

    monkeypatch.setattr(service, "rank_restaurants", spy)
    return seen


def _set_max_candidates(monkeypatch, value: int):
    monkeypatch.setenv("MAX_CANDIDATES", str(value))
    get_settings.cache_clear()


def test_full_search_uses_dense_scoring(monkeypatch):
    # The fake search returns 2 places; a cap of 2 means every slot filled.
    _install_fake_search(monkeypatch)
    _set_max_candidates(monkeypatch, 2)
    seen = _spy_on_ranking(monkeypatch)

    service.find_best_restaurants(USER_LAT, USER_LON)

    settings = get_settings()
    assert seen["min_votes_threshold"] == settings.dense_min_votes_threshold
    assert seen["weight_rating"] == settings.dense_weight_rating
    assert seen["weight_distance"] == settings.dense_weight_distance


def test_partial_search_uses_normal_scoring(monkeypatch):
    _install_fake_search(monkeypatch)
    _set_max_candidates(monkeypatch, 3)
    seen = _spy_on_ranking(monkeypatch)

    service.find_best_restaurants(USER_LAT, USER_LON)

    settings = get_settings()
    assert seen["min_votes_threshold"] == settings.min_votes_threshold
    assert seen["weight_rating"] == settings.weight_rating
    assert seen["weight_distance"] == settings.weight_distance


def test_dense_mode_applies_to_cached_requests_too(monkeypatch):
    calls = _install_fake_search(monkeypatch)
    _set_max_candidates(monkeypatch, 2)
    service.find_best_restaurants(USER_LAT, USER_LON)

    seen = _spy_on_ranking(monkeypatch)
    service.find_best_restaurants(USER_LAT, USER_LON)

    assert len(calls) == 1  # second request came from the cache
    assert seen["min_votes_threshold"] == get_settings().dense_min_votes_threshold


def test_dense_weights_sum_to_one():
    # rank_restaurants rejects weights that don't sum to 1; catch a bad
    # default here rather than on the first dense request in production.
    settings = get_settings()
    assert settings.dense_weight_rating + settings.dense_weight_distance == pytest.approx(1.0)
