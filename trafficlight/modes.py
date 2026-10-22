"""The simple modes: traffic light cycle, blinking and SOS."""
from __future__ import annotations
from time import time
from typing import Optional
from .config import AUTO_GREEN_DURATION, AUTO_RED_DURATION, AUTO_RED_YELLOW_DURATION, AUTO_YELLOW_DURATION, EMERGENCY_BLINK_INTERVAL

def handle_auto_mode(controller, elapsed: float) -> Optional[float]:
    s = controller.state; c = s.current_color
    if c == 'green' and elapsed > AUTO_GREEN_DURATION:
        controller.set_light_state('yellow'); s.mode_state['next_auto_state'] = 'red'; s.last_state_change_time = time()
    elif c == 'yellow' and elapsed > AUTO_YELLOW_DURATION:
        controller.set_light_state(s.mode_state['next_auto_state']); s.last_state_change_time = time()
    elif c == 'red' and elapsed > AUTO_RED_DURATION:
        controller.set_light_state('red_and_yellow'); s.mode_state['next_auto_state'] = 'green'; s.last_state_change_time = time()
    elif c == 'red_and_yellow' and elapsed > AUTO_RED_YELLOW_DURATION:
        controller.set_light_state(s.mode_state['next_auto_state']); s.last_state_change_time = time()

def handle_emergency_mode(controller, elapsed: float) -> Optional[float]:
    controller.set_light_state('yellow' if controller.state.current_color != 'yellow' else 'off'); return EMERGENCY_BLINK_INTERVAL

def handle_sos_mode(controller, elapsed: float) -> Optional[float]:
    s = controller.state; pattern = s.mode_state['sos_pattern']; idx = s.mode_state['sos_index']; step = pattern[idx]
    if elapsed > step['duration']:
        s.mode_state['sos_index'] = (idx + 1) % len(pattern)
        controller.set_light_state(pattern[s.mode_state['sos_index']]['state']); s.last_state_change_time = time()
