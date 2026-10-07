"""Request pipeline - glues places_client, cache, and scoring together.

This is the only module that knows about all three. The flow for one
request is:

    1. Look up the user's grid cell in the area cache.
    2. Cache hit  -> load each cached place.
       Cache miss -> run a live Nearby Search and cache what comes back.
    3. Rank the candidates against the user's exact coordinates.

If the area is cached but any of its places has expired, we treat the
whole area as a miss and re-run Nearby Search. One search call refreshes
every place at once, whereas patching the gaps would cost one Place
Details call per missing place (and details doesn't return location,
which scoring needs).
"""
from app import cache, places_client
from app.config import get_settings
from app.scoring import RankedRestaurant, rank_restaurants


def _load_cached_candidates(lat: float, lon: float) -> list[dict] | None:
    """Return the cached places for this area, or None if anything is missing."""
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


def _fetch_and_cache_candidates(lat: float, lon: float) -> list[dict]:
    """Run a live Nearby Search and store the results in both cache tiers."""
    places = places_client.nearby_search(lat, lon)
    for place in places:
        cache.set_cached_place(place)
    cache.set_cached_area(lat, lon, [place["place_id"] for place in places])
    return places


def find_best_restaurants(lat: float, lon: float, limit: int = 10) -> list[RankedRestaurant]:
    """Return the top `limit` restaurants near (lat, lon), best first."""
    settings = get_settings()

    candidates = _load_cached_candidates(lat, lon)
    if candidates is None:
        candidates = _fetch_and_cache_candidates(lat, lon)

    ranked = rank_restaurants(
        candidates,
        user_lat=lat,
        user_lon=lon,
        min_votes_threshold=settings.min_votes_threshold,
        prior_rating=settings.prior_rating,
        max_distance_km=settings.search_radius_m / 1000,
        weight_rating=settings.weight_rating,
        weight_distance=settings.weight_distance,
    )
    return ranked[:limit]
