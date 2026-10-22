"""The state shared between the controller, the monitors and the web server."""
from __future__ import annotations
import threading
from time import time
from dataclasses import dataclass, field
from typing import Dict, Optional
from .clock import CHIME_VOLUME
from .party import AUDIO_VU_FLOOR_SEED
from .racing import SIMHUB_STALE_SECONDS

@dataclass
class SharedState:
    target_mode: str = "auto"
    target_manual_color: str = "off"
    current_mode: str = "auto"
    current_color: str = "unknown"
    last_state_change_time: float = field(default_factory=time)
    s_bahn_minutes_away: int = -1
    s_bahn_transit_status: Dict = field(default_factory=dict)
    simhub_light_status: str = "black"
    simhub_rev_pct: Optional[float] = None
    simhub_delta: Optional[float] = None
    simhub_flag_status: str = "black"
    simhub_lap_invalid: bool = False
    simhub_last_rx: float = 0.0
    space_weather_status: Dict = field(default_factory=dict)
    traffic_status: Dict = field(default_factory=dict)
    environment_status: Dict = field(default_factory=dict)
    gremio_status: Dict = field(default_factory=dict)
    party_status: Dict = field(default_factory=dict)
    audio_vu_level: float = 0.0
    network_connected: bool = False
    # What was last picked in the UI: restored when the network comes back and after a restart.
    chosen_mode: str = 'manual'
    chosen_color: str = 'off'
    chime_volume: float = max(0.0, min(CHIME_VOLUME, 1.0))  # Clock bells; 0 = muted
    chime_volume_on: float = max(0.05, min(CHIME_VOLUME, 1.0))  # the level unmuting returns to
    mode_state: Dict = field(default_factory=lambda: {
        'next_auto_state': 'green',
        'sos_index': 0,
        'sos_pattern': [
            {'state': 'all_on', 'duration': 0.2}, {'state': 'off', 'duration': 0.2},
            {'state': 'all_on', 'duration': 0.2}, {'state': 'off', 'duration': 0.2},
            {'state': 'all_on', 'duration': 0.2}, {'state': 'off', 'duration': 0.4},
            {'state': 'all_on', 'duration': 0.6}, {'state': 'off', 'duration': 0.2},
            {'state': 'all_on', 'duration': 0.6}, {'state': 'off', 'duration': 0.2},
            {'state': 'all_on', 'duration': 0.6}, {'state': 'off', 'duration': 0.4},
            {'state': 'all_on', 'duration': 0.2}, {'state': 'off', 'duration': 0.2},
            {'state': 'all_on', 'duration': 0.2}, {'state': 'off', 'duration': 0.2},
            {'state': 'all_on', 'duration': 0.2}, {'state': 'off', 'duration': 1.5},
        ],
        'click_until': 0.0,
        'audio_vu_last': 0.0,
        'audio_vu_ignore_until': 0.0,
        'audio_vu_last_step_time': 0.0,
        'audio_vu_floor': AUDIO_VU_FLOOR_SEED,
        'audio_vu_last_update': 0.0,
    })
    lock: threading.RLock = field(default_factory=threading.RLock)
    running: bool = True

    def snapshot(self) -> Dict:
        return {
            'color': self.current_color,
            'mode': self.current_mode,
            's_bahn_minutes': self.s_bahn_minutes_away,
            's_bahn_transit': self.s_bahn_transit_status.copy(),
            'race_step': 0 if self.mode_state.get('intro_seq') else 4,  # the web UI waits for the intro to finish
            'simhub_rev_pct': self.simhub_rev_pct,
            'simhub_delta': self.simhub_delta,
            'simhub_lap_invalid': self.simhub_lap_invalid,
            'simhub_flag': self.simhub_flag_status,
            'simhub_live': time() - self.simhub_last_rx < SIMHUB_STALE_SECONDS,
            'space_weather': self.space_weather_status.copy(),
            'traffic': self.traffic_status.copy(),
            'environment': self.environment_status.copy(),
            'gremio': self.gremio_status.copy(),
            'party': self.party_status.copy(),
            'audio_level': self.audio_vu_level,
            'network_connected': self.network_connected,
            'chime_volume': round(self.chime_volume, 2),
        }
