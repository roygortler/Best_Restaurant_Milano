"""Tests for the Places API wrapper - no real network calls or API key.

requests.post/get are monkeypatched so these run fully offline. What's
being checked is our own logic: that we build the right request and that
we normalize Google's response shape into ours.
"""
import os

os.environ.setdefault("GOOGLE_PLACES_API_KEY", "test-key")

import pytest

from app import places_client
from app.config import get_settings
from app.scoring import haversine_distance_km


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


def _raw_place(place_id: str) -> dict:
    return {
        "id": place_id,
        "displayName": {"text": f"Trattoria {place_id}"},
        "location": {"latitude": 45.4642, "longitude": 9.1900},
        "rating": 4.5,
        "userRatingCount": 210,
    }


def _install_pages(monkeypatch, pages: list[dict]) -> list[dict]:
    """Serve `pages` in order from requests.post; returns the captured requests."""
    captured = []

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.append({"url": url, "headers": headers, "body": json})
        return _FakeResponse(200, pages[len(captured) - 1])

    monkeypatch.setattr(places_client.requests, "post", fake_post)
    return captured


def test_search_restaurants_normalizes_response(monkeypatch):
    _install_pages(monkeypatch, [{"places": [_raw_place("place-123")]}])

    results = places_client.search_restaurants(lat=45.4642, lon=9.1900)

    assert results == [
        {
            "place_id": "place-123",
            "name": "Trattoria place-123",
            "rating": 4.5,
            "user_rating_count": 210,
            "lat": 45.4642,
            "lon": 9.1900,
        }
    ]


def test_search_restaurants_builds_text_search_request(monkeypatch):
    captured = _install_pages(monkeypatch, [{"places": []}])

    places_client.search_restaurants(lat=45.4642, lon=9.1900, radius_m=1500, min_rating=4.2)

    request = captured[0]
    assert request["url"].endswith("places:searchText")
    assert request["headers"]["X-Goog-Api-Key"] == "test-key"
    field_mask = request["headers"]["X-Goog-FieldMask"]
    assert "places.rating" in field_mask
    assert "nextPageToken" in field_mask  # without it Google never pages
    body = request["body"]
    assert body["includedType"] == "restaurant"
    assert body["strictTypeFiltering"] is True
    assert body["minRating"] == 4.2
    assert body["pageSize"] == 20
    assert "pageToken" not in body


def test_bounding_box_extends_radius_in_each_direction():
    box = places_client._bounding_box(45.4642, 9.1900, 1500)

    north = haversine_distance_km(45.4642, 9.1900, box["high"]["latitude"], 9.1900)
    east = haversine_distance_km(45.4642, 9.1900, 45.4642, box["high"]["longitude"])
    assert north == pytest.approx(1.5, abs=0.01)
    assert east == pytest.approx(1.5, abs=0.01)
    assert box["low"]["latitude"] < 45.4642 < box["high"]["latitude"]
    assert box["low"]["longitude"] < 9.1900 < box["high"]["longitude"]


def test_search_restaurants_follows_page_tokens(monkeypatch):
    captured = _install_pages(monkeypatch, [
        {"places": [_raw_place("a")], "nextPageToken": "t1"},
        {"places": [_raw_place("b")], "nextPageToken": "t2"},
        {"places": [_raw_place("c")]},
    ])

    results = places_client.search_restaurants(lat=45.4642, lon=9.1900, max_results=60)

    assert [r["place_id"] for r in results] == ["a", "b", "c"]
    assert [c["body"].get("pageToken") for c in captured] == [None, "t1", "t2"]
    # Google rejects page requests whose other params differ from page one.
    first = {k: v for k, v in captured[0]["body"].items()}
    second = {k: v for k, v in captured[1]["body"].items() if k != "pageToken"}
    assert first == second


def test_search_restaurants_single_page_when_no_token(monkeypatch):
    # A quiet area: Google sends no nextPageToken, so we pay for one call.
    captured = _install_pages(monkeypatch, [{"places": [_raw_place("a")]}])

    places_client.search_restaurants(lat=45.4642, lon=9.1900, max_results=60)

    assert len(captured) == 1


def test_search_restaurants_never_requests_more_than_three_pages(monkeypatch):
    pages = [{"places": [_raw_place(f"p{i}")], "nextPageToken": f"t{i}"} for i in range(5)]
    captured = _install_pages(monkeypatch, pages)

    places_client.search_restaurants(lat=45.4642, lon=9.1900, max_results=60)

    assert len(captured) == 3


def test_search_restaurants_stops_once_max_results_reached(monkeypatch):
    first_page = {"places": [_raw_place(f"p{i}") for i in range(20)], "nextPageToken": "t1"}
    captured = _install_pages(monkeypatch, [first_page, first_page])

    results = places_client.search_restaurants(lat=45.4642, lon=9.1900, max_results=15)

    assert len(captured) == 1
    assert len(results) == 15


def test_search_restaurants_dedupes_across_pages(monkeypatch):
    _install_pages(monkeypatch, [
        {"places": [_raw_place("a"), _raw_place("b")], "nextPageToken": "t1"},
        {"places": [_raw_place("b"), _raw_place("c")]},
    ])

    results = places_client.search_restaurants(lat=45.4642, lon=9.1900, max_results=60)

    assert [r["place_id"] for r in results] == ["a", "b", "c"]


def test_search_restaurants_raises_on_error_status(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResponse(403, {"error": "permission denied"})

    monkeypatch.setattr(places_client.requests, "post", fake_post)

    with pytest.raises(places_client.PlacesAPIError):
        places_client.search_restaurants(lat=45.46, lon=9.19)


def test_search_restaurants_raises_if_a_later_page_fails(monkeypatch):
    responses = [_FakeResponse(200, {"places": [_raw_place("a")], "nextPageToken": "t1"}),
                 _FakeResponse(500, {"error": "backend error"})]

    def fake_post(url, headers=None, json=None, timeout=None):
        return responses.pop(0)

    monkeypatch.setattr(places_client.requests, "post", fake_post)

    with pytest.raises(places_client.PlacesAPIError):
        places_client.search_restaurants(lat=45.46, lon=9.19, max_results=60)


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
    # Search-only fields default sensibly when absent from details response
    assert result["lat"] is None
    assert result["lon"] is None
