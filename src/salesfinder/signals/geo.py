"""US state centroids and nearest-warehouse lookup (pure)."""

import math
from collections.abc import Sequence

from salesfinder.catalog import Warehouse

STATE_CENTROIDS: dict[str, tuple[float, float]] = {
    "AL": (32.81, -86.79), "AK": (63.35, -152.84), "AZ": (34.27, -111.66),
    "AR": (34.89, -92.44), "CA": (37.18, -119.47), "CO": (38.99, -105.55),
    "CT": (41.62, -72.73), "DE": (38.99, -75.51), "DC": (38.90, -77.03),
    "FL": (28.63, -82.45), "GA": (32.64, -83.44), "HI": (20.29, -156.37),
    "ID": (44.35, -114.61), "IL": (40.04, -89.20), "IN": (39.89, -86.28),
    "IA": (42.07, -93.50), "KS": (38.49, -98.38), "KY": (37.53, -85.30),
    "LA": (31.07, -92.00), "ME": (45.37, -69.24), "MD": (39.05, -76.79),
    "MA": (42.26, -71.81), "MI": (44.35, -85.41), "MN": (46.28, -94.31),
    "MS": (32.74, -89.67), "MO": (38.36, -92.46), "MT": (47.05, -109.63),
    "NE": (41.54, -99.80), "NV": (39.33, -116.63), "NH": (43.68, -71.58),
    "NJ": (40.19, -74.67), "NM": (34.41, -106.11), "NY": (42.95, -75.53),
    "NC": (35.56, -79.39), "ND": (47.45, -100.47), "OH": (40.29, -82.79),
    "OK": (35.59, -97.49), "OR": (43.93, -120.56), "PA": (40.88, -77.80),
    "RI": (41.68, -71.51), "SC": (33.91, -80.90), "SD": (44.44, -100.23),
    "TN": (35.86, -86.35), "TX": (31.48, -99.33), "UT": (39.31, -111.67),
    "VT": (44.07, -72.67), "VA": (37.52, -78.85), "WA": (47.38, -120.45),
    "WV": (38.64, -80.62), "WI": (44.62, -89.99), "WY": (42.99, -107.55),
}  # fmt: skip

_EARTH_RADIUS_MILES = 3958.8


def haversine_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_MILES * math.asin(min(1.0, math.sqrt(h)))


def nearest_warehouse(state: str, warehouses: Sequence[Warehouse]) -> tuple[Warehouse, int]:
    """The warehouse closest to the state's centroid and the rounded distance in miles.
    Raises KeyError for an unknown state and ValueError when there are no warehouses."""
    centroid = STATE_CENTROIDS[state]
    if not warehouses:
        raise ValueError("no warehouses configured")
    best = min(warehouses, key=lambda w: haversine_miles(centroid, (w.lat, w.lon)))
    return best, round(haversine_miles(centroid, (best.lat, best.lon)))
