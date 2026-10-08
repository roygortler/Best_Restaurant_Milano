"""Request pipeline - glues places_client, cache, db, and scoring together.

This is the only module that knows about all of them. The flow for one
request is:

    1. Redis:    look up the user's grid cell and load each cached place.
    2. Postgres: on a Redis miss, try the same lookup in the durable copy,
                 and refill Redis from it if it's complete.
    3. Google:   if both miss, run a live Places search and write the
                 results to both stores.
    4. Rank the candidates against the user's exact coordinates.

If an area is known but any of its places has expired, we treat the whole
area as a miss and move to the next tier. One search call refreshes every
place at once, whereas patching the gaps would cost one Place Details call
per missing place (and details doesn't return location, which scoring
needs).

Both stores are optional at runtime: if Redis or Postgres is down, that
tier is logged and skipped rather than failing the request. Only a Places
API failure (with nothing usable in either store) reaches the caller.
"""
import logging

import psycopg
import redis

from app import cache, db, places_client
from app.config import get_settings
from app.scoring import RankedRestaurant, haversine_distance_km, rank_restaurants

logger = logging.getLogger(__name__)


def _load_from_redis(lat: float, lon: float) -> list[dict] | None:
    """Return the cached places for this area, or None if anything is missing."""
    try:
        place_ids = cache.get_cached_area(lat, lon)
        if place_ids is None:
            return None

        candidates = []
        for place_id in place_ids:
            place = cache.get_cached_place(place_id)
            if place is None:
                return None
            candidates.append(place)
        return candidates
    except redis.RedisError:
        logger.warning("Redis unavailable, skipping to Postgres", exc_info=True)
        return None


def _load_from_postgres(lat: float, lon: float) -> list[dict] | None:
    """Return this area's places from Postgres (refilling Redis), or None if incomplete."""
    try:
        area = db.get_area(cache.area_bucket_key(lat, lon))
        if area is None:
            return None
        places = db.get_places(area["place_ids"])
    except psycopg.Error:
        logger.warning("Postgres unavailable, skipping to Places API", exc_info=True)
        return None

    if len(places) != len(area["place_ids"]):
        return None

    try:
        # Keep the original timestamps so the Redis copy expires when the
        # Postgres data does, not a full TTL from now.
        for entry in places.values():
            cache.set_cached_place(entry["place"], last_updated=entry["last_updated"])
        cache.set_cached_area(lat, lon, area["place_ids"], last_updated=area["last_updated"])
    except redis.RedisError:
        logger.warning("Redis unavailable, could not refill from Postgres", exc_info=True)

    return [places[place_id]["place"] for place_id in area["place_ids"]]


def fetch_and_store_area(lat: float, lon: float) -> list[dict]:
    """Run a live Places search and write the results to Redis and Postgres.

    Also used by the weekly refresh job. Raises PlacesAPIError if the
    search fails; a store being down is logged and skipped.
    """
    places = places_client.search_restaurants(lat, lon)
    place_ids = [place["place_id"] for place in places]

    try:
        for place in places:
            cache.set_cached_place(place)
        cache.set_cached_area(lat, lon, place_ids)
    except redis.RedisError:
        logger.warning("Redis unavailable, results not cached in Redis", exc_info=True)

    try:
        center_lat, center_lon = cache.bucket_center(lat, lon)
        db.upsert_places(places)
        db.upsert_area(cache.area_bucket_key(lat, lon), center_lat, center_lon, place_ids)
    except psycopg.Error:
        logger.warning("Postgres unavailable, results not persisted", exc_info=True)

    return places


def _area_profile(candidates: list[dict], lat: float, lon: float) -> tuple[list[dict], dict]:
    """Pick the normal or the dense profile: which candidates to rank, and how.

    An area is dense when the search filled every slot (max_candidates):
    Google had more good restaurants than we could take. There, the user
    has plenty of nearby choice, so we're pickier:
      - only places rated at least dense_min_rating, within
        dense_max_distance_m (and distance scores 0 at that limit),
      - quality outweighs distance, and the review count needs a higher
        bar before it stops mattering, since most places have hundreds.

    Decided from the cached candidate list on every request, so it costs
    nothing extra and follows the area's data when it's refreshed.
    """
    settings = get_settings()
    if len(candidates) < settings.max_candidates:
        return candidates, {
            "min_votes_threshold": settings.min_votes_threshold,
            "weight_rating": settings.weight_rating,
            "weight_distance": settings.weight_distance,
            "max_distance_km": settings.search_radius_m / 1000,
        }

    max_distance_km = settings.dense_max_distance_m / 1000
    picked = [
        c for c in candidates
        if (c.get("rating") or 0) >= settings.dense_min_rating
        and haversine_distance_km(lat, lon, c["lat"], c["lon"]) <= max_distance_km
    ]
    return picked, {
        "min_votes_threshold": settings.dense_min_votes_threshold,
        "weight_rating": settings.dense_weight_rating,
        "weight_distance": settings.dense_weight_distance,
        "max_distance_km": max_distance_km,
    }


def find_best_restaurants(lat: float, lon: float, limit: int = 10) -> list[RankedRestaurant]:
    """Return the top `limit` restaurants near (lat, lon), best first."""
    settings = get_settings()

    candidates = _load_from_redis(lat, lon)
    if candidates is None:
        candidates = _load_from_postgres(lat, lon)
    if candidates is None:
        candidates = fetch_and_store_area(lat, lon)

    candidates, params = _area_profile(candidates, lat, lon)
    ranked = rank_restaurants(
        candidates,
        user_lat=lat,
        user_lon=lon,
        prior_rating=settings.prior_rating,
        **params,
    )
    return ranked[:limit]
