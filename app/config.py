"""Centralized settings, loaded from environment variables (see .env.example).

Nothing else in the app should call os.getenv() directly - if a new tunable
is needed, it gets added here so there is one place that documents every
knob the system has.
"""
import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    google_places_api_key: str
    redis_url: str
    database_url: str
    search_radius_m: int
    max_candidates: int       # up to 60 (3 pages of Text Search)
    min_rating: float         # places rated below this are never fetched
    min_votes_threshold: int  # m: reviews needed to fully trust a rating
    prior_rating: float       # C, in the Bayesian formula
    weight_rating: float      # w1
    weight_distance: float    # w2
    # Used instead of the three above when an area is dense (the search
    # hit max_candidates) - see service._scoring_params.
    dense_min_votes_threshold: int
    dense_weight_rating: float
    dense_weight_distance: float
    cache_ttl_seconds: int


@lru_cache
def get_settings() -> Settings:
    """Read settings from the environment. Cached after the first call.

    Deferring this to a function (instead of building Settings at import
    time) means importing app.config never fails just because an .env file
    isn't set up yet - it only fails when something actually needs a value.
    """
    api_key = os.getenv("GOOGLE_PLACES_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GOOGLE_PLACES_API_KEY is not set. Copy .env.example to .env "
            "and fill in a real key."
        )

    return Settings(
        google_places_api_key=api_key,
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        database_url=os.getenv("DATABASE_URL", "postgresql://127.0.0.1:5432/best_restaurant"),
        search_radius_m=int(os.getenv("SEARCH_RADIUS_M", "1500")),
        max_candidates=int(os.getenv("MAX_CANDIDATES", "60")),
        min_rating=float(os.getenv("MIN_RATING", "4.0")),
        min_votes_threshold=int(os.getenv("MIN_VOTES_THRESHOLD", "200")),
        prior_rating=float(os.getenv("PRIOR_RATING", "4.0")),
        weight_rating=float(os.getenv("WEIGHT_RATING", "0.65")),
        weight_distance=float(os.getenv("WEIGHT_DISTANCE", "0.35")),
        dense_min_votes_threshold=int(os.getenv("DENSE_MIN_VOTES_THRESHOLD", "500")),
        dense_weight_rating=float(os.getenv("DENSE_WEIGHT_RATING", "0.8")),
        dense_weight_distance=float(os.getenv("DENSE_WEIGHT_DISTANCE", "0.2")),
        cache_ttl_seconds=int(os.getenv("CACHE_TTL_SECONDS", str(7 * 24 * 60 * 60))),
    )
