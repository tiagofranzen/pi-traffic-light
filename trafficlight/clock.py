"""Clock mode: quarter chimes as lamp sweeps, with Westminster bells on a speaker."""
from __future__ import annotations
import os
import logging
from time import time
from datetime import datetime
import shlex
import shutil
import subprocess
import tempfile
import threading
from typing import Dict, List, Optional, Tuple

# Clock mode - quarter chimes like an old striking clock, as sweeps along the
# light (green bottom, red top), rung with the Westminster Quarters (Big Ben's
# tune, 1793) on synthesised bells when there's a speaker. Each sweep is one
# change of four notes: the lamps join on the first three (up = green, +yellow,
# +red; down = red, +yellow, +green) and all go dark on the long fourth.
# :15 = 1 change, :30 = 2, :45 = 3, :00 = 4, then the hour bell strikes with
# each yellow blink (12h). Between chimes the lamps stay off. The volume (and
# mute) is set from the bar next to the light in the web UI.
CLOCK_STRIKE_ON = 0.75             # how long each hour strike stays lit
CLOCK_QUIET_HOURS = (23, 7)        # no chimes from 23:00 to 06:59 (relays click)
WESTMINSTER_BELLS = (415.30, 369.99, 329.63, 246.94)   # G#4 F#4 E4 B3
WESTMINSTER_HOUR_BELL = 164.81                         # E3
WESTMINSTER_CHANGES = ((0, 1, 2, 3), (2, 0, 1, 3), (2, 1, 0, 2), (0, 2, 1, 3), (3, 1, 0, 2))
WESTMINSTER_QUARTERS = {1: (0,), 2: (1, 2), 3: (3, 4, 0), 4: (1, 2, 3, 4)}
WESTMINSTER_BEAT = 0.7             # note to note; the fourth note of a change rings two beats
WESTMINSTER_HOUR_GAP = 1.5         # after the last change, before the first hour strike
WESTMINSTER_STRIKE_INTERVAL = 2.2  # hour strike to hour strike
CHIME_VOLUME = float(os.getenv("CHIME_VOLUME", "0.5"))  # starting volume 0..1 (on top of the system volume), until set in the UI
CHIME_PLAYER = os.getenv("CHIME_PLAYER", "").strip()    # e.g. "aplay -D plughw:CARD=UC"; default: PipeWire/Pulse/ALSA, whichever is there
CHIME_LATENCY_SECONDS = 0.12       # the player starts this late; the bells are rung this much earlier

def _clock_quiet(hour: int) -> bool:
    start, end = CLOCK_QUIET_HOURS
    return (hour >= start or hour < end) if start > end else (start <= hour < end)

def westminster_sequence(quarter: int, hour12: int) -> Tuple[List[Tuple[str, float]], List[Tuple[float, float, float]]]:
    """Light steps and bell notes (start s, Hz, strength) for one chime; quarter 4 = full hour."""
    b = WESTMINSTER_BEAT; t = 1.0
    steps: List[Tuple[str, float]] = [('off', t)]
    notes: List[Tuple[float, float, float]] = []
    up = ['green', 'green-yellow', 'all_on']
    down = ['red', 'red_and_yellow', 'all_on']
    for i, change in enumerate(WESTMINSTER_QUARTERS[quarter]):
        lamps = up if i % 2 == 0 else down
        for k, bell in enumerate(WESTMINSTER_CHANGES[change]):
            notes.append((t + k * b, WESTMINSTER_BELLS[bell], 1.0 if k < 3 else 1.15))
        steps += [(lamps[0], b), (lamps[1], b), (lamps[2], b), ('off', 2 * b)]  # the fourth note puts them out
        t += 5 * b
    if quarter == 4:
        steps.append(('off', WESTMINSTER_HOUR_GAP)); t += WESTMINSTER_HOUR_GAP
        for _ in range(hour12):
            notes.append((t, WESTMINSTER_HOUR_BELL, 1.4))
            steps += [('yellow', CLOCK_STRIKE_ON), ('off', WESTMINSTER_STRIKE_INTERVAL - CLOCK_STRIKE_ON)]
            t += WESTMINSTER_STRIKE_INTERVAL
    steps.append(('off', 1.0))
    return steps, notes

# Church-bell partials: (ratio to the strike note, loudness, decay seconds) - hum,
# prime, minor-third tierce, quint, nominal and a few upper ones. Like a real bell,
# the low hum rings on long after the bright strike tones have died away.
_BELL_PARTIALS = ((0.5, 0.25, 6.0), (1.0, 0.55, 4.0), (1.2, 0.30, 3.0), (1.5, 0.18, 2.2),
                  (2.0, 0.42, 1.8), (2.51, 0.14, 1.0), (3.0, 0.12, 0.7), (4.07, 0.07, 0.4))
BELL_RING_SECONDS = 12.0   # each strike is rendered this long...
BELL_FADE_SECONDS = 4.0    # ...and eased to silence over its last seconds, so nothing is cut off
_bell_tones: Dict[float, object] = {}  # one synthesised strike per pitch, reused

def _bell_tone(np, freq: float, rate: int):
    if freq not in _bell_tones:
        tt = np.arange(int(BELL_RING_SECONDS * rate)) / rate
        tone = sum(a * np.exp(-tt / tau) * np.sin(2 * np.pi * freq * r * tt + r) for r, a, tau in _BELL_PARTIALS)
        fade = np.clip((BELL_RING_SECONDS - tt) / BELL_FADE_SECONDS, 0.0, 1.0)
        tone *= np.minimum(tt / 0.004, 1.0) * np.sin(fade * np.pi / 2) ** 2  # 4 ms strike, smooth tail to zero
        _bell_tones[freq] = tone
    return _bell_tones[freq]

def _render_bells(np, notes: List[Tuple[float, float, float]], volume: float, rate: int = 44100):
    end = max(t for t, _, _ in notes) + BELL_RING_SECONDS  # the last bell rings out completely
    out = np.zeros(int(end * rate), dtype=np.float64)
    for start, freq, strength in notes:
        tone = _bell_tone(np, freq, rate)
        i = max(0, int((start - CHIME_LATENCY_SECONDS) * rate))
        n = min(len(tone), len(out) - i)
        out[i:i + n] += tone[:n] * strength
    peak = float(np.max(np.abs(out))) or 1.0
    return (out / peak * max(0.0, min(volume, 1.0)) * 32767).astype('<i2')

_chime_proc: Optional[subprocess.Popen] = None
_chime_lock = threading.Lock()

def stop_chime(wait: bool = True) -> None:
    """Silences the bells if they're still ringing, and collects the player once it has exited.
    wait=False only collects a player that has already finished."""
    global _chime_proc
    with _chime_lock:
        if not _chime_proc:
            return
        if _chime_proc.poll() is None:
            if not wait:
                return
            _chime_proc.terminate()
            try: _chime_proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired: _chime_proc.kill(); _chime_proc.wait()
        _chime_proc = None

def play_pcm(pcm, rate: int = 44100) -> None:
    """Writes a 16-bit mono PCM array to a WAV file and plays it in the background."""
    global _chime_proc
    stop_chime()
    import wave
    env = dict(os.environ)
    uid = os.getuid() if hasattr(os, 'getuid') else 0
    runtime = f"/run/user/{uid}" if uid else ''
    if CHIME_PLAYER:
        cmd = shlex.split(CHIME_PLAYER)
    else:
        # The service runs outside the desktop session, so point it at the user's PipeWire/Pulse.
        env.setdefault('XDG_RUNTIME_DIR', runtime)
        if shutil.which('pw-play') and os.path.exists(f"{runtime}/pipewire-0"): cmd = ['pw-play']
        elif shutil.which('paplay') and os.path.exists(f"{runtime}/pulse/native"): cmd = ['paplay']
        elif shutil.which('aplay'): cmd = ['aplay', '-q']
        else:
            logging.warning("Clock: no audio player found (pw-play, paplay or aplay)"); return
    wav_dir = runtime if (runtime and os.path.isdir(runtime)) else tempfile.gettempdir()
    path = os.path.join(wav_dir, f'traffic_light_chime_{uid}.wav')
    try:
        with _chime_lock:
            with wave.open(path, 'wb') as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm.tobytes())
            _chime_proc = subprocess.Popen(cmd + [path], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        logging.error(f"Clock: playing audio failed: {e}")

def play_chime(notes: List[Tuple[float, float, float]], volume: float) -> None:
    """Renders the bells and starts playing them now, in the background. Volume 0 = muted."""
    stop_chime()
    if volume <= 0:
        return
    try:
        import numpy as np  # type: ignore
    except ImportError:
        logging.warning("Clock: numpy missing, no bells"); return
    play_pcm(_render_bells(np, notes, volume))

def _start_clock_chime(controller, quarter: int, hour12: int) -> None:
    ms = controller.state.mode_state
    ms['clock_seq'], notes = westminster_sequence(quarter, hour12)
    play_chime(notes, controller.state.chime_volume)  # the lights start right after, in step with the bells
    ms['clock_idx'] = -1; ms['clock_step_until'] = time()  # the chime starts now (the bells too)

def handle_clock_mode(controller, elapsed: float) -> Optional[float]:
    ms = controller.state.mode_state; now = time(); dt = datetime.now()
    forced = ms.pop('clock_force', None)  # /?action=chime: ring this one now, to test
    if forced:
        ms['clock_last_key'] = (dt.date().isoformat(), dt.hour, dt.minute // 15)  # don't chime the real time on top
        _start_clock_chime(controller, *forced)
        return 0.02
    seq = ms.get('clock_seq')
    if seq:
        if now < ms.get('clock_step_until', 0.0):
            return 0.02
        idx = ms.get('clock_idx', -1) + 1
        if idx < len(seq):
            color, dur = seq[idx]
            controller.set_light_state(color)
            # Each step ends a fixed time after the chime started (not after the previous step
            # actually switched), so tick delays don't add up and the lamps stay on the bells.
            ms['clock_idx'] = idx; ms['clock_step_until'] = ms.get('clock_step_until', now) + dur
            return 0.02
        ms['clock_seq'] = None
    stop_chime(wait=False)  # the last bells may still be ringing out; collect the player once done

    q = dt.minute // 15
    key = (dt.date().isoformat(), dt.hour, q)
    if ms.get('clock_last_key') != key:
        # Entering the mode (key is None) always chimes - that's how you ask the time.
        entering = ms.get('clock_last_key') is None
        ms['clock_last_key'] = key
        if entering or not _clock_quiet(dt.hour):
            _start_clock_chime(controller, q or 4, dt.hour % 12 or 12)
            return 0.02
    controller.set_light_state('off')
    return 0.2
