"""Tests for the HTTP layer - the service and Places client are monkeypatched,
so these only cover param validation, serialization, and error mapping.
"""
import os

os.environ.setdefault("GOOGLE_PLACES_API_KEY", "test-key")

from fastapi.testclient import TestClient

from app import service
from app.cuisines import CUISINES
from app.main import app
from app.places_client import PlacesAPIError
from app.scoring import RankedRestaurant

client = TestClient(app)

_RANKED = RankedRestaurant(
    place_id="p1", name="Trattoria Da Mario", rating=4.5, user_rating_count=200,
    distance_km=0.3, bayesian_rating=4.4, normalized_rating=1.0,
    normalized_distance=1.0, final_score=1.0, primary_type="italian_restaurant",
)


def _install_fake_find(monkeypatch) -> list:
    calls = []

    def fake_find(lat, lon, limit, cuisine):
        calls.append((lat, lon, limit, cuisine))
        return [_RANKED]

    monkeypatch.setattr(service, "find_best_restaurants", fake_find)
    return calls


# --- /restaurants --------------------------------------------------------

def test_restaurants_returns_ranked_results(monkeypatch):
    calls = _install_fake_find(monkeypatch)

    response = client.get("/restaurants", params={"lat": 45.46, "lon": 9.19, "limit": 5})

    assert response.status_code == 200
    assert response.json()["results"][0]["place_id"] == "p1"
    assert calls == [(45.46, 9.19, 5, None)]


def test_restaurants_adds_cuisine_label_and_maps_link(monkeypatch):
    _install_fake_find(monkeypatch)

    result = client.get("/restaurants", params={"lat": 45.46, "lon": 9.19}).json()["results"][0]

    assert result["cuisine"] == "Italian"
    assert result["maps_url"].startswith("https://www.google.com/maps/search/?api=1")
    assert "query_place_id=p1" in result["maps_url"]
    assert "Trattoria%20Da%20Mario" in result["maps_url"]


def test_cuisine_is_passed_through(monkeypatch):
    calls = _install_fake_find(monkeypatch)

    response = client.get("/restaurants", params={"lat": 45.46, "lon": 9.19, "cuisine": "sushi"})

    assert response.status_code == 200
    assert calls[0][3] == "sushi"


def test_unknown_cuisine_is_422():
    response = client.get("/restaurants", params={"lat": 45.46, "lon": 9.19, "cuisine": "martian"})

    assert response.status_code == 422


def test_missing_coordinates_is_422():
    assert client.get("/restaurants", params={"lat": 45.46}).status_code == 422


def test_out_of_range_coordinates_is_422():
    assert client.get("/restaurants", params={"lat": 91, "lon": 9.19}).status_code == 422


def test_places_api_error_is_502(monkeypatch):
    def failing_find(lat, lon, limit, cuisine):
        raise PlacesAPIError("quota exceeded")

    monkeypatch.setattr(service, "find_best_restaurants", failing_find)

    response = client.get("/restaurants", params={"lat": 45.46, "lon": 9.19})

    assert response.status_code == 502


# --- /cuisines -----------------------------------------------------------

def test_cuisines_lists_every_cuisine():
    cuisines = client.get("/cuisines").json()["cuisines"]

    assert [c["key"] for c in cuisines] == list(CUISINES)
    assert {"key": "japanese", "label": "Japanese"} in cuisines


# --- /geocode ------------------------------------------------------------

def test_geocode_returns_coordinates(monkeypatch):
    found = {"lat": 45.4719, "lon": 9.1881, "address": "Via Brera, 10, Milano"}
    monkeypatch.setattr(service, "geocode", lambda address: found)

    response = client.get("/geocode", params={"address": "Via Brera 10"})

    assert response.status_code == 200
    assert response.json() == found


def test_geocode_not_found_is_404(monkeypatch):
    monkeypatch.setattr(service, "geocode", lambda address: None)

    assert client.get("/geocode", params={"address": "zzzz"}).status_code == 404


def test_geocode_api_error_is_502(monkeypatch):
    def failing(address):
        raise PlacesAPIError("quota exceeded")

    monkeypatch.setattr(service, "geocode", failing)

    assert client.get("/geocode", params={"address": "Duomo"}).status_code == 502


def test_geocode_rejects_empty_address():
    assert client.get("/geocode", params={"address": ""}).status_code == 422


# --- Page ----------------------------------------------------------------

def test_index_serves_the_page():
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Best Restaurant" in response.text


def test_health():
    assert client.get("/health").json() == {"status": "ok"}
