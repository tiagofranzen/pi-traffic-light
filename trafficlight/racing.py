"""Racing tile (Start, Revs, Hotlap) and the UDP listener for the SimHub plugin."""
from __future__ import annotations
import logging
from time import time
import socket
from typing import Optional

SIMHUB_UDP_HOST = "0.0.0.0"
SIMHUB_UDP_PORT = 9001

# Intro played when a Racing sub-mode is selected: (lamps, seconds) steps.
# One rev to the limiter: quick climb, hold on redline, drop back, short breath.
_REV_BLIP = [('green', 0.1), ('green-yellow', 0.1), ('all_on', 0.6), ('green-yellow', 0.12), ('green', 0.12), ('off', 0.35)]
RACING_INTROS = {
    'race_start': [('off', 1.0), ('red', 1.0), ('red_and_yellow', 1.0), ('all_on', 1.0), ('off', 0.5)],  # start lights
    'racing': _REV_BLIP * 2,                                                                            # two revs to the limiter
    'hotlap': [('green', 0.4), ('yellow', 0.4), ('green', 0.4), ('yellow', 0.4), ('red', 0.8), ('off', 0.4)],  # delta battle, then invalid
}

# Hotlap (Racing sub-mode): live delta to your best lap from the SimHub plugin.
HOTLAP_DEADBAND = 0.03       # +-s around 0 where the colour holds, so being exactly on pace doesn't chatter the relays
HOTLAP_MIN_SWITCH = 0.25     # s between green/yellow changes (red always switches at once)
SIMHUB_STALE_SECONDS = 2.0   # no packet from the plugin for this long (game closed) -> Racing lights off

def _play_intro(controller) -> Optional[float]:
    """Step through the sub-mode's intro; returns a sleep while it plays, None once it's done."""
    ms = controller.state.mode_state; seq = ms.get('intro_seq')
    if not seq:
        return None
    now = time()
    if now < ms.get('intro_until', 0.0):
        return 0.02
    idx = ms.get('intro_idx', -1) + 1
    if idx >= len(seq):
        ms['intro_seq'] = None
        return None
    color, dur = seq[idx]
    controller.set_light_state(color)
    ms['intro_idx'] = idx; ms['intro_until'] = now + dur
    return 0.02

def handle_racing_mode(controller, elapsed: float) -> Optional[float]:
    intro = _play_intro(controller)
    if intro is not None:
        return intro
    s = controller.state
    if time() - s.simhub_last_rx > SIMHUB_STALE_SECONDS:
        controller.set_light_state('off'); return 0.1  # the plugin stops sending when the game closes
    live = s.simhub_light_status if s.simhub_light_status != 'black' else 'off'; controller.set_light_state(live); return 0.05

def handle_race_start_mode(controller, elapsed: float) -> Optional[float]:
    # Race start lights and flags from the plugin: red = lights set, yellow = get ready / yellow flag, green = go.
    intro = _play_intro(controller)
    if intro is not None:
        return intro
    s = controller.state
    if time() - s.simhub_last_rx > SIMHUB_STALE_SECONDS:
        controller.set_light_state('off'); return 0.1
    flag = s.simhub_flag_status
    controller.set_light_state('off' if flag == 'black' else flag)
    return 0.02

def handle_hotlap_mode(controller, elapsed: float) -> Optional[float]:
    intro = _play_intro(controller)
    if intro is not None:
        return intro
    s = controller.state; now = time(); c = s.current_color
    if now - s.simhub_last_rx > SIMHUB_STALE_SECONDS:
        controller.set_light_state('off'); return 0.1
    if s.simhub_lap_invalid:
        controller.set_light_state('red'); return 0.02  # overrides the delta, immediately
    d = s.simhub_delta
    if d is None:
        controller.set_light_state('off'); return 0.05  # no reference lap yet
    if d < -HOTLAP_DEADBAND: want = 'green'
    elif d > HOTLAP_DEADBAND: want = 'yellow'
    else: want = c if c in ('green', 'yellow') else 'yellow'
    if want != c and c in ('green', 'yellow') and now - s.mode_state.get('hotlap_switch', 0.0) < HOTLAP_MIN_SWITCH:
        return 0.02
    if want != c:
        s.mode_state['hotlap_switch'] = now
    controller.set_light_state(want)
    return 0.02

def simhub_udp_listener(controller):
    valid = {'red','yellow','green','black','green-yellow','all_on'}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind((SIMHUB_UDP_HOST, SIMHUB_UDP_PORT)); sock.settimeout(1.0)
            while controller.state.running:
                try:
                    data, _ = sock.recvfrom(1024)
                    payload = data.decode('utf-8').strip()
                    # Wire format is "<rev_color>[:<revpct>[:<delta>:<invalid>[:<flag_color>]]]";
                    # the extra fields are optional so older senders keep working.
                    # delta = seconds vs your best lap (empty = no reference yet),
                    # invalid = 1 once the current lap is invalidated,
                    # flag_color = race start lights / flags (old senders put flags in the first field).
                    fields = payload.split(':')
                    color = fields[0]
                    if color in valid:
                        def num(i, lo=None, hi=None):
                            try:
                                v = float(fields[i])
                            except (IndexError, ValueError):
                                return None
                            return v if lo is None else max(lo, min(hi, v))
                        with controller.state.lock:
                            controller.state.simhub_light_status = color
                            controller.state.simhub_rev_pct = num(1, 0.0, 100.0)
                            controller.state.simhub_delta = num(2)
                            controller.state.simhub_lap_invalid = len(fields) > 3 and fields[3] == '1'
                            controller.state.simhub_flag_status = fields[4] if len(fields) > 4 and fields[4] in valid else color
                            controller.state.simhub_last_rx = time()
                except socket.timeout: continue
                except Exception: continue
    except Exception as e:
        logging.error(f"SimHub listener error: {e}")
