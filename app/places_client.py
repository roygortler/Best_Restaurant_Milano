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
import math

import requests

from app.config import get_settings

PLACES_BASE_URL = "https://places.googleapis.com/v1"

# Text Search returns an array of places, so mask fields are prefixed with
# "places.". nextPageToken must be in the mask too, or Google never sends
# it and we'd silently stop after the first page.
_TEXT_SEARCH_FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.location",
    "places.rating",
    "places.userRatingCount",
    "nextPageToken",
])

_PAGE_SIZE = 20       # Text Search's per-page maximum
_MAX_PAGES = 3        # Text Search returns at most 60 results in total
_METERS_PER_DEGREE_LAT = 111_320

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


def _bounding_box(lat: float, lon: float, radius_m: int) -> dict:
    """Square of side 2*radius_m centered on (lat, lon).

    Text Search only accepts a rectangle as a hard location restriction
    (circles are bias-only). Places in the square's corners are up to
    ~1.4x radius_m away; scoring gives anything past radius_m a distance
    score of 0, so they can still appear but only on rating strength.
    """
    dlat = radius_m / _METERS_PER_DEGREE_LAT
    dlon = radius_m / (_METERS_PER_DEGREE_LAT * math.cos(math.radians(lat)))
    return {
        "low": {"latitude": lat - dlat, "longitude": lon - dlon},
        "high": {"latitude": lat + dlat, "longitude": lon + dlon},
    }


def search_restaurants(lat: float, lon: float, radius_m: int | None = None,
                       max_results: int | None = None,
                       min_rating: float | None = None) -> list[dict]:
    """Find restaurants around (lat, lon) rated at least min_rating.

    Returns a list of normalized place dicts (see _normalize). This is the
    "discovery" call - it gets us place_ids and a first pass at rating
    data.

    Uses Text Search rather than Nearby Search because Nearby Search caps
    out at 20 results with no paging, which in a dense area like central
    Milan means only the 20 most popular places are ever considered. Text
    Search pages up to 60, and Google stops sending a next page once it
    runs out - so a quiet area costs 1 billed call and a dense one up to 3.
    """
    settings = get_settings()
    radius_m = radius_m or settings.search_radius_m
    max_results = min(max_results or settings.max_candidates, _PAGE_SIZE * _MAX_PAGES)
    min_rating = settings.min_rating if min_rating is None else min_rating

    url = f"{PLACES_BASE_URL}/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": settings.google_places_api_key,
        "X-Goog-FieldMask": _TEXT_SEARCH_FIELD_MASK,
    }
    # Google requires every page request to repeat the original parameters
    # exactly, with only pageToken added.
    base_body = {
        "textQuery": "restaurant",
        "includedType": "restaurant",
        "strictTypeFiltering": True,
        "pageSize": _PAGE_SIZE,
        "minRating": min_rating,
        "locationRestriction": {"rectangle": _bounding_box(lat, lon, radius_m)},
    }

    results: dict[str, dict] = {}
    page_token = None
    for _ in range(_MAX_PAGES):
        body = {**base_body, "pageToken": page_token} if page_token else base_body
        response = requests.post(url, headers=headers, json=body, timeout=10)
        if response.status_code != 200:
            raise PlacesAPIError(
                f"Text search failed ({response.status_code}): {response.text}"
            )

        payload = response.json()
        for raw in payload.get("places", []):
            place = _normalize(raw)
            results.setdefault(place["place_id"], place)  # dedupe across pages

        page_token = payload.get("nextPageToken")
        if not page_token or len(results) >= max_results:
            break

    return list(results.values())[:max_results]


def get_place_details(place_id: str) -> dict:
    """Fetch fresh rating/review-count data for a single place.

    Used by the weekly refresh job to update stale cache entries without
    re-running a full search.
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
