"""Tests for the Places API wrapper - no real network calls or API key.

requests.post/get are monkeypatched so these run fully offline. What's
being checked is our own logic: that we build the right request and that
we normalize Google's response shape into ours.
"""
import os

os.environ.setdefault("GOOGLE_PLACES_API_KEY", "test-key")

from app import places_client
from app.config import get_settings


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def setup_function():
    # Settings are cached with lru_cache; clear it so each test can rely on
    # the env var set above without leaking state from other test modules.
    get_settings.cache_clear()


def test_nearby_search_normalizes_response(monkeypatch):
    fake_payload = {
        "places": [
            {
                "id": "place-123",
                "displayName": {"text": "Trattoria Milano"},
                "location": {"latitude": 45.4642, "longitude": 9.1900},
                "rating": 4.5,
                "userRatingCount": 210,
            }
        ]
    }

    captured_request = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured_request["url"] = url
        captured_request["headers"] = headers
        captured_request["body"] = json
        return _FakeResponse(200, fake_payload)

    monkeypatch.setattr(places_client.requests, "post", fake_post)

    results = places_client.nearby_search(lat=45.4642, lon=9.1900)

    assert results == [
        {
            "place_id": "place-123",
            "name": "Trattoria Milano",
            "rating": 4.5,
            "user_rating_count": 210,
            "lat": 45.4642,
            "lon": 9.1900,
        }
    ]
    assert captured_request["url"].endswith("places:searchNearby")
    assert captured_request["headers"]["X-Goog-Api-Key"] == "test-key"
    assert "places.rating" in captured_request["headers"]["X-Goog-FieldMask"]
    assert captured_request["body"]["locationRestriction"]["circle"]["radius"] == 1500


def test_nearby_search_raises_on_error_status(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse(403, {"error": "permission denied"})

    monkeypatch.setattr(places_client.requests, "post", fake_post)

    try:
        places_client.nearby_search(lat=45.46, lon=9.19)
        assert False, "expected PlacesAPIError"
    except places_client.PlacesAPIError:
        pass


def test_get_place_details_normalizes_response(monkeypatch):
    fake_payload = {
        "id": "place-123",
        "displayName": {"text": "Trattoria Milano"},
        "rating": 4.6,
        "userRatingCount": 215,
    }

    def fake_get(url, headers=None, timeout=None):
        assert url.endswith("place-123")
        return _FakeResponse(200, fake_payload)

    monkeypatch.setattr(places_client.requests, "get", fake_get)

    result = places_client.get_place_details("place-123")

    assert result["place_id"] == "place-123"
    assert result["rating"] == 4.6
    assert result["user_rating_count"] == 215
    # Nearby-search-only fields default sensibly when absent from details response
    assert result["lat"] is None
    assert result["lon"] is None
