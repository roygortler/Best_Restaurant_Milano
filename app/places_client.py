"""Thin wrapper around the Places API (New).

Google is retiring the old `maps.googleapis.com/maps/api/place/*` endpoints
for new projects, so this talks to the current `places.googleapis.com/v1`
API instead. The New API requires an explicit field mask on every request
(X-Goog-FieldMask) - you only get billed for the fields you actually ask
for, so being stingy here directly controls cost.

Both functions return plain dicts with a small, stable set of keys instead
of the raw Google response shape. That normalization is what lets the rest
of the app (cache, scoring) stay ignorant of Google's JSON structure - if
Google changes their response format, only this file needs to change.
"""
import requests

from app.config import get_settings

PLACES_BASE_URL = "https://places.googleapis.com/v1"

# Nearby Search returns an array of places, so mask fields are prefixed
# with "places.".
_NEARBY_SEARCH_FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.location",
    "places.rating",
    "places.userRatingCount",
])

# Place Details returns a single place, so no "places." prefix.
_PLACE_DETAILS_FIELD_MASK = ",".join([
    "id",
    "displayName",
    "rating",
    "userRatingCount",
])


class PlacesAPIError(Exception):
    """Raised when the Places API returns a non-2xx response."""


def _normalize(raw: dict) -> dict:
    """Convert a raw Places API place object into our internal shape."""
    location = raw.get("location", {})
    return {
        "place_id": raw["id"],
        "name": raw.get("displayName", {}).get("text", "Unknown"),
        "rating": raw.get("rating"),
        "user_rating_count": raw.get("userRatingCount", 0),
        "lat": location.get("latitude"),
        "lon": location.get("longitude"),
    }


def nearby_search(lat: float, lon: float, radius_m: int | None = None,
                   max_results: int | None = None) -> list[dict]:
    """Find restaurants within radius_m of (lat, lon).

    Returns a list of normalized place dicts (see _normalize). This is the
    "discovery" call - it gets us place_ids and a first pass at rating
    data. Rating/review count from this call may be stale for places
    already in Redis; the cache layer decides whether to trust it or
    refetch details.
    """
    settings = get_settings()
    radius_m = radius_m or settings.search_radius_m
    max_results = max_results or settings.max_candidates

    url = f"{PLACES_BASE_URL}/places:searchNearby"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": settings.google_places_api_key,
        "X-Goog-FieldMask": _NEARBY_SEARCH_FIELD_MASK,
    }
    body = {
        "includedTypes": ["restaurant"],
        "maxResultCount": max_results,
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lon},
                "radius": radius_m,
            }
        },
    }

    response = requests.post(url, headers=headers, json=body, timeout=10)
    if response.status_code != 200:
        raise PlacesAPIError(
            f"Nearby search failed ({response.status_code}): {response.text}"
        )

    places = response.json().get("places", [])
    return [_normalize(place) for place in places]


def get_place_details(place_id: str) -> dict:
    """Fetch fresh rating/review-count data for a single place.

    Used by the weekly refresh job to update stale cache entries without
    re-running a full nearby search.
    """
    settings = get_settings()
    url = f"{PLACES_BASE_URL}/places/{place_id}"
    headers = {
        "X-Goog-Api-Key": settings.google_places_api_key,
        "X-Goog-FieldMask": _PLACE_DETAILS_FIELD_MASK,
    }

    response = requests.get(url, headers=headers, timeout=10)
    if response.status_code != 200:
        raise PlacesAPIError(
            f"Place details failed ({response.status_code}): {response.text}"
        )

    return _normalize(response.json())
