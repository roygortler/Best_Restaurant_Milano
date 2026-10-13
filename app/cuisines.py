"""The cuisines a user can filter by, and how each maps to Google's place types.

Keys are what the API and the web page use (`?cuisine=japanese`); each maps
to a display label and the Places API type to search for. Every type here
was checked against the live API around central Milan. To add a cuisine,
pick a type from Google's "Table A" restaurant types and add a row.
"""
CUISINES: dict[str, dict[str, str]] = {
    "italian": {"label": "Italian", "google_type": "italian_restaurant"},
    "pizza": {"label": "Pizza", "google_type": "pizza_restaurant"},
    "japanese": {"label": "Japanese", "google_type": "japanese_restaurant"},
    "sushi": {"label": "Sushi", "google_type": "sushi_restaurant"},
    "chinese": {"label": "Chinese", "google_type": "chinese_restaurant"},
    "korean": {"label": "Korean", "google_type": "korean_restaurant"},
    "thai": {"label": "Thai", "google_type": "thai_restaurant"},
    "indian": {"label": "Indian", "google_type": "indian_restaurant"},
    "middle_eastern": {"label": "Middle Eastern", "google_type": "middle_eastern_restaurant"},
    "mediterranean": {"label": "Mediterranean", "google_type": "mediterranean_restaurant"},
    "mexican": {"label": "Mexican", "google_type": "mexican_restaurant"},
    "seafood": {"label": "Seafood", "google_type": "seafood_restaurant"},
    "steak": {"label": "Steakhouse", "google_type": "steak_house"},
    "burger": {"label": "Burgers", "google_type": "hamburger_restaurant"},
    "vegetarian": {"label": "Vegetarian", "google_type": "vegetarian_restaurant"},
    "vegan": {"label": "Vegan", "google_type": "vegan_restaurant"},
    "brunch": {"label": "Brunch", "google_type": "brunch_restaurant"},
}

_LABEL_BY_GOOGLE_TYPE = {c["google_type"]: c["label"] for c in CUISINES.values()}


def label_for(primary_type: str | None) -> str:
    """Human-readable label for a Google primary type, e.g. for display.

    Covers types outside CUISINES too ("sandwich_shop" -> "Sandwich shop"),
    since a restaurant's primary type can be anything Google assigns.
    """
    if not primary_type:
        return "Restaurant"
    if primary_type in _LABEL_BY_GOOGLE_TYPE:
        return _LABEL_BY_GOOGLE_TYPE[primary_type]
    words = primary_type.removesuffix("_restaurant").replace("_", " ")
    return words.capitalize()
