"""HTTP layer - a thin FastAPI wrapper around the service and the web page.

Everything interesting happens in service.py; this module only validates
query params, calls the pipeline, and translates errors into status codes.

Run locally with:
    uvicorn app.main:app --reload
then open http://127.0.0.1:8000/ for the page, or /docs for the raw API.
"""
from dataclasses import asdict
from enum import Enum
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from app import places_client, service
from app.cuisines import CUISINES, label_for
from app.places_client import PlacesAPIError

app = FastAPI(title="Best Restaurant")

_INDEX_HTML = Path(__file__).parent / "static" / "index.html"

# Built from CUISINES so FastAPI rejects unknown values with a 422, and
# /docs shows the allowed values as a dropdown.
Cuisine = Enum("Cuisine", {key: key for key in CUISINES}, type=str)


def _maps_url(place_id: str, name: str) -> str:
    """Link that opens the place in Google Maps (app or web)."""
    return (f"https://www.google.com/maps/search/?api=1"
            f"&query={quote(name)}&query_place_id={quote(place_id)}")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(_INDEX_HTML)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/cuisines")
def get_cuisines() -> dict:
    """The cuisines /restaurants accepts, for building a picker."""
    return {"cuisines": [{"key": key, "label": c["label"]} for key, c in CUISINES.items()]}


@app.get("/geocode")
def geocode(address: str = Query(..., min_length=2, max_length=200)) -> dict:
    """Turn a typed address or landmark into coordinates."""
    try:
        result = places_client.geocode(address)
    except PlacesAPIError as exc:
        raise HTTPException(status_code=502, detail=f"Places API error: {exc}") from exc
    if result is None:
        raise HTTPException(status_code=404, detail=f"Couldn't find '{address}'")
    return result


@app.get("/restaurants")
def get_restaurants(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    limit: int = Query(10, ge=1, le=20),
    cuisine: Cuisine | None = Query(None, description="Leave empty for all cuisines"),
) -> dict:
    """Top `limit` restaurants near (lat, lon), best first."""
    try:
        ranked = service.find_best_restaurants(
            lat, lon, limit=limit, cuisine=cuisine.value if cuisine else None,
        )
    except PlacesAPIError as exc:
        # Upstream failure, not the caller's fault - 502 rather than 500.
        raise HTTPException(status_code=502, detail=f"Places API error: {exc}") from exc

    return {
        "results": [
            {
                **asdict(r),
                "cuisine": label_for(r.primary_type),
                "maps_url": _maps_url(r.place_id, r.name),
            }
            for r in ranked
        ]
    }
