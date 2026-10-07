"""Scoring pipeline - pure functions only, no API/Redis/network dependencies.

Takes a list of candidate restaurants (plain dicts with place_id, name,
rating, user_rating_count, lat, lon) plus the user's live GPS coordinates,
and ranks them by a weighted blend of:

  1. A Bayesian-adjusted rating, which pulls low-review-count restaurants
     toward a fixed prior rating - so a brand-new place with one 5-star
     review doesn't outrank an established one with 800 reviews at 4.6.
     Past MIN_VOTES_THRESHOLD reviews the rating is taken at face value.
  2. A log-distance score - closer is better, but with diminishing
     sensitivity as distance grows (the gap between 200m and 400m matters
     far more than the gap between 1.2km and 1.4km).

Both components are scaled to [0, 1] against FIXED bounds (the 1-5 star
scale, and 0 to the search radius) rather than against the min/max of the
current candidate set. Scaling against the candidate set made a result's
score depend on who else happened to be in the list: with two candidates
every component became exactly 0 or 1, so the weights alone decided the
winner no matter how large or small the real differences were.

Everything here operates on plain dicts/dataclasses, so it's unit testable
with zero mocking - see tests/test_scoring.py.
"""
import math
from dataclasses import dataclass

DEFAULT_MIN_VOTES_THRESHOLD = 200 # m: reviews needed to fully trust a rating
DEFAULT_PRIOR_RATING = 4.0        # C, in the Bayesian formula
DEFAULT_MAX_DISTANCE_KM = 1.5     # distances at or beyond this score 0
DEFAULT_WEIGHT_RATING = 0.65      # w1
DEFAULT_WEIGHT_DISTANCE = 0.35    # w2

_EARTH_RADIUS_KM = 6371.0
_MIN_STARS, _MAX_STARS = 1.0, 5.0


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
    """trust * R + (1 - trust) * C, where trust = min(v / m, 1)

    Trust in the restaurant's own rating grows linearly with its review
    count until it reaches m, and is total from then on - 200 reviews and
    2000 reviews are treated as equally reliable. (The classic v/(v+m)
    form never fully trusts anyone, so 600 reviews would still beat 200.)

    A restaurant with zero reviews has no meaningful R - Google typically
    omits `rating` entirely for it - so we return C directly rather than
    multiplying a possibly-None rating by zero.
    """
    if review_count <= 0 or rating is None:
        return global_mean
    trust = min(review_count / m, 1.0)
    return trust * rating + (1 - trust) * global_mean


def rating_score(bayesian: float) -> float:
    """Map a 1-5 star rating onto [0, 1]."""
    return (bayesian - _MIN_STARS) / (_MAX_STARS - _MIN_STARS)


def distance_score(distance_km: float, max_distance_km: float) -> float:
    """1 - log(1 + d) / log(1 + max_d), clamped to [0, 1].

    1.0 when standing in the restaurant, 0.0 at the edge of the search
    radius. The log curve makes the score drop fastest close to the user,
    so nearby differences matter more than far-away ones.
    """
    if distance_km >= max_distance_km:
        return 0.0
    return 1 - math.log(1 + distance_km) / math.log(1 + max_distance_km)


def rank_restaurants(
    candidates: list[dict],
    user_lat: float,
    user_lon: float,
    min_votes_threshold: int = DEFAULT_MIN_VOTES_THRESHOLD,
    prior_rating: float = DEFAULT_PRIOR_RATING,
    max_distance_km: float = DEFAULT_MAX_DISTANCE_KM,
    weight_rating: float = DEFAULT_WEIGHT_RATING,
    weight_distance: float = DEFAULT_WEIGHT_DISTANCE,
) -> list[RankedRestaurant]:
    """Score and rank candidate restaurants for a given live location."""
    if not candidates:
        return []

    weight_sum = weight_rating + weight_distance
    if abs(weight_sum - 1.0) > 1e-9:
        raise ValueError(f"weight_rating + weight_distance must equal 1.0, got {weight_sum}")

    # C is a fixed prior, not the mean of this candidate set. With few
    # candidates, the set's mean is dominated by the very places being
    # judged - a lone 5.0 drags the mean up and barely gets discounted.
    bayesian_scores = [
        bayesian_rating(c.get("rating"), c["user_rating_count"], prior_rating, min_votes_threshold)
        for c in candidates
    ]
    distances_km = [
        haversine_distance_km(user_lat, user_lon, c["lat"], c["lon"])
        for c in candidates
    ]

    normalized_ratings = [rating_score(b) for b in bayesian_scores]
    normalized_distances = [distance_score(d, max_distance_km) for d in distances_km]

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
