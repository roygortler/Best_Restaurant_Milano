"""HTTP layer - a thin FastAPI wrapper around service.find_best_restaurants.

Everything interesting happens in service.py; this module only validates
query params, calls the pipeline, and translates errors into status codes.

Run locally with:
    uvicorn app.main:app --reload
"""
from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Query

from app import service
from app.places_client import PlacesAPIError

app = FastAPI(title="Best Restaurant")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/restaurants")
def get_restaurants(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    limit: int = Query(10, ge=1, le=20),
) -> dict:
    """Top `limit` restaurants near (lat, lon), best first."""
    try:
        ranked = service.find_best_restaurants(lat, lon, limit=limit)
    except PlacesAPIError as exc:
        # Upstream failure, not the caller's fault - 502 rather than 500.
        raise HTTPException(status_code=502, detail=f"Places API error: {exc}") from exc

    return {"results": [asdict(r) for r in ranked]}
