"""PostgreSQL persistence - the durable copy of everything in the Redis cache.

Redis is the fast tier but lives in memory, so a restart or eviction loses
everything and the next requests would all pay for live Places API calls.
Every write therefore goes to both stores, and service.py reads Postgres
when Redis misses, before falling back to Google.

Same two tiers as cache.py (places, and areas listing place_ids), same
staleness window. Like cache.py, this module is pure storage I/O - it never
calls the Places API and doesn't know about Redis.
"""
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS places (
    place_id          TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    rating            DOUBLE PRECISION,
    user_rating_count INTEGER NOT NULL,
    lat               DOUBLE PRECISION NOT NULL,
    lon               DOUBLE PRECISION NOT NULL,
    last_updated      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS areas (
    area_key     TEXT PRIMARY KEY,
    center_lat   DOUBLE PRECISION NOT NULL,
    center_lon   DOUBLE PRECISION NOT NULL,
    place_ids    TEXT[] NOT NULL,
    last_updated TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# Short timeouts on purpose: Postgres is a fallback, so if it's down a
# request should give up on it quickly and go to Google, not hang.
_CONNECT_TIMEOUT_S = 2
_POOL_TIMEOUT_S = 2

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    """Lazily open the pool and make sure the tables exist."""
    global _pool
    if _pool is None:
        settings = get_settings()
        pool = ConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=5,
            timeout=_POOL_TIMEOUT_S,
            kwargs={"connect_timeout": _CONNECT_TIMEOUT_S, "row_factory": dict_row},
            open=True,
        )
        try:
            with pool.connection() as conn:
                conn.execute(_SCHEMA)
        except Exception:
            # Don't leak a pool (and its background workers) per failed request.
            pool.close()
            raise
        _pool = pool
    return _pool


def get_area(area_key: str) -> dict | None:
    """Return {place_ids, center_lat, center_lon, last_updated}, or None if missing/stale.

    last_updated is a Unix timestamp, matching what cache.py stores.
    """
    with get_pool().connection() as conn:
        return conn.execute(
            """
            SELECT place_ids, center_lat, center_lon,
                   extract(epoch FROM last_updated)::float AS last_updated
            FROM areas
            WHERE area_key = %s
              AND last_updated > now() - make_interval(secs => %s)
            """,
            (area_key, get_settings().cache_ttl_seconds),
        ).fetchone()


def get_places(place_ids: list[str]) -> dict[str, dict]:
    """Return {place_id: {"place": {...}, "last_updated": ts}} for the fresh ones.

    Missing and stale places are simply absent from the result - the caller
    compares lengths to decide whether the set is complete.
    """
    if not place_ids:
        return {}
    with get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT place_id, name, rating, user_rating_count, lat, lon,
                   extract(epoch FROM last_updated)::float AS last_updated
            FROM places
            WHERE place_id = ANY(%s)
              AND last_updated > now() - make_interval(secs => %s)
            """,
            (place_ids, get_settings().cache_ttl_seconds),
        ).fetchall()

    result = {}
    for row in rows:
        last_updated = row.pop("last_updated")
        result[row["place_id"]] = {"place": row, "last_updated": last_updated}
    return result


def upsert_places(places: list[dict]) -> None:
    """Insert or refresh each place, stamping it with the current time."""
    if not places:
        return
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO places (place_id, name, rating, user_rating_count, lat, lon)
            VALUES (%(place_id)s, %(name)s, %(rating)s, %(user_rating_count)s, %(lat)s, %(lon)s)
            ON CONFLICT (place_id) DO UPDATE SET
                name = EXCLUDED.name,
                rating = EXCLUDED.rating,
                user_rating_count = EXCLUDED.user_rating_count,
                lat = EXCLUDED.lat,
                lon = EXCLUDED.lon,
                last_updated = now()
            """,
            places,
        )


def upsert_area(area_key: str, center_lat: float, center_lon: float, place_ids: list[str]) -> None:
    """Insert or refresh an area's place list, stamping it with the current time."""
    with get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO areas (area_key, center_lat, center_lon, place_ids)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (area_key) DO UPDATE SET
                center_lat = EXCLUDED.center_lat,
                center_lon = EXCLUDED.center_lon,
                place_ids = EXCLUDED.place_ids,
                last_updated = now()
            """,
            (area_key, center_lat, center_lon, place_ids),
        )


def all_areas() -> list[dict]:
    """List every known area (key + center coords), stale or not, for the refresh job."""
    with get_pool().connection() as conn:
        return conn.execute(
            "SELECT area_key AS key, center_lat, center_lon FROM areas"
        ).fetchall()
