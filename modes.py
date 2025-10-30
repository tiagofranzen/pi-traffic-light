"""All mode handlers consolidated into a single file."""
from time import time
import random
from datetime import datetime
from typing import Optional
from traffic_light.config import timing_config

# Each handler returns optional custom sleep.

def handle_auto_mode(controller, elapsed: float) -> Optional[float]:
    state = controller.state
    c = state.current_color
    if c == 'green' and elapsed > timing_config.AUTO_GREEN_DURATION:
        controller.set_light_state('yellow'); state.mode_state['next_auto_state'] = 'red'; state.last_state_change_time = time()
    elif c == 'yellow' and elapsed > timing_config.AUTO_YELLOW_DURATION:
        controller.set_light_state(state.mode_state['next_auto_state']); state.last_state_change_time = time()
    elif c == 'red' and elapsed > timing_config.AUTO_RED_DURATION:
        controller.set_light_state('red_and_yellow'); state.mode_state['next_auto_state'] = 'green'; state.last_state_change_time = time()
    elif c == 'red_and_yellow' and elapsed > timing_config.AUTO_RED_YELLOW_DURATION:
        controller.set_light_state(state.mode_state['next_auto_state']); state.last_state_change_time = time()

def handle_party_mode(controller, elapsed: float) -> Optional[float]:
    controller.set_light_state(random.choice(['red','yellow','green','off'])); return timing_config.PARTY_BLINK_INTERVAL

def handle_emergency_mode(controller, elapsed: float) -> Optional[float]:
    controller.set_light_state('yellow' if controller.state.current_color != 'yellow' else 'off'); return timing_config.EMERGENCY_BLINK_INTERVAL

def handle_sos_mode(controller, elapsed: float) -> Optional[float]:
    state = controller.state
    pattern = state.mode_state['sos_pattern']; idx = state.mode_state['sos_index']; step = pattern[idx]
    if elapsed > step['duration']:
        state.mode_state['sos_index'] = (idx + 1) % len(pattern); controller.set_light_state(pattern[state.mode_state['sos_index']]['state']); state.last_state_change_time = time()

def handle_s_bahn_mode(controller, elapsed: float) -> Optional[float]:
    minutes = controller.state.s_bahn_minutes_away; c = controller.state.current_color
    if minutes == -1: controller.set_light_state('red' if c != 'red' else 'off'); return 0.5
    elif minutes < 9: controller.set_light_state('red')
    elif minutes == 9: controller.set_light_state('yellow' if c != 'yellow' else 'off'); return 0.5
    elif minutes <= 12: controller.set_light_state('yellow')
    else: controller.set_light_state('green')

def handle_biergarten_mode(controller, elapsed: float) -> Optional[float]:
    w = controller.state.weather_status; temp = w.get('temp'); cond = w.get('condition'); hour = datetime.now().hour; c = controller.state.current_color
    if temp is None or cond is None: controller.set_light_state('red' if c != 'red' else 'off'); return 0.5
    elif hour < 16 or temp < 15 or 'Rain' in cond or 'Snow' in cond: controller.set_light_state('red')
    elif temp < 18 or 'Clouds' in cond: controller.set_light_state('yellow')
    else: controller.set_light_state('green')

def handle_racing_mode(controller, elapsed: float) -> Optional[float]:
    state = controller.state; step = state.mode_state['race_step']
    if step < 4:
        if step == 0 and elapsed > 1: controller.set_light_state('red'); state.mode_state['race_step'] += 1; state.last_state_change_time = time()
        elif step == 1 and elapsed > 1: controller.set_light_state('red_and_yellow'); state.mode_state['race_step'] += 1; state.last_state_change_time = time()
        elif step == 2 and elapsed > 1: controller.set_light_state('all_on'); state.mode_state['race_step'] += 1; state.last_state_change_time = time()
        elif step == 3 and elapsed > 1: controller.set_light_state('off'); state.mode_state['race_step'] += 1; state.last_state_change_time = time()
    else:
        live = state.iracing_light_status if state.iracing_light_status != 'black' else 'off'; controller.set_light_state(live); return 0.05

def handle_space_mode(controller, elapsed: float) -> Optional[float]:
    kp = controller.state.space_weather_status.get('kp_index'); c = controller.state.current_color
    if kp is None or kp >= 5: controller.set_light_state('red' if c != 'red' else 'off'); return 0.5
    elif kp == 4: controller.set_light_state('yellow')
    else: controller.set_light_state('green')

def handle_stau_mode(controller, elapsed: float) -> Optional[float]:
    delay = controller.state.traffic_status.get('avg_delay'); c = controller.state.current_color
    if delay is None: controller.set_light_state('red' if c != 'red' else 'off'); return 0.5
    elif delay > 45: controller.set_light_state('red')
    elif delay > 20: controller.set_light_state('yellow')
    else: controller.set_light_state('green')
