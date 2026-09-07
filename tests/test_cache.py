"""Tests for the Redis caching layer, using fakeredis instead of a real
Redis instance - these run fully offline and don't need `redis-server`
installed anywhere.
"""
import os
import time

os.environ.setdefault("GOOGLE_PLACES_API_KEY", "test-key")

import fakeredis

from app import cache
from app.config import get_settings


def setup_function():
    get_settings.cache_clear()
    cache._client = fakeredis.FakeRedis(decode_responses=True)


def test_is_stale_pure_logic():
    now = 1_000_000.0
    assert cache.is_stale(last_updated=now - 100, ttl_seconds=50, now=now) is True
    assert cache.is_stale(last_updated=now - 10, ttl_seconds=50, now=now) is False


def test_bucket_key_stable_for_points_in_the_same_cell():
    base_lat, base_lon = 45.4642, 9.1900
    # ~1m offset - far smaller than the ~150m cell, so this must land in
    # the same bucket regardless of where base point sits within its cell.
    nearby_lat, nearby_lon = base_lat + 0.00001, base_lon + 0.00001
    assert cache.area_bucket_key(base_lat, base_lon) == cache.area_bucket_key(nearby_lat, nearby_lon)


def test_bucket_key_differs_for_points_a_kilometer_apart():
    base_lat, base_lon = 45.4642, 9.1900
    far_lat, far_lon = base_lat + 0.01, base_lon + 0.01  # roughly 1km away
    assert cache.area_bucket_key(base_lat, base_lon) != cache.area_bucket_key(far_lat, far_lon)


def test_set_and_get_cached_place_round_trip():
    place = {"place_id": "abc", "name": "Test Trattoria", "rating": 4.5, "user_rating_count": 100}
    cache.set_cached_place(place)
    assert cache.get_cached_place("abc") == place


def test_get_cached_place_returns_none_when_missing():
    assert cache.get_cached_place("does-not-exist") is None


def test_get_cached_place_returns_none_when_stale(monkeypatch):
    place = {"place_id": "abc", "name": "Test", "rating": 4.5, "user_rating_count": 100}
    cache.set_cached_place(place)

    settings = get_settings()
    future = time.time() + settings.cache_ttl_seconds + 1
    monkeypatch.setattr(cache.time, "time", lambda: future)

    assert cache.get_cached_place("abc") is None


def test_set_and_get_cached_area_round_trip():
    cache.set_cached_area(45.4642, 9.1900, ["p1", "p2"])
    assert cache.get_cached_area(45.4642, 9.1900) == ["p1", "p2"]


def test_get_cached_area_returns_none_when_stale(monkeypatch):
    cache.set_cached_area(45.4642, 9.1900, ["p1", "p2"])

    settings = get_settings()
    future = time.time() + settings.cache_ttl_seconds + 1
    monkeypatch.setattr(cache.time, "time", lambda: future)

    assert cache.get_cached_area(45.4642, 9.1900) is None


def test_all_cached_areas_returns_center_coords_for_refresh_job():
    cache.set_cached_area(45.4642, 9.1900, ["p1"])
    cache.set_cached_area(45.5000, 9.2200, ["p2"])

    areas = cache.all_cached_areas()

    assert len(areas) == 2
    for area in areas:
        assert "center_lat" in area and "center_lon" in area
