"""
Shared state management for the traffic light system.
Replaces all global variables with a thread-safe state container.
"""
from dataclasses import dataclass, field
from typing import Dict, List
import threading
from time import time


@dataclass
class SharedState:
    """
    Thread-safe shared state container.
    All mutable state is stored here and protected by a lock.
    """
    # Mode management
    target_mode: str = "auto"
    target_manual_color: str = "off"
    current_mode: str = "auto"
    current_color: str = "unknown"
    last_state_change_time: float = field(default_factory=time)
    
    # External data from monitors
    s_bahn_minutes_away: int = -1
    weather_status: Dict = field(default_factory=dict)
    iracing_light_status: str = "black"
    space_weather_status: Dict = field(default_factory=dict)
    traffic_status: Dict = field(default_factory=dict)
    
    # Mode-specific state
    mode_state: Dict = field(default_factory=lambda: {
        'next_auto_state': 'green',
        'sos_index': 0,
        'race_step': 0,
        'sos_pattern': [
            {'state': 'all_on', 'duration': 0.2},
            {'state': 'off', 'duration': 0.2},
            {'state': 'all_on', 'duration': 0.2},
            {'state': 'off', 'duration': 0.2},
            {'state': 'all_on', 'duration': 0.2},
            {'state': 'off', 'duration': 0.4},
            {'state': 'all_on', 'duration': 0.6},
            {'state': 'off', 'duration': 0.2},
            {'state': 'all_on', 'duration': 0.6},
            {'state': 'off', 'duration': 0.2},
            {'state': 'all_on', 'duration': 0.6},
            {'state': 'off', 'duration': 0.4},
            {'state': 'all_on', 'duration': 0.2},
            {'state': 'off', 'duration': 0.2},
            {'state': 'all_on', 'duration': 0.2},
            {'state': 'off', 'duration': 0.2},
            {'state': 'all_on', 'duration': 0.2},
            {'state': 'off', 'duration': 1.5},
        ]
    })
    
    # Thread synchronization
    # Use RLock because controller methods (e.g. run loop) acquire the lock and then
    # call other methods like set_light_state that also attempt to acquire it.
    # A standard Lock would deadlock on this re-entrancy. RLock allows the same thread
    # to acquire multiple times safely.
    lock: threading.RLock = field(default_factory=threading.RLock)
    
    # Shutdown flag
    running: bool = True
    
    def get_status_snapshot(self) -> Dict:
        """
        Return a thread-safe snapshot of current status.
        Must be called with lock held or will acquire lock.
        """
        return {
            'color': self.current_color,
            'mode': self.current_mode,
            's_bahn_minutes': self.s_bahn_minutes_away,
            'weather': self.weather_status.copy() if self.weather_status else {},
            'race_step': self.mode_state.get('race_step', 0),
            'space_weather': self.space_weather_status.copy() if self.space_weather_status else {},
            'traffic': self.traffic_status.copy() if self.traffic_status else {}
        }
