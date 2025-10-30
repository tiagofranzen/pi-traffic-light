"""
Configuration management for the traffic light system.
All configurable parameters are centralized here.
"""
from dataclasses import dataclass
from typing import Tuple
import os


@dataclass
class GPIOConfig:
    """GPIO pin configuration for Raspberry Pi"""
    RED_PIN: int = 22
    YELLOW_PIN: int = 27
    GREEN_PIN: int = 17
    ACTIVE_HIGH: bool = False


@dataclass
class NetworkConfig:
    """Network settings for web server and UDP listener"""
    WEB_SERVER_HOST: str = "0.0.0.0"
    WEB_SERVER_PORT: int = 8000
    IRACING_UDP_HOST: str = "0.0.0.0"
    IRACING_UDP_PORT: int = 9001


@dataclass
class TimingConfig:
    """Timing constants for various modes and operations"""
    # Auto mode timings
    AUTO_GREEN_DURATION: float = 20.0
    AUTO_YELLOW_DURATION: float = 3.0
    AUTO_RED_DURATION: float = 20.0
    AUTO_RED_YELLOW_DURATION: float = 2.0
    
    # Effect timings
    EMERGENCY_BLINK_INTERVAL: float = 0.5
    PARTY_BLINK_INTERVAL: float = 0.08
    CONTROLLER_LOOP_SLEEP: float = 0.2
    RACING_STEP_DURATION: float = 1.0
    
    # Monitor polling intervals
    S_BAHN_POLL_INTERVAL: float = 30.0
    WEATHER_POLL_INTERVAL: float = 900.0  # 15 minutes
    SPACE_POLL_INTERVAL: float = 900.0    # 15 minutes
    TRAFFIC_POLL_INTERVAL: float = 600.0   # 10 minutes
    
    # Error handling
    API_TIMEOUT: float = 15.0
    MAX_CONSECUTIVE_FAILURES: int = 5
    FAILURE_BACKOFF_DURATION: float = 300.0  # 5 minutes


@dataclass
class LocationConfig:
    """Geographic location settings"""
    # Hohenbrunn coordinates for weather
    WEATHER_LAT: str = "48.0667"
    WEATHER_LON: str = "11.7167"
    
    # S-Bahn station
    S_BAHN_EVA: str = "8004733"  # Ottobrunn
    
    # S-Bahn destinations to filter out (outbound trains)
    OUTBOUND_DESTINATIONS: Tuple[str, ...] = (
        "Kreuzstraße",
        "Aying",
        "Höhenkirchen-Siegertsbrunn",
        "Dürrnhaar",
        "Hohenbrunn",
        "Wächterhof"
    )
    
    # Traffic routes for monitoring
    TRAFFIC_ROUTES: Tuple[dict, ...] = (
        {
            "name": "commute",
            "origin": "Nelkenstraße 24A, 85521 Hohenbrunn, Germany",
            "destination": "Landaubogen 1, 81373 München, Germany"
        },
        {
            "name": "center",
            "origin": "Hohenbrunn, Germany",
            "destination": "Marienplatz, Munich, Germany"
        },
        {
            "name": "north",
            "origin": "Hohenbrunn, Germany",
            "destination": "BMW Welt, Munich, Germany"
        }
    )


@dataclass
class APIConfig:
    """External API configuration and credentials"""
    # API Keys from environment
    DB_CLIENT_ID: str = os.getenv("DB_CLIENT_ID", "")
    DB_CLIENT_SECRET: str = os.getenv("DB_CLIENT_SECRET", "")
    OWM_API_KEY: str = os.getenv("OWM_API_KEY", "")
    GOOGLE_MAPS_API_KEY: str = os.getenv("GOOGLE_MAPS_API_KEY", "")
    
    # API URLs
    DB_API_URL: str = "https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1/plan"
    WEATHER_API_URL_TEMPLATE: str = "https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={api_key}&units=metric"
    SPACE_WEATHER_URL: str = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
    GOOGLE_DIRECTIONS_URL: str = "https://maps.googleapis.com/maps/api/directions/json"
    
    def is_s_bahn_enabled(self) -> bool:
        """Check if S-Bahn monitoring is properly configured"""
        return bool(self.DB_CLIENT_ID and self.DB_CLIENT_SECRET)
    
    def is_weather_enabled(self) -> bool:
        """Check if weather monitoring is properly configured"""
        return bool(self.OWM_API_KEY)
    
    def is_traffic_enabled(self) -> bool:
        """Check if traffic monitoring is properly configured"""
        return bool(self.GOOGLE_MAPS_API_KEY)


# Singleton instances for easy import
gpio_config = GPIOConfig()
network_config = NetworkConfig()
timing_config = TimingConfig()
location_config = LocationConfig()
api_config = APIConfig()
