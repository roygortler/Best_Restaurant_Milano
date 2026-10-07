"""Tests for the request pipeline - fakeredis for the cache, and
places_client.nearby_search monkeypatched so nothing touches the network.
"""
import os

os.environ.setdefault("GOOGLE_PLACES_API_KEY", "test-key")

import fakeredis

from app import cache, places_client, service
from app.config import get_settings

USER_LAT, USER_LON = 45.4642, 9.1900

_FAKE_PLACES = [
    {"place_id": "near-ok", "name": "Near OK", "rating": 4.0,
     "user_rating_count": 300, "lat": 45.4645, "lon": 9.1902},
    {"place_id": "far-great", "name": "Far Great", "rating": 4.8,
     "user_rating_count": 900, "lat": 45.4750, "lon": 9.2000},
]


def setup_function():
    get_settings.cache_clear()
    cache._client = fakeredis.FakeRedis(decode_responses=True)


def _install_fake_search(monkeypatch) -> list:
    """Replace nearby_search with a fake; returns a list that records each call."""
    calls = []

    def fake_nearby_search(lat, lon):
        calls.append((lat, lon))
        return [dict(p) for p in _FAKE_PLACES]

    monkeypatch.setattr(places_client, "nearby_search", fake_nearby_search)
    return calls


def test_cache_miss_calls_api_and_fills_cache(monkeypatch):
    calls = _install_fake_search(monkeypatch)

    results = service.find_best_restaurants(USER_LAT, USER_LON)

    assert len(calls) == 1
    assert {r.place_id for r in results} == {"near-ok", "far-great"}
    assert cache.get_cached_area(USER_LAT, USER_LON) == ["near-ok", "far-great"]
    assert cache.get_cached_place("near-ok")["name"] == "Near OK"


def test_second_request_in_same_area_uses_cache(monkeypatch):
    calls = _install_fake_search(monkeypatch)

    service.find_best_restaurants(USER_LAT, USER_LON)
    # ~1m away - same grid cell, so this must not hit the API again.
    service.find_best_restaurants(USER_LAT + 0.00001, USER_LON + 0.00001)

    assert len(calls) == 1


def test_missing_place_in_cached_area_triggers_refetch(monkeypatch):
    calls = _install_fake_search(monkeypatch)
    service.find_best_restaurants(USER_LAT, USER_LON)

    cache.get_client().delete("place:near-ok")
    service.find_best_restaurants(USER_LAT, USER_LON)

    assert len(calls) == 2


def test_results_are_ranked_and_limited(monkeypatch):
    _install_fake_search(monkeypatch)

    results = service.find_best_restaurants(USER_LAT, USER_LON, limit=1)

    assert len(results) == 1
    all_results = service.find_best_restaurants(USER_LAT, USER_LON)
    assert all_results[0].final_score >= all_results[1].final_score
    assert results[0].place_id == all_results[0].place_id


def test_no_restaurants_found_returns_empty_list(monkeypatch):
    monkeypatch.setattr(places_client, "nearby_search", lambda lat, lon: [])

    assert service.find_best_restaurants(USER_LAT, USER_LON) == []
