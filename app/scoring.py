"""Scoring pipeline - pure functions only, no API/Redis/network dependencies.

Takes a list of candidate restaurants (plain dicts with place_id, name,
rating, user_rating_count, lat, lon) plus the user's live GPS coordinates,
and ranks them by a weighted blend of:

  1. A Bayesian-adjusted rating, which pulls low-review-count restaurants
     toward the candidate set's mean - so a brand-new place with one
     5-star review doesn't outrank an established one with 800 reviews
     at 4.6.
  2. A log-distance score - closer is better, but with diminishing
     sensitivity as distance grows (the gap between 200m and 400m matters
     far more than the gap between 2km and 2.2km).

Both components are min-max normalized to [0, 1] across the candidate set
before being combined - they live on completely different numeric scales
(ratings ~1-5, distance scores unbounded above), so combining them with
weights only makes sense once they're on the same footing.

Everything here operates on plain dicts/dataclasses, so it's unit testable
with zero mocking - see tests/test_scoring.py.
"""
import math
from dataclasses import dataclass

DEFAULT_MIN_VOTES_THRESHOLD = 50  # m, in the Bayesian formula
DEFAULT_WEIGHT_RATING = 0.65      # w1
DEFAULT_WEIGHT_DISTANCE = 0.35    # w2

_EARTH_RADIUS_KM = 6371.0
_MIN_DISTANCE_KM = 0.001  # 1 meter floor - see distance_score() docstring


@dataclass(frozen=True)
class RankedRestaurant:
    place_id: str
    name: str
    rating: float | None
    user_rating_count: int
    distance_km: float
    bayesian_rating: float
    normalized_rating: float
    normalized_distance: float
    final_score: float


def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in kilometers."""
    lat1_r, lon1_r, lat2_r, lon2_r = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def bayesian_rating(rating: float | None, review_count: int, global_mean: float, m: int) -> float:
    """(v / (v+m)) * R + (m / (v+m)) * C

    A restaurant with zero reviews has no meaningful R - Google typically
    omits `rating` entirely for it - but v=0 already makes R's coefficient
    zero, so we return the global mean directly rather than multiplying a
    possibly-None rating by zero.
    """
    if review_count <= 0 or rating is None:
        return global_mean
    v = review_count
    return (v / (v + m)) * rating + (m / (v + m)) * global_mean


def distance_score(distance_km: float) -> float:
    """1 / log(1 + distance_km)

    log(1 + 0) = 0, which would divide by zero for a restaurant at the
    user's exact coordinates. GPS is rarely accurate below a few meters
    anyway, so distances under 1m are floored to 1m - "you're standing in
    it" still yields a large-but-finite score instead of crashing.
    """
    return 1 / math.log(1 + max(distance_km, _MIN_DISTANCE_KM))


def _min_max_normalize(values: list[float]) -> list[float]:
    """Scale values to [0, 1]. If every value is identical there's no basis
    to rank them apart - return 1.0 for all rather than 0.0, so a tied
    component still counts fully instead of vanishing from the final score.
    """
    lo, hi = min(values), max(values)
    if hi == lo:
        return [1.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def rank_restaurants(
    candidates: list[dict],
    user_lat: float,
    user_lon: float,
    min_votes_threshold: int = DEFAULT_MIN_VOTES_THRESHOLD,
    weight_rating: float = DEFAULT_WEIGHT_RATING,
    weight_distance: float = DEFAULT_WEIGHT_DISTANCE,
) -> list[RankedRestaurant]:
    """Score and rank candidate restaurants for a given live location."""
    if not candidates:
        return []

    weight_sum = weight_rating + weight_distance
    if abs(weight_sum - 1.0) > 1e-9:
        raise ValueError(f"weight_rating + weight_distance must equal 1.0, got {weight_sum}")

    # C: computed only from candidates that actually have a rating - a
    # zero-review place has no rating to contribute to "the mean rating of
    # restaurants around here".
    rated_values = [c["rating"] for c in candidates if c.get("rating") is not None]
    global_mean = sum(rated_values) / len(rated_values) if rated_values else 0.0

    bayesian_scores = [
        bayesian_rating(c.get("rating"), c["user_rating_count"], global_mean, min_votes_threshold)
        for c in candidates
    ]
    distances_km = [
        haversine_distance_km(user_lat, user_lon, c["lat"], c["lon"])
        for c in candidates
    ]
    distance_scores = [distance_score(d) for d in distances_km]

    normalized_ratings = _min_max_normalize(bayesian_scores)
    normalized_distances = _min_max_normalize(distance_scores)

    ranked = [
        RankedRestaurant(
            place_id=c["place_id"],
            name=c["name"],
            rating=c.get("rating"),
            user_rating_count=c["user_rating_count"],
            distance_km=distances_km[i],
            bayesian_rating=bayesian_scores[i],
            normalized_rating=normalized_ratings[i],
            normalized_distance=normalized_distances[i],
            final_score=weight_rating * normalized_ratings[i] + weight_distance * normalized_distances[i],
        )
        for i, c in enumerate(candidates)
    ]

    ranked.sort(key=lambda r: r.final_score, reverse=True)
    return ranked
