"""Racing tile (Start, Revs, Hotlap) and the UDP listener for the SimHub plugin."""
from __future__ import annotations
import logging
import random
from time import time
import socket
from typing import List, Optional
from .clock import CHIME_LATENCY_SECONDS, play_pcm, stop_chime

SIMHUB_UDP_HOST = "0.0.0.0"
SIMHUB_UDP_PORT = 9001

# Formula 1 start gantry. As on the real grid, five lights come on one second apart,
# each with the broadcast "beep-thump"; after a random 0.2-3 s hold they all go out
# (silently - "lights out and away we go"). The light only has three lamps, so they
# join on the 1st, 3rd and 5th beep and the gantry is full on the fifth, as in F1.
# The sound is modelled on a recording of the F1 start sound, measured per hit:
#   - beep: 494.2 Hz (B4) for ~150 ms, faint 2nd/3rd harmonics (-20 dB);
#   - thump: a kick-drum sweep from ~190 Hz down to 52.5 Hz within ~60 ms, held at
#     full level and cut off at 332 ms;
#   - the beep has about 0.7x the thump's energy in the first 120 ms.
F1_LIGHTS = 5
F1_LIGHT_INTERVAL = 1.0
F1_LAMP_STEPS = {0: 'red', 2: 'red_and_yellow', 4: 'all_on'}  # beep index -> lamps
F1_HOLD_RANGE = (0.2, 3.0)     # FIA: random delay between the last light and lights out
F1_GRID_PAUSE = 0.8            # silence before the first light
F1_BEEP_HZ = 494.2
F1_BEEP_SECONDS = 0.15
F1_BEEP_HARMONICS = ((1.0, 1.0), (2.0, 0.1), (3.0, 0.08))
F1_THUMP_HZ = (190.0, 52.5)    # sweep start -> settled pitch
F1_THUMP_SWEEP_SECONDS = 0.02  # time constant of that sweep
F1_HIT_SECONDS = 0.332         # the whole hit, thump cut off at the end
F1_BEEP_TO_THUMP = 0.46   # balance measured against the recording (beep/thump energy 0.68 in the first 120 ms)

# Intro played when a Racing sub-mode is selected: (lamps, seconds) steps.
# One rev to the limiter: quick climb, hold on redline, drop back, short breath.
_REV_BLIP = [('green', 0.1), ('green-yellow', 0.1), ('all_on', 0.6), ('green-yellow', 0.12), ('green', 0.12), ('off', 0.35)]
RACING_INTROS = {
    'racing': _REV_BLIP * 2,                                                                            # two revs to the limiter
    'hotlap': [('green', 0.4), ('yellow', 0.4), ('green', 0.4), ('yellow', 0.4), ('red', 0.8), ('off', 0.4)],  # delta battle, then invalid
}

# Hotlap (Racing sub-mode): live delta to your best lap from the SimHub plugin.
HOTLAP_DEADBAND = 0.03       # +-s around 0 where the colour holds, so being exactly on pace doesn't chatter the relays
HOTLAP_MIN_SWITCH = 0.25     # s between green/yellow changes (red always switches at once)
SIMHUB_STALE_SECONDS = 2.0   # no packet from the plugin for this long (game closed) -> Racing lights off

def _f1_hit(np, rate: int):
    """One gantry light: the beep over the thump (see F1_* config)."""
    tt = np.arange(int(F1_HIT_SECONDS * rate)) / rate
    def gate(length, fade):  # 2 ms attack, flat, short fade at the end: no clicks
        return np.minimum(tt / 0.002, 1.0) * np.clip((length - tt) / fade, 0.0, 1.0)
    beep = sum(a * np.sin(2 * np.pi * F1_BEEP_HZ * r * tt) for r, a in F1_BEEP_HARMONICS) * gate(F1_BEEP_SECONDS, 0.02)
    lo, hi = F1_THUMP_HZ[1], F1_THUMP_HZ[0]; tau = F1_THUMP_SWEEP_SECONDS
    phase = 2 * np.pi * (lo * tt + (hi - lo) * tau * (1.0 - np.exp(-tt / tau)))  # pitch sweeps hi -> lo
    # A little saturation adds the overtones that let small speakers suggest the 52 Hz they can't play.
    thump = np.tanh(1.6 * np.sin(phase)) / np.tanh(1.6) * gate(F1_HIT_SECONDS, 0.012)
    rms = lambda x: float(np.sqrt(np.mean(x ** 2))) or 1.0
    return thump / rms(thump) + beep / rms(beep) * F1_BEEP_TO_THUMP

def _render_f1_beeps(np, starts: List[float], volume: float, rate: int = 44100):
    hit = _f1_hit(np, rate)
    end = max(starts) + F1_HIT_SECONDS + 0.1
    out = np.zeros(int(end * rate), dtype=np.float64)
    for s in starts:
        i = max(0, int((s - CHIME_LATENCY_SECONDS) * rate))
        n = min(len(hit), len(out) - i)
        out[i:i + n] += hit[:n]
    peak = float(np.max(np.abs(out))) or 1.0
    return (out / peak * max(0.0, min(volume, 1.0)) * 32767).astype('<i2')

def play_f1_beeps(starts: List[float], volume: float) -> None:
    """Synthesises and plays the F1 start gantry beeps at the given timestamps (seconds from now)."""
    stop_chime()
    if volume <= 0 or not starts:
        return
    try:
        import numpy as np  # type: ignore
    except ImportError:
        logging.warning("Racing: numpy missing, no F1 start sound"); return
    play_pcm(_render_f1_beeps(np, starts, volume))

def start_f1_sequence(controller, hold: Optional[float] = None) -> None:
    """Starts the Formula 1 start: five beeps a second apart (lamps join on the 1st, 3rd and 5th),
    a random hold, then lights out."""
    if hold is None:
        hold = round(random.uniform(*F1_HOLD_RANGE), 2)
    b = F1_LIGHT_INTERVAL; g = F1_GRID_PAUSE
    seq = [('off', g)]
    for i in range(F1_LIGHTS):
        lamps = F1_LAMP_STEPS.get(i, seq[-1][0])
        seq.append((lamps, hold if i == F1_LIGHTS - 1 else b))
    seq.append(('off', 1.2))
    play_f1_beeps([g + i * b for i in range(F1_LIGHTS)], controller.state.chime_volume)
    controller.state.mode_state.update(intro_seq=seq, intro_idx=-1, intro_until=time())

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
    # Advance from the scheduled boundary so tick delays don't drift off the F1 beeps.
    base = ms.get('intro_until') or now
    ms['intro_idx'] = idx; ms['intro_until'] = (base if now - base < 0.2 else now) + dur
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
    # Formula 1 start lights + beeps on entry (or when SimHub signals a standing start), then live flags.
    s = controller.state; ms = s.mode_state; now = time()
    if ms.pop('race_start_force', False):
        start_f1_sequence(controller)
        return 0.02
    live_simhub = now - s.simhub_last_rx <= SIMHUB_STALE_SECONDS
    flag = s.simhub_flag_status if live_simhub else 'black'
    prev_flag = ms.get('race_prev_flag', 'black')
    if live_simhub and flag != prev_flag:
        ms['race_prev_flag'] = flag
        if flag == 'red':
            # Standing start lights on in the sim: build up the 3 lights with F1 beeps and hold on the grid.
            start_f1_sequence(controller, hold=12.0)
            return 0.02
        if prev_flag == 'red' and flag in ('green', 'black'):
            # Lights out and away we go! Extinguish all lamps at the start signal.
            stop_chime()
            ms.update(intro_seq=[('off', 1.5)], intro_idx=-1, intro_until=now)
    intro = _play_intro(controller)
    if intro is not None:
        return intro
    stop_chime(wait=False)
    if not live_simhub:
        ms['race_prev_flag'] = 'black'
        controller.set_light_state('off'); return 0.1
    controller.set_light_state('off' if flag in ('black', 'red') else flag)
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
