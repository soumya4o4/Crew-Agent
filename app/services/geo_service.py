"""Maps lookups on OpenStreetMap services (free, no API key): reverse and forward geocoding (Nominatim), places around a
point (Overpass, with Nominatim as the fallback) and driving time (OSRM).

These are shared public servers, fine for a prototype: keep traffic low (we throttle and cache) and move to a paid maps
API (Google Places / Directions) before real launch. Coordinates are sent to these services, mention that in your privacy
text. Every method returns None / [] when the service is down or finds nothing; callers decide what to tell the user.
"""
import asyncio
import logging
import re
import time

import httpx

from app.core.geo import PLACE_CATEGORIES, haversine_m

logger = logging.getLogger(__name__)
UA = "CrewBuddy/0.1 (WhatsApp travel concierge prototype)"
NOMINATIM = "https://nominatim.openstreetmap.org"
OSRM = "https://router.project-osrm.org/route/v1/driving"
OVERPASS_HOSTS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
NOMINATIM_GAP_S = 1.1  # the Nominatim usage policy allows one request per second
TIMEOUT_S, OVERPASS_TIMEOUT_S = 8, 7  # a WhatsApp user is waiting: fail fast and fall back
PLACE_KINDS = {"place", "boundary"}  # when geocoding an area name, a suburb or town beats a park or bus stop of the same name
CACHE_TTL_S = 600

_gate = asyncio.Lock()
_last_nominatim = 0.0
_cache: dict[tuple, tuple[float, object]] = {}


def _cached(key: tuple):
    hit = _cache.get(key)
    return hit[1] if hit and time.monotonic() - hit[0] < CACHE_TTL_S else None


def _store(key: tuple, value):
    if len(_cache) > 500:
        _cache.clear()
    _cache[key] = (time.monotonic(), value)
    return value


def clean_text(text: str, limit: int = 30) -> str:
    """Letters, digits and spaces only: what we put into an Overpass query can never break out of it."""
    return re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip()[:limit]


def overpass_query(lat: float, lon: float, radius_m: int, filters: list[tuple[str, str]], text: str | None) -> str:
    around = f"(around:{radius_m},{lat},{lon})"
    parts = [f'nwr{around}["{k}"="{v}"]["name"];' for k, v in filters]
    if text := clean_text(text or ""):
        parts += [f'nwr{around}["name"~"{text}",i];', f'nwr{around}["cuisine"~"{text}",i]["name"];']
    return f"[out:json][timeout:20];({''.join(parts)});out center 60;"


def _place(name: str, lat: float, lon: float, origin: tuple[float, float], info: str = "", hours: str = "") -> dict:
    return {"name": name, "lat": lat, "lon": lon, "dist_m": haversine_m(origin[0], origin[1], lat, lon), "info": info, "hours": hours}


def _dedupe(places: list[dict], limit: int) -> list[dict]:
    seen, out = set(), []
    for p in sorted(places, key=lambda p: p["dist_m"]):
        key = (p["name"].lower(), round(p["lat"], 3), round(p["lon"], 3))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out[:limit]


def parse_overpass(data: dict, origin: tuple[float, float], limit: int) -> list[dict]:
    places = []
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        pos = el if "lat" in el else el.get("center")
        if not tags.get("name") or not pos:
            continue
        info = (tags.get("cuisine", "").replace("_", " ").replace(";", ", ")
                or tags.get("addr:street") or tags.get("amenity", "").replace("_", " "))
        places.append(_place(tags["name"], pos["lat"], pos["lon"], origin, info, tags.get("opening_hours", "")))
    return _dedupe(places, limit)


class GeoService:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport  # tests pass httpx.MockTransport

    def _http(self, timeout: float = TIMEOUT_S) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, headers={"User-Agent": UA}, transport=self._transport)

    async def _get_nominatim(self, path: str, params: dict) -> list | dict | None:
        global _last_nominatim
        async with _gate:  # one at a time, at least NOMINATIM_GAP_S apart
            wait = _last_nominatim + NOMINATIM_GAP_S - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with self._http() as http:
                    resp = await http.get(f"{NOMINATIM}/{path}", params={"format": "jsonv2", "accept-language": "en", **params})
                    resp.raise_for_status()
                    return resp.json()
            except Exception as exc:
                logger.warning("Nominatim %s failed: %s", path, type(exc).__name__)
                return None
            finally:
                _last_nominatim = time.monotonic()

    # ---------------------------------------------------------------- addresses
    async def reverse(self, lat: float, lon: float) -> str | None:
        """A short human label for a coordinate, like "Martand Chowk, Indore"."""
        key = ("rev", round(lat, 3), round(lon, 3))
        if (hit := _cached(key)) is not None:
            return hit
        data = await self._get_nominatim("reverse", {"lat": lat, "lon": lon, "zoom": 16})
        addr = (data or {}).get("address") if isinstance(data, dict) else None
        if not addr:
            return None
        local = addr.get("road") or addr.get("neighbourhood") or addr.get("suburb") or addr.get("hamlet") or ""
        town = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("state_district") or ""
        label = ", ".join(dict.fromkeys(x for x in (local, addr.get("suburb", ""), town) if x))
        return _store(key, label or None)

    async def geocode(self, query: str, near: tuple[float, float] | None = None) -> dict | None:
        """Find a place by typed name. Returns {lat, lon, name} or None. `near` only biases the order."""
        query = " ".join(query.split())[:100]
        if not query:
            return None
        key = ("geo", query.lower(), near and (round(near[0], 1), round(near[1], 1)))
        if (hit := _cached(key)) is not None:
            return hit
        params = {"q": query, "limit": 5, "countrycodes": "in,ae"}
        if near:
            params |= {"viewbox": f"{near[1] - 0.5},{near[0] + 0.5},{near[1] + 0.5},{near[0] - 0.5}"}
        data = await self._get_nominatim("search", params)
        if not data or not isinstance(data, list):
            return None
        top = next((r for r in data if r.get("category") in PLACE_KINDS), data[0])
        name = top.get("name") or top.get("display_name", "").split(",")[0]
        return _store(key, {"lat": float(top["lat"]), "lon": float(top["lon"]), "name": name})

    # ------------------------------------------------------------------- places
    async def places(self, lat: float, lon: float, category: str | None = None, text: str | None = None,
                     radius_m: int = 2500, limit: int = 9) -> list[dict]:
        """Closest first: [{name, lat, lon, dist_m, info, hours}]. A category key or free text (a name or a cuisine)."""
        filters = PLACE_CATEGORIES[category][2] if category in PLACE_CATEGORIES else []
        key = ("pl", round(lat, 3), round(lon, 3), category, (text or "").lower(), radius_m)
        if (hit := _cached(key)) is not None:
            return hit
        query = overpass_query(lat, lon, radius_m, filters, text)
        data = await self._overpass(query)
        if data is not None:
            return _store(key, parse_overpass(data, (lat, lon), limit))
        return _store(key, await self._places_from_nominatim(lat, lon, category, text, radius_m, limit))

    async def _overpass(self, query: str) -> dict | None:
        """Ask every Overpass server at once and take the first answer: the public ones are often busy."""
        async def ask(host: str) -> dict:
            async with self._http(OVERPASS_TIMEOUT_S) as http:
                resp = await http.post(host, data={"data": query})
                resp.raise_for_status()
                return resp.json()

        tasks = [asyncio.create_task(ask(host)) for host in OVERPASS_HOSTS]
        try:
            for finished in asyncio.as_completed(tasks):
                try:
                    return await finished
                except Exception as exc:
                    logger.warning("Overpass failed: %s", type(exc).__name__)
        finally:
            for task in tasks:
                task.cancel()
        return None

    async def _places_from_nominatim(self, lat, lon, category, text, radius_m, limit) -> list[dict]:
        """Fallback when Overpass is busy: a bounded text search around the point."""
        word = clean_text(text or "") or (PLACE_CATEGORIES[category][3] if category in PLACE_CATEGORIES else "")
        if not word:
            return []
        dlat, dlon = radius_m / 111_000, radius_m / 111_000
        data = await self._get_nominatim("search", {"q": word, "limit": 20, "bounded": 1, "countrycodes": "in,ae",
                                                    "viewbox": f"{lon - dlon},{lat + dlat},{lon + dlon},{lat - dlat}"})
        rows = data if isinstance(data, list) else []
        places = [_place(p.get("name") or p["display_name"].split(",")[0], float(p["lat"]), float(p["lon"]), (lat, lon),
                         p.get("type", "").replace("_", " ")) for p in rows]
        return _dedupe(places, limit)

    # -------------------------------------------------------------------- route
    async def route(self, origin: tuple[float, float], dest: tuple[float, float]) -> dict | None:
        """Driving distance and time without live traffic: {km, minutes}."""
        try:
            async with self._http() as http:
                resp = await http.get(f"{OSRM}/{origin[1]},{origin[0]};{dest[1]},{dest[0]}", params={"overview": "false"})
                resp.raise_for_status()
                route = resp.json()["routes"][0]
        except Exception as exc:
            logger.warning("OSRM route failed: %s", type(exc).__name__)
            return None
        return {"km": round(route["distance"] / 1000, 1), "minutes": max(1, round(route["duration"] / 60))}
