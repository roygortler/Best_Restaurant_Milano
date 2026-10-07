"""Tests for the HTTP layer - service.find_best_restaurants is monkeypatched,
so these only cover param validation, serialization, and error mapping.
"""
import os

os.environ.setdefault("GOOGLE_PLACES_API_KEY", "test-key")

from fastapi.testclient import TestClient

from app import service
from app.main import app
from app.places_client import PlacesAPIError
from app.scoring import RankedRestaurant

client = TestClient(app)

_RANKED = RankedRestaurant(
    place_id="p1", name="Trattoria", rating=4.5, user_rating_count=200,
    distance_km=0.3, bayesian_rating=4.4, normalized_rating=1.0,
    normalized_distance=1.0, final_score=1.0,
)


def test_restaurants_returns_ranked_results(monkeypatch):
    calls = []

    def fake_find(lat, lon, limit):
        calls.append((lat, lon, limit))
        return [_RANKED]

    monkeypatch.setattr(service, "find_best_restaurants", fake_find)

    response = client.get("/restaurants", params={"lat": 45.46, "lon": 9.19, "limit": 5})

    assert response.status_code == 200
    assert response.json()["results"][0]["place_id"] == "p1"
    assert calls == [(45.46, 9.19, 5)]


def test_missing_coordinates_is_422():
    assert client.get("/restaurants", params={"lat": 45.46}).status_code == 422


def test_out_of_range_coordinates_is_422():
    assert client.get("/restaurants", params={"lat": 91, "lon": 9.19}).status_code == 422


def test_places_api_error_is_502(monkeypatch):
    def failing_find(lat, lon, limit):
        raise PlacesAPIError("quota exceeded")

    monkeypatch.setattr(service, "find_best_restaurants", failing_find)

    response = client.get("/restaurants", params={"lat": 45.46, "lon": 9.19})

    assert response.status_code == 502


def test_health():
    assert client.get("/health").json() == {"status": "ok"}
