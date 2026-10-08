"""Weekly refresh job.

Re-runs the Places search once per *known area* (not once per restaurant), so
one search (1-3 billed pages) refreshes both the restaurant list and every place's rating
data for that whole neighborhood at once - see cache.py's module docstring
for why areas are cached this way.

New areas are only ever added by live requests (app/service.py, on a cache
miss); this script just keeps areas that are already known from going stale.

The list of known areas comes from Postgres, which survives Redis restarts.
Areas found only in Redis (e.g. cached before Postgres was added) are
included too, so they get persisted on this run.

Intended to run on a schedule (cron / Windows Task Scheduler), e.g. weekly:
    python scripts/refresh_cache.py
"""
import logging
import sys
from pathlib import Path

# Allows `python scripts/refresh_cache.py` to work from any working
# directory (e.g. from a cron entry) without needing -m or a PYTHONPATH
# env var set up - `app` lives one level above this file.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import redis  # noqa: E402

from app import cache, db, places_client, service  # noqa: E402  (import after sys.path fix)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("refresh_cache")


def _known_areas() -> list[dict]:
    """Every area in Postgres, plus any that only exist in Redis."""
    areas = {area["key"]: area for area in db.all_areas()}
    try:
        for area in cache.all_cached_areas():
            areas.setdefault(area["key"], area)
    except redis.RedisError:
        logger.warning("Redis unavailable, refreshing Postgres areas only", exc_info=True)
    return list(areas.values())


def refresh_all_areas() -> None:
    areas = _known_areas()
    logger.info("Found %d known area(s) to refresh", len(areas))

    refreshed, failed = 0, 0
    for area in areas:
        try:
            places = service.fetch_and_store_area(area["center_lat"], area["center_lon"])
        except places_client.PlacesAPIError:
            logger.exception("Places search failed for area %s", area["key"])
            failed += 1
            continue

        refreshed += 1
        logger.info("Refreshed area %s: %d restaurant(s)", area["key"], len(places))

    logger.info("Done. %d area(s) refreshed, %d failed.", refreshed, failed)


if __name__ == "__main__":
    refresh_all_areas()
