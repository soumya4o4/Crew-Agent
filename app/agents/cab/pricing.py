"""Cab fare maths: distance, peak/night multipliers, airport fee. Pure functions, easy to test."""
import math
from datetime import datetime

AIRPORT_FEE = 100
PEAK_HOURS = (range(8, 11), range(17, 21))


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance x1.3 as a stand-in for road distance."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return max(1.0, round(2 * r * math.asin(math.sqrt(a)) * 1.3, 1))


def multiplier(when: datetime) -> tuple[float, str]:
    h = when.hour
    if any(h in r for r in PEAK_HOURS):
        return 1.2, "Peak hours (+20%)"
    if h >= 23 or h < 5:
        return 1.15, "Night charge (+15%)"
    return 1.0, ""


def quote(rate: dict, km: float, when: datetime, airport: bool) -> dict:
    """rate is a cab_fares row. Returns the fare and what went into it."""
    mult, note = multiplier(when)
    raw = max(rate["min_fare"], rate["base_fare"] + float(rate["per_km"]) * km)
    fee = AIRPORT_FEE if airport else 0
    fare = int(round((raw * mult + fee) / 5.0) * 5)
    return {"fare": fare, "km": km, "note": note, "airport_fee": fee}


def eta_min(city: str, vehicle: str) -> int:
    """Stable fake 'driver is N min away' so the same screen doesn't flicker between taps."""
    return 3 + (sum(map(ord, city + vehicle)) % 7)
