"""Commute tile (S-Bahn, Stau) and its data sources: DB timetables, MVG, TomTom/Google."""
from __future__ import annotations
import os
import logging
from time import time
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple
from .config import API_TIMEOUT
from .net import _poll, http

S_BAHN_POLL_INTERVAL = 30.0
S_BAHN_STALE_SECONDS = 300.0   # keep counting down from the last timetable this long when fetches fail
TRAFFIC_POLL_INTERVAL = 600.0
TRANSIT_POLL_INTERVAL = 600.0
S_BAHN_EVA = os.getenv("S_BAHN_EVA", "").strip()  # your station's EVA number; S-Bahn mode is off without it

# Trains ending at these stations (comma-separated) go the wrong way and are ignored.
OUTBOUND_DESTINATIONS: Tuple[str, ...] = tuple(
    d.strip() for d in os.getenv("S_BAHN_SKIP_DESTINATIONS", "").split(",") if d.strip()
)

# Home -> work, for Stau and the public-transport commute; addresses are
# geocoded via TomTom and passed to MVG (Munich transit authority, free, no
# key). Without a home address there are no routes.
MVG_ROUTES_URL = "https://www.mvg.de/api/bgw-pt/v3/routes"
COMMUTE_ORIGIN = os.getenv("COMMUTE_ORIGIN", "").strip()
COMMUTE_DESTINATION = os.getenv("COMMUTE_DESTINATION", "").strip()
TRAFFIC_ROUTES: Tuple[dict, ...] = (
    *([{"name": "commute","origin": COMMUTE_ORIGIN,"destination": COMMUTE_DESTINATION}] if COMMUTE_ORIGIN and COMMUTE_DESTINATION else []),
    *([{"name": "center","origin": COMMUTE_ORIGIN,"destination": "Marienplatz, Munich, Germany"},
       {"name": "north","origin": COMMUTE_ORIGIN,"destination": "BMW Welt, Munich, Germany"}] if COMMUTE_ORIGIN else []),
)

# API keys (env)
DB_CLIENT_ID = os.getenv("DB_CLIENT_ID", "")
DB_CLIENT_SECRET = os.getenv("DB_CLIENT_SECRET", "")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
TOMTOM_API_KEY = os.getenv("TOMTOM_API_KEY", "")

# API URLs
DB_API_URL = "https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1"
GOOGLE_DIRECTIONS_URL = "https://maps.googleapis.com/maps/api/directions/json"
TOMTOM_GEOCODE_URL = "https://api.tomtom.com/search/2/geocode/{query}.json"
TOMTOM_ROUTING_URL = "https://api.tomtom.com/routing/1/calculateRoute/{loc}/json"

# In-process geocode cache for TomTom (address -> (lat, lon))
_TOMTOM_GEOCODE_CACHE: Dict[str, Tuple[float, float]] = {}

def handle_s_bahn_mode(controller, elapsed: float) -> Optional[float]:
    mins = controller.state.s_bahn_minutes_away; c = controller.state.current_color
    if mins == -1: controller.set_light_state('red' if c != 'red' else 'off'); return 0.5
    elif mins < 9: controller.set_light_state('red')
    elif mins == 9: controller.set_light_state('yellow' if c != 'yellow' else 'off'); return 0.5
    elif mins <= 12: controller.set_light_state('yellow')
    else: controller.set_light_state('green')

def handle_stau_mode(controller, elapsed: float) -> Optional[float]:
    delay = controller.state.traffic_status.get('avg_delay'); c = controller.state.current_color
    if delay is None: controller.set_light_state('red' if c != 'red' else 'off'); return 0.5
    elif delay > 45: controller.set_light_state('red')
    elif delay > 20: controller.set_light_state('yellow')
    else: controller.set_light_state('green')

def _db_get(path: str) -> Optional[bytes]:
    headers = {"DB-Client-Id": DB_CLIENT_ID, "DB-Api-Key": DB_CLIENT_SECRET, "accept": "application/xml"}
    try:
        r = http().get(f"{DB_API_URL}/{path}", headers=headers, timeout=API_TIMEOUT)
        r.raise_for_status()
        return r.content
    except Exception as e:
        logging.debug(f"DB timetable {path} failed: {e}")
        return None

def _fetch_departures() -> Optional[List[datetime]]:
    """Upcoming departures in our direction, with live times and without cancelled trains.
    None if the timetable couldn't be fetched at all."""
    now = datetime.now(); stops = []; got_plan = False
    for i in range(2):  # this hour and the next
        t = now + timedelta(hours=i)
        content = _db_get(f"plan/{S_BAHN_EVA}/{t.strftime('%y%m%d')}/{t.strftime('%H')}")
        if content is None:
            continue  # keep the other hour
        got_plan = True
        if content:
            try: stops.extend(ET.fromstring(content).findall('s'))
            except ET.ParseError: pass
    if not got_plan:
        return None
    # Live changes for the station: new departure time (ct) and cancellation (cs="c"), matched by stop id.
    changes: Dict[str, ET.Element] = {}
    content = _db_get(f"fchg/{S_BAHN_EVA}")
    if content:
        try:
            for s in ET.fromstring(content).findall('s'):
                dp = s.find('dp')
                if dp is not None and s.get('id'):
                    changes[s.get('id')] = dp
        except ET.ParseError:
            pass
    departures = []
    for s in stops:
        dp = s.find('.//dp')
        if dp is None or not dp.get('ppth') or not dp.get('pt'):
            continue
        if dp.get('ppth').split('|')[-1] in OUTBOUND_DESTINATIONS:
            continue
        live = changes.get(s.get('id'))
        if live is not None and live.get('cs') == 'c':
            continue  # cancelled
        try:
            dt = datetime.strptime((live.get('ct') if live is not None and live.get('ct') else dp.get('pt')), '%y%m%d%H%M')
        except ValueError:
            continue
        departures.append(dt)
    return sorted(departures)

def s_bahn_monitor(controller):
    if not (DB_CLIENT_ID and DB_CLIENT_SECRET and S_BAHN_EVA):
        logging.warning("S-Bahn disabled: credentials or S_BAHN_EVA missing")
        return
    departures: List[datetime] = []; fetched_at = 0.0
    while controller.state.running:
        fresh = _fetch_departures()
        if fresh is not None:
            departures, fetched_at = fresh, time()
        elif time() - fetched_at > S_BAHN_STALE_SECONDS:
            departures = []  # too old to trust (cancellations, delays)
        # Minutes from the stored times, so a failed fetch doesn't freeze or blank the countdown.
        now = datetime.now()
        upcoming = [int((d - now).total_seconds() / 60) for d in departures if d >= now]
        with controller.state.lock:
            controller.state.s_bahn_minutes_away = min(upcoming) if upcoming else -1
        controller._stop.wait(timeout=S_BAHN_POLL_INTERVAL)

def _commute_route() -> Optional[dict]:
    for route in TRAFFIC_ROUTES:
        if route.get('name') == 'commute':
            return route
    return TRAFFIC_ROUTES[0] if TRAFFIC_ROUTES else None

def _fetch_transit_commute() -> Dict:
    route = _commute_route()
    if not route:
        return {}
    origin_coord = _tomtom_geocode(route['origin'])
    dest_coord = _tomtom_geocode(route['destination'])
    if not origin_coord or not dest_coord:
        return {}
    try:
        params = {
            'originLatitude': origin_coord[0],
            'originLongitude': origin_coord[1],
            'destinationLatitude': dest_coord[0],
            'destinationLongitude': dest_coord[1],
            'routingDateTimeIsArrival': 'false',
            'transportTypes': 'SCHIFF,UBAHN,TRAM,BUS,SBAHN,REGIONAL_BUS,BAHN',
        }
        r = http().get(MVG_ROUTES_URL, params=params, timeout=API_TIMEOUT)
        r.raise_for_status()
        routes_data = r.json() or []
        if not routes_data:
            return {}
        best = routes_data[0]
        parts = best.get('parts') or []
        if not parts:
            return {}
        dep_raw = (parts[0].get('from') or {}).get('plannedDeparture')
        arr_raw = (parts[-1].get('to') or {}).get('plannedDeparture')
        # MVG uses plannedDeparture on the 'to' node of the last part as arrival time.
        if not dep_raw or not arr_raw:
            return {}
        dep = datetime.fromisoformat(dep_raw.replace('Z', '+00:00'))
        arr = datetime.fromisoformat(arr_raw.replace('Z', '+00:00'))
        minutes = max(0, int(round((arr - dep).total_seconds() / 60.0)))
        # Count transport legs (ignore walking/PEDESTRIAN).
        pt_parts = [p for p in parts if ((p.get('line') or {}).get('transportType') or '').upper() != 'PEDESTRIAN']
        transfers = max(0, len(pt_parts) - 1)
        return {
            'commute_minutes': minutes,
            'transfers': transfers,
        }
    except Exception as e:
        logging.debug(f"MVG transit fetch failed: {e}")
        return {}

def transit_monitor(controller):
    if not TOMTOM_API_KEY:
        logging.warning("Transit commute disabled: TOMTOM_API_KEY missing (used for geocoding)")
        return
    _poll(controller, 'Transit', _fetch_transit_commute, TRANSIT_POLL_INTERVAL,
          lambda v: setattr(controller.state, 's_bahn_transit_status', v))

def _format_minutes(seconds: float) -> str:
    mins = int(round(seconds / 60.0))
    if mins < 60:
        return f"{mins} min"
    h, m = divmod(mins, 60)
    return f"{h} h {m} min"

def _tomtom_geocode(address: str) -> Optional[Tuple[float, float]]:
    cached = _TOMTOM_GEOCODE_CACHE.get(address)
    if cached is not None:
        return cached
    try:
        from urllib.parse import quote
        url = TOMTOM_GEOCODE_URL.format(query=quote(address))
        r = http().get(url, params={'key': TOMTOM_API_KEY, 'limit': 1}, timeout=API_TIMEOUT)
        r.raise_for_status()
        results = r.json().get('results') or []
        if not results:
            return None
        pos = results[0].get('position') or {}
        lat = pos.get('lat'); lon = pos.get('lon')
        if lat is None or lon is None:
            return None
        coord = (float(lat), float(lon))
        _TOMTOM_GEOCODE_CACHE[address] = coord
        return coord
    except Exception as e:
        logging.debug(f"TomTom geocode failed for {address!r}: {e}")
        return None

def _fetch_traffic_tomtom() -> Dict:
    if not TOMTOM_API_KEY:
        return {}
    delays: List[float] = []
    commute_text = 'N/A'
    for route in TRAFFIC_ROUTES:
        origin = _tomtom_geocode(route['origin'])
        dest = _tomtom_geocode(route['destination'])
        if not origin or not dest:
            continue
        loc = f"{origin[0]},{origin[1]}:{dest[0]},{dest[1]}"
        try:
            url = TOMTOM_ROUTING_URL.format(loc=loc)
            params = {
                'key': TOMTOM_API_KEY,
                'traffic': 'true',
                'travelMode': 'car',
                'routeType': 'fastest',
                'computeTravelTimeFor': 'all',
            }
            r = http().get(url, params=params, timeout=API_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            routes = data.get('routes') or []
            if not routes:
                continue
            summary = routes[0].get('summary') or {}
            # travelTimeInSeconds already reflects live traffic;
            # noTrafficTravelTimeInSeconds is the free-flow baseline.
            traffic_s = summary.get('travelTimeInSeconds')
            base_s = summary.get('noTrafficTravelTimeInSeconds') or summary.get('historicTrafficTravelTimeInSeconds')
            if not traffic_s or not base_s:
                continue
            if base_s > 0:
                delays.append(((traffic_s - base_s) / base_s) * 100.0)
            if route['name'] == 'commute':
                commute_text = _format_minutes(traffic_s)
        except Exception as e:
            logging.debug(f"TomTom routing failed for {route['name']}: {e}")
            continue
    if delays:
        return {'avg_delay': sum(delays) / len(delays), 'commute_time': commute_text}
    return {}

def _fetch_traffic_google() -> Dict:
    if not GOOGLE_MAPS_API_KEY:
        return {}
    delays = []; commute_text = 'N/A'
    for route in TRAFFIC_ROUTES:
        try:
            params = { 'origin': route['origin'], 'destination': route['destination'], 'key': GOOGLE_MAPS_API_KEY, 'departure_time': 'now' }
            r = http().get(GOOGLE_DIRECTIONS_URL, params=params, timeout=API_TIMEOUT); r.raise_for_status(); data = r.json()
            if data.get('status') != 'OK': continue
            leg = data['routes'][0]['legs'][0]; base = leg['duration']['value']; traffic_val = leg.get('duration_in_traffic', leg['duration'])['value']
            if base > 0: delays.append(((traffic_val - base)/base)*100)
            if route['name'] == 'commute': commute_text = leg.get('duration_in_traffic', leg['duration'])['text']
        except Exception: continue
    if delays: return {'avg_delay': sum(delays)/len(delays), 'commute_time': commute_text}
    return {}

def _fetch_traffic() -> Dict:
    # Prefer TomTom (free tier); fall back to Google if still configured.
    if TOMTOM_API_KEY:
        result = _fetch_traffic_tomtom()
        if result:
            return result
    return _fetch_traffic_google()

def traffic_monitor(controller):
    if not (TOMTOM_API_KEY or GOOGLE_MAPS_API_KEY):
        logging.warning("Traffic disabled: set TOMTOM_API_KEY (free) or GOOGLE_MAPS_API_KEY")
        return
    _poll(controller, 'Traffic', _fetch_traffic, TRAFFIC_POLL_INTERVAL,
          lambda v: setattr(controller.state, 'traffic_status', v))
