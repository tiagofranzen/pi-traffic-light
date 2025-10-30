"""Traffic Light Control System Package"""

__version__ = "2.0.0"
__author__ = "Traffic Light Team"

from traffic_light.controller import TrafficLightController
from traffic_light.state import SharedState
from traffic_light.config import (
    gpio_config,
    network_config,
    timing_config,
    location_config,
    api_config
)

__all__ = [
    'TrafficLightController',
    'SharedState',
    'gpio_config',
    'network_config',
    'timing_config',
    'location_config',
    'api_config'
]
