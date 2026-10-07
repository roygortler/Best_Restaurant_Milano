"""Weekly refresh job.

Re-runs Nearby Search once per *known area* (not once per restaurant), so a
single API call refreshes both the restaurant list and every place's rating
data for that whole neighborhood at once - see cache.py's module docstring
for why areas are cached this way.

New areas are only ever added by live requests (app/service.py, on a cache
miss); this script just keeps areas that are already known from going stale.

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

from app import cache, places_client  # noqa: E402  (import after sys.path fix)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("refresh_cache")


def refresh_all_areas() -> None:
    areas = cache.all_cached_areas()
    logger.info("Found %d cached area(s) to refresh", len(areas))

    refreshed, failed = 0, 0
    for area in areas:
        lat, lon = area["center_lat"], area["center_lon"]
        try:
            places = places_client.nearby_search(lat, lon)
        except places_client.PlacesAPIError:
            logger.exception("Nearby search failed for area %s", area["key"])
            failed += 1
            continue

        place_ids = []
        for place in places:
            cache.set_cached_place(place)
            place_ids.append(place["place_id"])

        cache.set_cached_area(lat, lon, place_ids)
        refreshed += 1
        logger.info("Refreshed area %s: %d restaurant(s)", area["key"], len(place_ids))

    logger.info("Done. %d area(s) refreshed, %d failed.", refreshed, failed)


if __name__ == "__main__":
    refresh_all_areas()
