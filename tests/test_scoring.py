"""Tests for the scoring pipeline - pure functions, no mocking needed at all."""
import pytest

from app import scoring


def test_haversine_distance_km_same_point_is_zero():
    assert scoring.haversine_distance_km(45.4642, 9.1900, 45.4642, 9.1900) == pytest.approx(0.0)


def test_haversine_distance_km_one_degree_latitude_is_about_111km():
    # A well-known reference distance, useful as a sanity check on the formula itself.
    distance = scoring.haversine_distance_km(0.0, 0.0, 1.0, 0.0)
    assert distance == pytest.approx(111.19, abs=0.5)


def test_bayesian_rating_pulls_low_review_count_toward_global_mean():
    # 2 reviews of 5.0 stars, against a prior of 4.0 - 1% trust, so it
    # should land almost exactly on 4.0.
    result = scoring.bayesian_rating(rating=5.0, review_count=2, global_mean=4.0, m=200)
    assert result == pytest.approx(4.01)


def test_bayesian_rating_trust_grows_linearly_below_threshold():
    # 100 of 200 reviews -> half trust -> halfway between 4.0 and 4.8.
    result = scoring.bayesian_rating(rating=4.8, review_count=100, global_mean=4.0, m=200)
    assert result == pytest.approx(4.4)


def test_bayesian_rating_fully_trusts_at_threshold():
    result = scoring.bayesian_rating(rating=4.8, review_count=200, global_mean=4.0, m=200)
    assert result == pytest.approx(4.8)


def test_bayesian_rating_review_count_stops_mattering_past_threshold():
    at_threshold = scoring.bayesian_rating(rating=4.5, review_count=200, global_mean=4.0, m=200)
    way_past = scoring.bayesian_rating(rating=4.5, review_count=5000, global_mean=4.0, m=200)
    assert at_threshold == way_past == pytest.approx(4.5)


def test_bayesian_rating_zero_reviews_returns_global_mean_without_crashing():
    # Google omits `rating` for places with no reviews - rating=None must
    # not blow up even though it's still technically multiplied by zero.
    result = scoring.bayesian_rating(rating=None, review_count=0, global_mean=4.1, m=200)
    assert result == 4.1


def test_distance_score_decreases_as_distance_increases():
    close = scoring.distance_score(0.2, max_distance_km=1.5)
    far = scoring.distance_score(1.2, max_distance_km=1.5)
    assert close > far


def test_distance_score_is_one_at_zero_distance():
    assert scoring.distance_score(0.0, max_distance_km=1.5) == 1.0


def test_distance_score_is_zero_at_and_beyond_max_distance():
    assert scoring.distance_score(1.5, max_distance_km=1.5) == 0.0
    assert scoring.distance_score(3.0, max_distance_km=1.5) == 0.0


def test_distance_score_drops_faster_close_to_the_user():
    # Log curve: the first 200m costs more than 200m further out.
    near_drop = scoring.distance_score(0.0, 1.5) - scoring.distance_score(0.2, 1.5)
    far_drop = scoring.distance_score(1.0, 1.5) - scoring.distance_score(1.2, 1.5)
    assert near_drop > far_drop


def test_rating_score_maps_star_scale_onto_zero_to_one():
    assert scoring.rating_score(1.0) == 0.0
    assert scoring.rating_score(3.0) == 0.5
    assert scoring.rating_score(5.0) == 1.0


def test_rank_restaurants_score_does_not_depend_on_other_candidates():
    # Regression: scores used to be min-max normalized across the candidate
    # set, so adding an unrelated place changed everyone else's score.
    place = {"place_id": "a", "name": "A", "rating": 4.3, "user_rating_count": 200,
             "lat": 45.4650, "lon": 9.1910}
    other = {"place_id": "b", "name": "B", "rating": 3.1, "user_rating_count": 40,
             "lat": 45.4700, "lon": 9.2000}

    alone = scoring.rank_restaurants([place], user_lat=45.4642, user_lon=9.1900)
    together = scoring.rank_restaurants([place, other], user_lat=45.4642, user_lon=9.1900)

    score_together = next(r for r in together if r.place_id == "a").final_score
    assert alone[0].final_score == pytest.approx(score_together)


def test_rank_restaurants_empty_candidates_returns_empty_list():
    assert scoring.rank_restaurants([], user_lat=45.4642, user_lon=9.1900) == []


def test_rank_restaurants_rejects_weights_that_dont_sum_to_one():
    with pytest.raises(ValueError):
        scoring.rank_restaurants(
            [{"place_id": "a", "name": "A", "rating": 4.0, "user_rating_count": 100, "lat": 45.46, "lon": 9.19}],
            user_lat=45.4642, user_lon=9.1900,
            weight_rating=0.5, weight_distance=0.6,
        )


def test_rank_restaurants_established_place_beats_new_place_at_equal_distance():
    """The whole point of the Bayesian adjustment: a new restaurant with one
    perfect review should NOT outrank an established one with hundreds of
    solid reviews, when both are equally close to the user."""
    user_lat, user_lon = 45.4642, 9.1900
    same_spot_lat, same_spot_lon = 45.4650, 9.1910  # identical distance for both

    candidates = [
        {
            "place_id": "new-place", "name": "Brand New Trattoria",
            "rating": 5.0, "user_rating_count": 2,
            "lat": same_spot_lat, "lon": same_spot_lon,
        },
        {
            "place_id": "established-place", "name": "Established Osteria",
            "rating": 4.5, "user_rating_count": 600,
            "lat": same_spot_lat, "lon": same_spot_lon,
        },
    ]

    ranked = scoring.rank_restaurants(candidates, user_lat=user_lat, user_lon=user_lon)

    assert ranked[0].place_id == "established-place"


def test_rank_restaurants_closer_place_can_beat_higher_rated_farther_place():
    """Sanity check that distance actually matters: a slightly lower-rated
    place right next to the user should be able to outrank a marginally
    better-rated place that's much farther away, given default weights."""
    user_lat, user_lon = 45.4642, 9.1900

    candidates = [
        {
            "place_id": "far-place", "name": "Great But Far",
            "rating": 4.8, "user_rating_count": 500,
            "lat": 45.4642, "lon": 9.2200,  # ~1.9km away
        },
        {
            "place_id": "near-place", "name": "Good And Close",
            "rating": 4.4, "user_rating_count": 500,
            "lat": 45.4645, "lon": 9.1905,  # a few dozen meters away
        },
    ]

    ranked = scoring.rank_restaurants(candidates, user_lat=user_lat, user_lon=user_lon)

    assert ranked[0].place_id == "near-place"
