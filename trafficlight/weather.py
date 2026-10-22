"""Weather tile (Temperature, Rain, UV, Biergarten, Space) and its data sources."""
from __future__ import annotations
import os
import logging
from datetime import datetime, timedelta
import requests
from typing import Dict, Optional, Tuple
from .config import API_TIMEOUT
from .net import _poll

SPACE_POLL_INTERVAL = 900.0
ENVIRONMENT_POLL_INTERVAL = 600.0   # also feeds Temperature and Rain, so keep it fresh

# Temperature (Weather sub-mode): green inside the comfortable band, yellow
# in the outer band, red beyond it.
TEMP_GREEN_RANGE = (15.0, 26.0)
TEMP_YELLOW_RANGE = (5.0, 30.0)

# Rain (Weather sub-mode), from the Open-Meteo hourly forecast: an hour counts
# as rainy at this much precipitation (mm) or this probability (%).
RAIN_MM = 0.2
RAIN_PROBABILITY = 60
RAIN_RED_MINUTES = 60              # rain this soon (or now) -> red
RAIN_YELLOW_MINUTES = 180          # rain within this -> yellow, else green

# Where the weather is read for - from the environment, so your location stays out of the repo.
WEATHER_LAT = os.getenv("WEATHER_LAT", "48.137").strip()   # default: Munich centre
WEATHER_LON = os.getenv("WEATHER_LON", "11.575").strip()
SPACE_WEATHER_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

def handle_biergarten_mode(controller, elapsed: float) -> Optional[float]:
    w = controller.state.environment_status; temp = w.get('temp'); cond = w.get('condition'); hour = datetime.now().hour; c = controller.state.current_color
    if temp is None or cond is None: controller.set_light_state('red' if c != 'red' else 'off'); return 0.5
    elif hour < 16 or temp < 15 or cond in ('Drizzle', 'Rain', 'Snow', 'Thunderstorm'): controller.set_light_state('red')
    elif temp < 18 or cond in ('Clouds', 'Fog'): controller.set_light_state('yellow')
    else: controller.set_light_state('green')

def handle_space_mode(controller, elapsed: float) -> Optional[float]:
    kp = controller.state.space_weather_status.get('kp_index'); c = controller.state.current_color
    if kp is None or kp >= 5: controller.set_light_state('red' if c != 'red' else 'off'); return 0.5
    elif kp == 4: controller.set_light_state('yellow')
    else: controller.set_light_state('green')

def handle_uv_mode(controller, elapsed: float) -> Optional[float]:
    env = controller.state.environment_status
    uv = env.get('uv_index')
    c = controller.state.current_color
    if uv is None:
        # No data yet: slow red blink to signal "waiting".
        controller.set_light_state('red' if c != 'red' else 'off')
        return 0.5
    if uv >= 8:
        # Very high / extreme: fast red blink as a warning.
        controller.set_light_state('red' if c != 'red' else 'off')
        return 0.4
    if uv >= 6:
        controller.set_light_state('red')
    elif uv >= 3:
        controller.set_light_state('yellow')
    else:
        controller.set_light_state('green')
    return None

def handle_temp_mode(controller, elapsed: float) -> Optional[float]:
    temp = controller.state.environment_status.get('temp'); c = controller.state.current_color
    if temp is None: controller.set_light_state('red' if c != 'red' else 'off'); return 0.5
    if TEMP_GREEN_RANGE[0] <= temp <= TEMP_GREEN_RANGE[1]: controller.set_light_state('green')
    elif TEMP_YELLOW_RANGE[0] <= temp <= TEMP_YELLOW_RANGE[1]: controller.set_light_state('yellow')
    else: controller.set_light_state('red')

def handle_rain_mode(controller, elapsed: float) -> Optional[float]:
    env = controller.state.environment_status; c = controller.state.current_color
    if 'rain_now' not in env: controller.set_light_state('red' if c != 'red' else 'off'); return 0.5
    eta = env.get('rain_eta_min')
    if env['rain_now'] or (eta is not None and eta <= RAIN_RED_MINUTES): controller.set_light_state('red')
    elif eta is not None and eta <= RAIN_YELLOW_MINUTES: controller.set_light_state('yellow')
    else: controller.set_light_state('green')

def _fetch_space() -> Dict:
    try:
        r = requests.get(SPACE_WEATHER_URL, timeout=API_TIMEOUT); r.raise_for_status(); data = r.json()
        if not data:
            return {}
        latest = data[-1]
        # NOAA has returned both list-of-lists ([time, kp, ...]) and list-of-dicts
        # ({"time_tag":..., "Kp":...}) formats. Handle both.
        if isinstance(latest, dict):
            raw_kp = latest.get('Kp', latest.get('kp_index'))
        else:
            raw_kp = latest[1]
        if raw_kp is None:
            return {}
        kp = int(round(float(raw_kp)))  # NOAA gives thirds (4.67 = "5-"); truncating under-reports storms
        cond = 'Storm' if kp >= 5 else ('Active' if kp == 4 else 'Quiet')
        return {'kp_index': kp, 'condition': cond}
    except Exception as e:
        logging.debug(f"Space weather fetch failed: {e}")
        return {}

def space_weather_monitor(controller):
    _poll(controller, 'Space weather', _fetch_space, SPACE_POLL_INTERVAL,
          lambda v: setattr(controller.state, 'space_weather_status', v))

def _level_from_thresholds(value: Optional[float], thresholds: Tuple[float, float]) -> str:
    if value is None:
        return 'unknown'
    low, high = thresholds
    if value < low:
        return 'low'
    if value < high:
        return 'moderate'
    return 'high'

def _rain_outlook(current: Dict, hourly: Dict) -> Dict:
    """Raining now, and minutes until the first rainy hour within the look-ahead."""
    times = hourly.get('time') or []
    mm = hourly.get('precipitation') or []
    prob = hourly.get('precipitation_probability') or []
    if not times:
        return {}
    now = datetime.now()
    eta: Optional[int] = None; prob_max = 0
    for i, ts in enumerate(times):
        try:
            start = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if start + timedelta(hours=1) <= now:
            continue  # already over
        minutes = max(0, int((start - now).total_seconds() // 60))
        if minutes > RAIN_YELLOW_MINUTES:
            break
        p = prob[i] if i < len(prob) and isinstance(prob[i], (int, float)) else 0
        m = mm[i] if i < len(mm) and isinstance(mm[i], (int, float)) else 0.0
        prob_max = max(prob_max, int(p))
        if eta is None and (m >= RAIN_MM or p >= RAIN_PROBABILITY):
            eta = minutes
    now_mm = current.get('precipitation')
    return {
        'rain_now': bool(isinstance(now_mm, (int, float)) and now_mm >= RAIN_MM / 2),
        'rain_eta_min': eta,
        'rain_prob_max': prob_max,
    }

def _wmo_condition(code: int) -> str:
    """Open-Meteo's WMO weather code as a one-word condition."""
    if code <= 1: return 'Clear'
    if code <= 3: return 'Clouds'
    if code in (45, 48): return 'Fog'
    if 51 <= code <= 57: return 'Drizzle'
    if 71 <= code <= 77 or code in (85, 86): return 'Snow'
    if code >= 95: return 'Thunderstorm'
    return 'Rain'  # 61-67, 80-82

def _fetch_environment() -> Dict:
    result: Dict = {}
    # Forecast: UV index + is_day + sunrise/sunset
    try:
        r = requests.get(
            OPEN_METEO_FORECAST_URL,
            params={
                'latitude': WEATHER_LAT,
                'longitude': WEATHER_LON,
                'current': 'uv_index,is_day,temperature_2m,apparent_temperature,precipitation,weather_code',
                'daily': 'sunrise,sunset,temperature_2m_max',
                'hourly': 'precipitation,precipitation_probability',
                'timezone': 'auto',
                'forecast_days': 2,  # so the 3-hour rain look-ahead works late in the evening
            },
            timeout=API_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json() or {}
        cur = data.get('current') or {}
        uv = cur.get('uv_index')
        if isinstance(uv, (int, float)):
            result['uv_index'] = round(float(uv), 1)
        is_day = cur.get('is_day')
        if is_day is not None:
            result['is_day'] = bool(is_day)
        daily = data.get('daily') or {}
        sunrises = daily.get('sunrise') or []
        sunsets = daily.get('sunset') or []
        if sunrises:
            result['sunrise'] = sunrises[0]
        if sunsets:
            result['sunset'] = sunsets[0]
        for key, src in (('temp', 'temperature_2m'), ('feels_like', 'apparent_temperature')):
            if isinstance(cur.get(src), (int, float)):
                result[key] = round(float(cur[src]), 1)
        if isinstance(cur.get('weather_code'), (int, float)):
            result['condition'] = _wmo_condition(int(cur['weather_code']))
        highs = daily.get('temperature_2m_max') or []
        if highs and isinstance(highs[0], (int, float)):
            result['temp_max'] = round(float(highs[0]), 1)
        result.update(_rain_outlook(cur, data.get('hourly') or {}))
    except Exception as e:
        logging.warning(f"Open-Meteo forecast fetch failed: {e}")

    # Air quality + pollen
    try:
        r = requests.get(
            OPEN_METEO_AIR_QUALITY_URL,
            params={
                'latitude': WEATHER_LAT,
                'longitude': WEATHER_LON,
                'current': 'european_aqi,pm2_5,grass_pollen,birch_pollen,alder_pollen,olive_pollen,mugwort_pollen,ragweed_pollen',
                'timezone': 'auto',
            },
            timeout=API_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json() or {}
        cur = data.get('current') or {}
        aqi = cur.get('european_aqi')
        if isinstance(aqi, (int, float)):
            result['aqi'] = int(round(aqi))
            # EU AQI bands: 0-20 good, 20-40 fair, 40-60 moderate, 60-80 poor, 80-100 very poor, 100+ extreme.
            if aqi < 20: result['aqi_label'] = 'good'
            elif aqi < 40: result['aqi_label'] = 'fair'
            elif aqi < 60: result['aqi_label'] = 'moderate'
            elif aqi < 80: result['aqi_label'] = 'poor'
            else: result['aqi_label'] = 'very poor'

        pollen_species = ('grass_pollen', 'birch_pollen', 'alder_pollen',
                          'olive_pollen', 'mugwort_pollen', 'ragweed_pollen')
        max_pollen: Optional[float] = None
        dominant: Optional[str] = None
        for sp in pollen_species:
            v = cur.get(sp)
            if isinstance(v, (int, float)):
                if max_pollen is None or v > max_pollen:
                    max_pollen = float(v)
                    dominant = sp.replace('_pollen', '')
        if max_pollen is not None:
            # Rough bands (grains/m³) used by many European networks.
            result['pollen_max'] = round(max_pollen, 1)
            result['pollen_level'] = _level_from_thresholds(max_pollen, (20.0, 50.0))
            if dominant:
                result['pollen_dominant'] = dominant
    except Exception as e:
        logging.warning(f"Open-Meteo air quality fetch failed: {e}")

    return result

def environment_monitor(controller):
    # Forecast and air quality are separate requests; when only one comes back,
    # merge it over the last values instead of losing the other half.
    _poll(controller, 'Environment', _fetch_environment, ENVIRONMENT_POLL_INTERVAL,
          lambda v: setattr(controller.state, 'environment_status', {**controller.state.environment_status, **v} if v else {}))
