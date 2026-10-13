"""Redis caching layer - pure Redis I/O, no knowledge of the Places API.

Two tiers, both keyed for a reason:

1. `place:{place_id}` - one restaurant's rating/review data.
2. `area:{lat_bucket}:{lon_bucket}` - the list of place_ids discovered
   within a ~150m x 150m grid cell. This is what lets repeat requests from
   the same neighborhood skip a live Places search entirely.

Distance is deliberately NOT cached anywhere here - it's cheap math computed
live from the request's exact GPS coordinates (see scoring.py), not an API
call, so there's nothing to cache.

This module never calls the Places API itself. Something else (the refresh
script, or the live request pipeline on a cache miss) is responsible for
fetching fresh data and handing it to set_cached_place/set_cached_area.
"""
import json
import math
import time

import redis

from app.config import get_settings

_AREA_CELL_SIZE_M = 150
_METERS_PER_DEGREE_LAT = 111_320  # approx, good enough at city scale

_client: redis.Redis | None = None


def get_client() -> redis.Redis:
    global _client
    if _client is None:
        settings = get_settings()
        _client = redis.from_url(settings.redis_url, decode_responses=True)
    return _client


def is_stale(last_updated: float, ttl_seconds: int, now: float | None = None) -> bool:
    """Pure staleness check, kept separate from Redis so it's trivial to unit test."""
    now = now if now is not None else time.time()
    return (now - last_updated) > ttl_seconds


def _bucket_indices(lat: float, lon: float) -> tuple[int, int]:
    lat_cell_deg = _AREA_CELL_SIZE_M / _METERS_PER_DEGREE_LAT
    lon_cell_deg = _AREA_CELL_SIZE_M / (_METERS_PER_DEGREE_LAT * math.cos(math.radians(lat)))
    return math.floor(lat / lat_cell_deg), math.floor(lon / lon_cell_deg)


def bucket_center(lat: float, lon: float) -> tuple[float, float]:
    """Center coordinate of the grid cell containing (lat, lon).

    Stored alongside each area's place_ids so the weekly refresh job has a
    stable point to re-run the Places search against, without needing to
    remember the original request's exact coordinates.
    """
    lat_cell_deg = _AREA_CELL_SIZE_M / _METERS_PER_DEGREE_LAT
    lon_cell_deg = _AREA_CELL_SIZE_M / (_METERS_PER_DEGREE_LAT * math.cos(math.radians(lat)))
    lat_bucket, lon_bucket = _bucket_indices(lat, lon)
    return (lat_bucket + 0.5) * lat_cell_deg, (lon_bucket + 0.5) * lon_cell_deg


def area_bucket_key(lat: float, lon: float, cuisine: str | None = None) -> str:
    """Key for the grid cell containing (lat, lon), one per cuisine.

    "area:33740:4783" is all restaurants in that cell; a cuisine search
    gets its own list, "area:33740:4783:japanese", since it's a separate
    Places search with different results.
    """
    lat_bucket, lon_bucket = _bucket_indices(lat, lon)
    key = f"area:{lat_bucket}:{lon_bucket}"
    return f"{key}:{cuisine}" if cuisine else key


def get_cached_area(lat: float, lon: float, cuisine: str | None = None) -> list[str] | None:
    """Return cached place_ids near (lat, lon), or None if missing/stale."""
    settings = get_settings()
    raw = get_client().get(area_bucket_key(lat, lon, cuisine))
    if raw is None:
        return None

    data = json.loads(raw)
    if is_stale(data["last_updated"], settings.cache_ttl_seconds):
        return None
    return data["place_ids"]


def set_cached_area(lat: float, lon: float, place_ids: list[str],
                    last_updated: float | None = None, cuisine: str | None = None) -> None:
    """Store the place_ids discovered near (lat, lon) for this grid cell.

    last_updated defaults to now; pass it explicitly when copying data in
    from Postgres, so the copy doesn't look fresher than the original.
    """
    settings = get_settings()
    center_lat, center_lon = bucket_center(lat, lon)
    payload = json.dumps({
        "place_ids": place_ids,
        "center_lat": center_lat,
        "center_lon": center_lon,
        "cuisine": cuisine,
        "last_updated": last_updated if last_updated is not None else time.time(),
    })
    # Redis TTL is a backstop (2x the staleness window) so dead areas
    # eventually get evicted even if the refresh job stops running; the
    # is_stale() check above is what actually drives refresh decisions.
    get_client().set(area_bucket_key(lat, lon, cuisine), payload,
                     ex=settings.cache_ttl_seconds * 2)


def get_cached_place(place_id: str) -> dict | None:
    """Return cached rating data for a place, or None if missing/stale."""
    settings = get_settings()
    raw = get_client().get(f"place:{place_id}")
    if raw is None:
        return None

    data = json.loads(raw)
    if is_stale(data["last_updated"], settings.cache_ttl_seconds):
        return None
    return data["place"]


def set_cached_place(place: dict, last_updated: float | None = None) -> None:
    """Store/refresh a single restaurant's rating data (last_updated: see set_cached_area)."""
    settings = get_settings()
    payload = json.dumps({
        "place": place,
        "last_updated": last_updated if last_updated is not None else time.time(),
    })
    get_client().set(f"place:{place['place_id']}", payload, ex=settings.cache_ttl_seconds * 2)


def all_cached_areas() -> list[dict]:
    """List every cached area (key, center coords, cuisine), for the weekly refresh job."""
    client = get_client()
    areas = []
    for key in client.scan_iter(match="area:*"):
        raw = client.get(key)
        if raw is None:
            continue
        data = json.loads(raw)
        areas.append({
            "key": key,
            "center_lat": data["center_lat"],
            "center_lon": data["center_lon"],
            "cuisine": data.get("cuisine"),  # absent on areas cached before cuisines existed
        })
    return areas


def _geocode_key(query: str) -> str:
    return f"geocode:{query}"


def get_cached_geocode(query: str) -> dict | None:
    """Return {"result": {lat, lon, address} or None} if `query` was looked up recently.

    A stored result of None means Google found nothing for that address -
    remembered too, so a typo isn't sent to Google on every retry. Returns
    None (no dict at all) when the address hasn't been looked up.

    Unlike areas, Redis's own expiry is the freshness rule here: a lookup
    is either reused or gone, there's no "stale but still listed" state.
    """
    raw = get_client().get(_geocode_key(query))
    return None if raw is None else json.loads(raw)


def set_cached_geocode(query: str, result: dict | None, ttl_seconds: int) -> None:
    get_client().set(_geocode_key(query), json.dumps({"result": result}), ex=ttl_seconds)
