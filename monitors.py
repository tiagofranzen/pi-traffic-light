"""Consolidated background monitor threads: S-Bahn, Weather, Space Weather, Traffic, iRacing UDP."""
import logging
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from time import sleep
import socket
from typing import Optional

from traffic_light.config import api_config, location_config, timing_config, network_config

logger = logging.getLogger(__name__)

# ---------------- S-Bahn -----------------

def get_next_train_minutes(eva_number: str, client_id: str, client_secret: str) -> Optional[int]:
    headers = {"DB-Client-Id": client_id, "DB-Api-Key": client_secret, "accept": "application/xml"}
    now = datetime.now()
    all_stops = []
    for i in range(2):
        check_time = now + timedelta(hours=i)
        date, hour = check_time.strftime('%y%m%d'), check_time.strftime('%H')
        try:
            url = f"{api_config.DB_API_URL}/{eva_number}/{date}/{hour}"
            response = requests.get(url, headers=headers, timeout=timing_config.API_TIMEOUT)
            response.raise_for_status()
            if not response.content:
                continue
            root = ET.fromstring(response.content)
            all_stops.extend(root.findall('s'))
        except (requests.RequestException, ET.ParseError):
            return None
    if not all_stops:
        return None
    upcoming = []
    for stop in all_stops:
        try:
            dp = stop.find('.//dp')
            if dp is None:
                continue
            path_string = dp.get('ppth')
            departure_raw = dp.get('pt')
            if not path_string or not departure_raw:
                continue
            destination = path_string.split('|')[-1]
            if destination in location_config.OUTBOUND_DESTINATIONS:
                continue
            departure_dt = datetime.strptime(departure_raw, '%y%m%d%H%M')
            if departure_dt < now:
                continue
            minutes_until = int((departure_dt - now).total_seconds() / 60)
            upcoming.append(minutes_until)
        except Exception:
            continue
    return min(upcoming) if upcoming else None

def s_bahn_monitor(controller):
    if not api_config.is_s_bahn_enabled():
        logger.warning("S-Bahn Monitor disabled: missing credentials")
        return
    while controller.state.running:
        minutes = get_next_train_minutes(location_config.S_BAHN_EVA, api_config.DB_CLIENT_ID, api_config.DB_CLIENT_SECRET)
        with controller.state.lock:
            controller.state.s_bahn_minutes_away = minutes if minutes is not None else -1
        sleep(timing_config.S_BAHN_POLL_INTERVAL)

# ---------------- Weather -----------------

def fetch_weather_data() -> dict:
    url = api_config.WEATHER_API_URL_TEMPLATE.format(lat=location_config.WEATHER_LAT, lon=location_config.WEATHER_LON, api_key=api_config.OWM_API_KEY)
    try:
        r = requests.get(url, timeout=timing_config.API_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        temp = data.get('main', {}).get('temp')
        condition = data.get('weather', [{}])[0].get('main')
        if temp is None or not condition:
            return {}
        return {'temp': temp, 'condition': condition}
    except Exception:
        return {}

def weather_monitor(controller):
    if not api_config.is_weather_enabled():
        logger.warning("Weather Monitor disabled: OWM_API_KEY not set")
        return
    while controller.state.running:
        weather = fetch_weather_data()
        with controller.state.lock:
            controller.state.weather_status = weather
        sleep(timing_config.WEATHER_POLL_INTERVAL)

# ---------------- Space Weather -----------------

def fetch_space_weather_data() -> dict:
    try:
        r = requests.get(api_config.SPACE_WEATHER_URL, timeout=timing_config.API_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if not data:
            return {}
        latest = data[-1]
        kp = int(float(latest[1]))
        condition = 'Quiet'
        if kp >= 5:
            condition = 'Storm'
        elif kp == 4:
            condition = 'Active'
        return {'kp_index': kp, 'condition': condition}
    except Exception:
        return {}

def space_weather_monitor(controller):
    while controller.state.running:
        space = fetch_space_weather_data()
        with controller.state.lock:
            controller.state.space_weather_status = space
        sleep(timing_config.SPACE_POLL_INTERVAL)

# ---------------- Traffic -----------------

def fetch_traffic_data() -> dict:
    if not api_config.is_traffic_enabled():
        return {}
    delays = []
    commute_text = 'N/A'
    for route in location_config.TRAFFIC_ROUTES:
        try:
            params = { 'origin': route['origin'], 'destination': route['destination'], 'key': api_config.GOOGLE_MAPS_API_KEY, 'departure_time': 'now' }
            r = requests.get(api_config.GOOGLE_DIRECTIONS_URL, params=params, timeout=timing_config.API_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            if data.get('status') != 'OK':
                continue
            leg = data['routes'][0]['legs'][0]
            base = leg['duration']['value']
            traffic = leg.get('duration_in_traffic', leg['duration'])['value']
            if base > 0:
                delays.append(((traffic - base)/base)*100)
            if route['name'] == 'commute':
                commute_text = leg.get('duration_in_traffic', leg['duration'])['text']
        except Exception:
            continue
    if delays:
        avg = sum(delays)/len(delays)
        return {'avg_delay': avg, 'commute_time': commute_text}
    return {}

def traffic_monitor(controller):
    if not api_config.is_traffic_enabled():
        logger.warning("Traffic Monitor disabled: GOOGLE_MAPS_API_KEY not set")
        return
    while controller.state.running:
        traffic = fetch_traffic_data()
        with controller.state.lock:
            controller.state.traffic_status = traffic
        sleep(timing_config.TRAFFIC_POLL_INTERVAL)

# ---------------- iRacing UDP -----------------

def iracing_udp_listener(controller):
    host, port = network_config.IRACING_UDP_HOST, network_config.IRACING_UDP_PORT
    valid = {'red','yellow','green','black','green-yellow','all_on'}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind((host, port))
            sock.settimeout(1.0)
            while controller.state.running:
                try:
                    data, _ = sock.recvfrom(1024)
                    color = data.decode('utf-8').strip()
                    if color in valid:
                        with controller.state.lock:
                            controller.state.iracing_light_status = color
                except socket.timeout:
                    continue
                except Exception:
                    continue
    except Exception as e:
        logger.error(f"iRacing listener error: {e}")
