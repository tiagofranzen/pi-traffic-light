"""Party tile (Music, VU meter): the mic, beat tracking and level metering."""
from __future__ import annotations
import os
import logging
import math
from collections import deque
from time import time
from datetime import datetime
import random
from typing import Dict, List, Optional, Tuple

AUDIO_INPUT_DEVICE = os.getenv("AUDIO_INPUT_DEVICE", "").strip()
AUDIO_RETRY_SECONDS = 5.0       # mic missing or failed: look again after this, doubling...
AUDIO_RETRY_MAX_SECONDS = 60.0  # ...up to this

# Party mode - beat-synced light show driven by the USB mic.
# Pipeline (all numpy, runs in the audio thread only while Party is active):
#   1. Onset envelope: spectral flux (sum of positive frame-to-frame rises in
#      log-magnitude) per 1024-sample block, in two bands. The bass band
#      (kick drum) carries the beat; the mid band helps on kick-less music.
#   2. Tempo: autocorrelation of the last few seconds of that envelope,
#      weighted toward ~120 BPM so it prefers the beat over 8th notes / half
#      time (same idea as librosa's tempo estimator, done incrementally).
#   3. Phase: comb-filter the envelope at the found period to locate where
#      the beats land, then predict the next ones so the relays switch *on*
#      the beat instead of a block late.
#   4. Lights: one random lamp on every predicted beat (a different one
#      each time), so the light hops in time with the music.
# The relays click when they switch and the mic hears it. The click is
# mostly above ~1 kHz, so while it rings only the mid band is masked - the
# bass band keeps running, otherwise every beat we switch on would be erased
# from the envelope and the tracker would lose its own lock.
PARTY_TEMPO_WINDOW_SECONDS = 8.0   # onset history used for tempo/phase
PARTY_MIN_HISTORY_SECONDS = 3.0    # before this, no tempo guess at all
PARTY_TEMPO_UPDATE_SECONDS = 0.5
PARTY_MIN_BPM = 60.0
PARTY_MAX_BPM = 180.0
PARTY_PRIOR_BPM = 120.0            # centre of the tempo preference
PARTY_PRIOR_OCTAVES = 1.0          # width of that preference (log2 std dev)
PARTY_LOCK_CONFIDENCE = 0.20       # normalized autocorrelation needed to lock
PARTY_UNLOCK_UPDATES = 4           # this many weak estimates in a row -> unlock
PARTY_TEMPO_CHANGE_UPDATES = 3     # a new tempo must persist this long to win
PARTY_BASS_BAND = (40.0, 200.0)
PARTY_MID_BAND = (200.0, 5000.0)
PARTY_MID_WEIGHT = 0.5
PARTY_CLICK_MASK_SECONDS = 0.20    # relay click rings ~120-200ms (measured)
PARTY_SILENCE_RMS = 0.02           # mic RMS below this = no music (mic gain 11/16: quiet room ~-45 dB, music ~-20 dB)
PARTY_CHILL_MAX_BPM = 95.0
PARTY_GROOVE_MAX_BPM = 125.0
PARTY_BEAT_LEAD_SECONDS = 0.03     # fire this early to cover relay + perception lag
PARTY_MIN_SWITCH_SECONDS = 0.33    # never switch the relays faster than this

# Music with no beat to lock onto (dense metal through a bass-heavy mic, rubato,
# ambient): react to individual hits instead. A hit is a block whose spectral
# flux stands PARTY_HIT_Z standard deviations above that band's last ~2 s.
PARTY_HIT_Z = 2.5
PARTY_HIT_TAU_SECONDS = 2.0        # how fast the per-band baseline adapts
PARTY_IDLE_DRIFT_SECONDS = 3.0     # no hits for this long: drift one step so it never freezes
PARTY_LOG_INTERVAL = 5.0           # tracker summary in the journal, for calibration

# /?action=party_record saves the last N seconds the tracker heard (relay
# clicks and all) to PARTY_RECORD_DIR, so live behaviour can be replayed offline.
PARTY_RECORD_SECONDS = 30.0
PARTY_RECORD_DIR = os.path.expanduser("~/party-cal")

# VU meter (Party sub-mode) - continuous mic-level meter.
# Level is normalized against a self-adjusting noise floor (an auto-ranging
# noise gate, not a fixed reference level), so it re-calibrates to whatever
# room/mic gain it's running in:
#   - floor tracks the recent quietest level: snaps down fast, creeps up slow.
#   - ceiling is derived from the floor by a fixed ratio, not tracked
#     independently (two adaptive envelopes chase each other and go unstable).
#   - displayed level = (raw - floor) / (floor*ratio - floor), clamped 0..1.
# Measured with Auto Gain Control off and mic gain 11/16: quiet room ~0.004-0.006 RMS.
AUDIO_VU_FLOOR_SEED = 0.005
AUDIO_VU_FLOOR_TAU_DOWN = 1.2   # seconds to mostly settle onto a new, quieter floor
AUDIO_VU_FLOOR_TAU_UP = 8.0     # seconds to drift the floor up if the room gets noisier
AUDIO_VU_CEILING_RATIO = 9.0    # "loud" = floor * this
AUDIO_VU_MIN_RANGE = 0.05       # floor..ceiling floor - guards near-zero floor

# Hysteresis bands on the normalized 0..1 scale: separate enter/exit thresholds
# per color so hovering near a boundary can't flap the relay back and forth.
AUDIO_VU_ENTER_GREEN = 0.16
AUDIO_VU_EXIT_GREEN = 0.08
AUDIO_VU_ENTER_YELLOW = 0.50
AUDIO_VU_EXIT_YELLOW = 0.36
AUDIO_VU_ENTER_ALL = 0.82
AUDIO_VU_EXIT_ALL = 0.66
AUDIO_VU_STALE_SECONDS = 0.12
AUDIO_VU_MODE_LOOP_SECONDS = 0.02
AUDIO_VU_SMOOTHING_ATTACK = 0.30
AUDIO_VU_SMOOTHING_RELEASE = 0.82

# Relay click decays to baseline within ~120-200ms (measured); mute the level
# tracker for longer than that so the click's own noise never reaches it.
AUDIO_VU_RELAY_IGNORE_SECONDS = 0.30
AUDIO_VU_MIN_STEP_SECONDS = 0.09
PARTY_LAMPS = ('red', 'yellow', 'green')

def _party_hop(controller) -> None:
    # Light one lamp, never the one already on, so every switch is visible.
    current = controller.state.current_color
    controller.set_light_state(random.choice([c for c in PARTY_LAMPS if c != current]))

def handle_party_mode(controller, elapsed: float) -> Optional[float]:
    s = controller.state; ms = s.mode_state; now = time()
    with s.lock:
        p = dict(s.party_status)
    bpm = p.get('bpm'); anchor = p.get('beat_anchor'); vibe = p.get('vibe', 'silence')

    if vibe == 'silence':
        # Nothing playing: stay dark - flickering would just fill the quiet
        # room with relay clicks.
        controller.set_light_state('off')
        return 0.05
    if not bpm or anchor is None or vibe == 'ambient':
        # Music without a beat to lock onto: hop on each hit the tracker
        # hears, or drift now and then so it never freezes.
        hit = p.get('hit_seq', 0) != ms.get('party_seen_hit')
        if hit or now - ms.get('party_last_switch', 0.0) >= PARTY_IDLE_DRIFT_SECONDS:
            ms['party_seen_hit'] = p.get('hit_seq', 0)
            _party_hop(controller); ms['party_last_switch'] = now
        return 0.01

    # Fire once per predicted beat, a little early to cover relay latency.
    period = 60.0 / bpm
    t = now + PARTY_BEAT_LEAD_SECONDS
    beat = anchor + round((t - anchor) / period) * period
    if t < beat or beat <= ms.get('party_last_beat', 0.0) + 0.5 * period:
        return 0.01
    ms['party_last_beat'] = beat
    ms['party_beat_count'] = ms.get('party_beat_count', 0) + 1
    every = 1
    while every * period < PARTY_MIN_SWITCH_SECONDS:
        every *= 2  # very fast tracks: hop on every other beat instead
    if ms['party_beat_count'] % every == 0:
        _party_hop(controller); ms['party_last_switch'] = now
    return 0.01

def handle_audio_vu_mode(controller, elapsed: float) -> Optional[float]:
    state = controller.state
    now = time()

    ignore_until = float(state.mode_state.get('audio_vu_ignore_until', 0.0) or 0.0)
    if now < ignore_until:
        return AUDIO_VU_MODE_LOOP_SECONDS

    last = state.mode_state.get('audio_vu_last', 0.0)
    level = state.audio_vu_level if (now - last) <= AUDIO_VU_STALE_SECONDS else 0.0

    # Hysteresis (a Schmitt trigger): separate enter/exit thresholds per band,
    # keyed off the color we're currently sitting in, so a level hovering near
    # a boundary can't flap the relay back and forth every cycle.
    color = state.current_color
    if color == 'off':
        target_color = 'green' if level >= AUDIO_VU_ENTER_GREEN else 'off'
    elif color == 'green':
        if level < AUDIO_VU_EXIT_GREEN:
            target_color = 'off'
        elif level >= AUDIO_VU_ENTER_YELLOW:
            target_color = 'green-yellow'
        else:
            target_color = 'green'
    elif color == 'green-yellow':
        if level < AUDIO_VU_EXIT_YELLOW:
            target_color = 'green'
        elif level >= AUDIO_VU_ENTER_ALL:
            target_color = 'all_on'
        else:
            target_color = 'green-yellow'
    else:  # all_on
        target_color = 'all_on' if level >= AUDIO_VU_EXIT_ALL else 'green-yellow'

    if target_color != state.current_color:
        last_step = float(state.mode_state.get('audio_vu_last_step_time', 0.0) or 0.0)
        if (now - last_step) < AUDIO_VU_MIN_STEP_SECONDS:
            return AUDIO_VU_MODE_LOOP_SECONDS

    controller.set_light_state(target_color)
    return AUDIO_VU_MODE_LOOP_SECONDS

class BeatTracker:
    """Live tempo/phase/energy estimate from mic blocks (see PARTY_* config)."""

    def __init__(self, np, samplerate: int, blocksize: int):
        self.np = np; self.sr = samplerate; self.bs = blocksize
        self.fps = samplerate / blocksize  # onset envelope frames per second
        self.window = np.hanning(2 * blocksize)
        freqs = np.fft.rfftfreq(2 * blocksize, 1.0 / samplerate)
        self.bass = (freqs >= PARTY_BASS_BAND[0]) & (freqs <= PARTY_BASS_BAND[1])
        self.mid = (freqs > PARTY_MID_BAND[0]) & (freqs <= PARTY_MID_BAND[1])
        self.lag_min = max(1, int(self.fps * 60.0 / PARTY_MAX_BPM))
        self.lag_max = int(math.ceil(self.fps * 60.0 / PARTY_MIN_BPM))
        lags = np.arange(self.lag_max + 2, dtype=float); lags[0] = 1.0
        # Log-normal tempo preference: halves/doubles of the beat get less weight.
        self.prior = np.exp(-0.5 * (np.log2((60.0 * self.fps / lags) / PARTY_PRIOR_BPM) / PARTY_PRIOR_OCTAVES) ** 2)
        self.maxlen = int(PARTY_TEMPO_WINDOW_SECONDS * self.fps)
        self.reset()

    def reset(self) -> None:
        np = self.np
        self.prev_block = np.zeros(self.bs, dtype=np.float32)
        self.prev_bass = None; self.prev_mid = None
        self.mid_avg = 0.0
        self.env: deque = deque(maxlen=self.maxlen); self.env_t: deque = deque(maxlen=self.maxlen)
        self.bpm: Optional[float] = None; self.confidence = 0.0
        self.anchor: Optional[float] = None
        self.pending_bpm: Optional[float] = None; self.pending_count = 0
        self.weak_count = 0
        self.last_tempo_update = 0.0
        self.short_db: Optional[float] = None; self.long_db: Optional[float] = None
        self.band_mean = [0.0, 0.0]; self.band_var = [0.0, 0.0]; self.band_n = [0, 0]
        self.last_hit = 0.0; self.hit_seq = 0; self.hit_strength = 0.0

    def _band_z(self, i: int, x: float) -> float:
        # Score against the baseline *before* this block, then fold it in.
        mean, var = self.band_mean[i], self.band_var[i]
        z = (x - mean) / math.sqrt(var) if self.band_n[i] >= self.fps and var > 1e-12 else 0.0
        a = 1.0 - math.exp(-1.0 / (PARTY_HIT_TAU_SECONDS * self.fps))
        d = x - mean
        self.band_mean[i] = mean + a * d
        self.band_var[i] = (1.0 - a) * (var + a * d * d)
        self.band_n[i] += 1
        return z

    def process(self, samples, now: float, mask_mid: bool) -> None:
        np = self.np
        frame = np.concatenate((self.prev_block, samples)) * self.window
        self.prev_block = samples.copy()
        mag = np.log1p(1000.0 * np.abs(np.fft.rfft(frame)))
        bass = mag[self.bass]; mid = mag[self.mid]
        bass_flux = float(np.mean(np.maximum(0.0, bass - self.prev_bass))) if self.prev_bass is not None else 0.0
        hit_z = self._band_z(0, bass_flux)
        if mask_mid or self.prev_mid is None:
            mid_flux = self.mid_avg  # relay click is ringing: hold the mid band at its average
        else:
            mid_flux = float(np.mean(np.maximum(0.0, mid - self.prev_mid)))
            self.mid_avg += (mid_flux - self.mid_avg) * 0.05
            hit_z = max(hit_z, self._band_z(1, mid_flux))
        # Per-band z-score so whichever band carries the attacks (bass for a
        # ballad, mids for guitars/snare) can trigger, whatever its level.
        if hit_z >= PARTY_HIT_Z and now - self.last_hit >= PARTY_MIN_SWITCH_SECONDS:
            self.last_hit = now; self.hit_seq += 1; self.hit_strength = hit_z
        self.prev_bass = bass; self.prev_mid = mid
        # The block was captured over the last bs/sr seconds; stamp its middle.
        self.env.append(bass_flux + PARTY_MID_WEIGHT * mid_flux)
        self.env_t.append(now - 0.5 * self.bs / self.sr)

        if not mask_mid:  # the click would read as loudness too
            self._update_energy(float(np.sqrt(np.mean(np.square(samples)))))
        if now - self.last_tempo_update >= PARTY_TEMPO_UPDATE_SECONDS:
            self.last_tempo_update = now
            if not self._music():
                # In a quiet room the only rhythm left is our own relay
                # clicks - don't let them become a phantom beat.
                self.bpm = None; self.anchor = None; self.confidence = 0.0
                return
            self._update_tempo()
            self._update_phase()

    def _update_energy(self, rms: float) -> None:
        db = 20.0 * math.log10(rms + 1e-9)
        dt = self.bs / self.sr
        if self.short_db is None:
            self.short_db = self.long_db = db
        self.short_db += (db - self.short_db) * (1.0 - math.exp(-dt / 0.4))
        self.long_db += (db - self.long_db) * (1.0 - math.exp(-dt / 15.0))

    def _onset_curve(self):
        # Subtract a ~0.5 s moving average and keep what sticks out, so the
        # tempo search sees the hits and not the overall loudness.
        np = self.np
        e = np.asarray(self.env, dtype=float)
        k = max(3, int(self.fps * 0.5)) | 1
        local = np.convolve(e, np.ones(k) / k, mode='same')
        return np.maximum(0.0, e - local)

    def _acf_tempo(self) -> Optional[Tuple[float, float]]:
        """(bpm, confidence) of the strongest beat period in an onset envelope."""
        np = self.np
        e = self._onset_curve(); e = e - e.mean()
        n = len(e); size = 1 << (2 * n - 1).bit_length()
        spec = np.fft.rfft(e, size)
        acf = np.fft.irfft(spec * np.conj(spec), size)[:n]
        if acf[0] <= 1e-12:
            return None
        acf = acf / acf[0]
        hi = min(self.lag_max, n // 2)
        if hi <= self.lag_min + 1:
            return None
        lags = np.arange(self.lag_min, hi + 1)
        # A true beat period also lines up at twice the lag; 8th notes don't
        # line up as well at their own double, which helps pick the beat.
        double = np.where(2 * lags < n, acf[np.minimum(2 * lags, n - 1)], 0.0)
        score = (acf[lags] + 0.5 * np.maximum(0.0, double)) * self.prior[lags]
        i = int(np.argmax(score)); lag = float(lags[i]); conf = float(acf[lags[i]])
        if 0 < i < len(lags) - 1:  # parabolic peak refinement on the raw autocorrelation
            a, b, c = acf[lags[i] - 1], acf[lags[i]], acf[lags[i] + 1]
            den = a - 2 * b + c
            if abs(den) > 1e-12:
                lag += max(-0.5, min(0.5, float(0.5 * (a - c) / den)))
        return 60.0 * self.fps / lag, conf  # plain floats: the status dict goes out as JSON

    def _update_tempo(self) -> None:
        if len(self.env) < PARTY_MIN_HISTORY_SECONDS * self.fps:
            return
        found = self._acf_tempo()
        if found is None:
            self._weak(); return
        raw, conf = found
        self.confidence = conf
        if conf < PARTY_LOCK_CONFIDENCE:
            self._weak(); return
        self.weak_count = 0
        if self.bpm is None:
            self.bpm = raw; self.pending_bpm = None
            logging.info(f"Party: locked {raw:.1f} BPM (conf {conf:.2f})")
            return
        ratio = raw / self.bpm
        if abs(ratio - 1.0) <= 0.06:
            self.bpm += (raw - self.bpm) * 0.2; self.pending_bpm = None
        elif abs(ratio - 2.0) <= 0.12 or abs(ratio - 0.5) <= 0.03:
            pass  # octave flip of the same tempo - keep what we have
        elif self.pending_bpm and abs(raw / self.pending_bpm - 1.0) <= 0.06:
            self.pending_count += 1
            if self.pending_count >= PARTY_TEMPO_CHANGE_UPDATES:
                logging.info(f"Party: tempo change {self.bpm:.1f} -> {raw:.1f} BPM")
                self.bpm = raw; self.pending_bpm = None; self.anchor = None
        else:
            self.pending_bpm = raw; self.pending_count = 1

    def _weak(self) -> None:
        self.weak_count += 1
        if self.bpm is not None and self.weak_count >= PARTY_UNLOCK_UPDATES:
            logging.info("Party: lost the beat")
            self.bpm = None; self.anchor = None

    def _update_phase(self) -> None:
        if not self.bpm:
            return
        np = self.np
        e = self._onset_curve(); n = len(e)
        period = self.fps * 60.0 / self.bpm
        beats = int((n - 1) / period)
        if beats < 2:
            return
        # Comb filter: for each offset back from the newest frame, sum the
        # envelope at every beat before it (recent beats weigh more).
        best, best_off = -1.0, 0
        weights = 0.85 ** np.arange(beats)
        for off in range(int(math.ceil(period))):
            idx = (n - 1 - off - np.round(np.arange(beats) * period)).astype(int)
            idx = idx[idx >= 0]
            sc = float(np.dot(e[idx], weights[:len(idx)]))
            if sc > best:
                best, best_off = sc, off
        found = self.env_t[n - 1 - best_off]
        p_sec = 60.0 / self.bpm
        if self.anchor is None:
            self.anchor = found; return
        # Phase-locked loop: nudge toward the new estimate instead of jumping,
        # so one sloppy estimate doesn't jerk the lights.
        projected = self.anchor + round((found - self.anchor) / p_sec) * p_sec
        err = found - projected
        self.anchor = projected + 0.35 * err

    def _music(self) -> bool:
        return self.short_db is not None and self.short_db > 20.0 * math.log10(PARTY_SILENCE_RMS)

    def status(self, now: float) -> Dict:
        silence_db = 20.0 * math.log10(PARTY_SILENCE_RMS)
        music = self._music()
        if not music:
            vibe = 'silence'
        elif not self.bpm or self.anchor is None:
            vibe = 'ambient'
        else:
            # Label only (shown in the UI) - the lights hop on every beat either way.
            vibe = 'chill' if self.bpm < PARTY_CHILL_MAX_BPM else 'groove' if self.bpm < PARTY_GROOVE_MAX_BPM else 'hype'
        level = 0.0 if self.short_db is None else max(0.0, min(1.0, (self.short_db - silence_db) / 30.0))
        return {
            'bpm': round(self.bpm, 1) if self.bpm else None,
            'confidence': round(self.confidence, 2),
            'beat_anchor': self.anchor if self.bpm else None,
            'vibe': vibe,
            'level': round(level, 2),
            'db': round(self.short_db, 1) if self.short_db is not None else None,
            'rel_db': round(self.short_db - self.long_db, 1) if self.short_db is not None else None,
            'hit_seq': self.hit_seq,
            'hit_strength': round(self.hit_strength, 1),
        }

def _update_vu_level(controller, level: float) -> None:
    now = time()
    with controller.state.lock:
        # The relay clicks audibly when the light switches - drop samples
        # taken while that click is still ringing out, or the mic picks up
        # its own relay and re-triggers another switch in a feedback loop.
        ms = controller.state.mode_state
        if now < float(ms.get('audio_vu_ignore_until', 0.0) or 0.0):
            return
        floor = float(ms.get('audio_vu_floor', AUDIO_VU_FLOOR_SEED) or AUDIO_VU_FLOOR_SEED)
        last_update = float(ms.get('audio_vu_last_update', now) or now)
        dt = max(0.0, min(now - last_update, 0.5))
        ms['audio_vu_last_update'] = now

        # Asymmetric envelope follower: floor snaps down onto quiet fast
        # but only creeps up slowly - so it settles on "recent quiet",
        # not an average that a sustained loud stretch would drag upward.
        floor_tau = AUDIO_VU_FLOOR_TAU_DOWN if level < floor else AUDIO_VU_FLOOR_TAU_UP
        floor += (level - floor) * (1.0 - math.exp(-dt / floor_tau))
        ms['audio_vu_floor'] = floor

        ceiling = max(floor * AUDIO_VU_CEILING_RATIO, floor + AUDIO_VU_MIN_RANGE)
        target = max(0.0, min((level - floor) / (ceiling - floor), 1.0))
        prev = controller.state.audio_vu_level
        smoothing = AUDIO_VU_SMOOTHING_ATTACK if target >= prev else AUDIO_VU_SMOOTHING_RELEASE
        controller.state.audio_vu_level = max(0.0, (prev * smoothing) + (target * (1.0 - smoothing)))
        ms['audio_vu_last'] = now

def _save_party_recording(np, blocks: List, samplerate: int) -> None:
    import wave
    try:
        os.makedirs(PARTY_RECORD_DIR, exist_ok=True)
        path = os.path.join(PARTY_RECORD_DIR, datetime.now().strftime("live-%Y%m%d-%H%M%S.wav"))
        pcm = (np.clip(np.concatenate(blocks), -1.0, 1.0) * 32767).astype('<i2')
        with wave.open(path, 'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(samplerate); w.writeframes(pcm.tobytes())
        logging.info(f"Party: saved {len(pcm) / samplerate:.1f}s to {path}")
    except Exception as e:
        logging.error(f"Party: recording failed: {e}")

def audio_monitor(controller):
    try:
        import sounddevice as sd  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        logging.warning("Party audio disabled: install sounddevice and numpy")
        return

    blocksize = 1024

    def _resolve_input_device() -> Tuple[Optional[int], Optional[str]]:
        try:
            devices = sd.query_devices()
        except Exception as e:
            logging.error(f"Audio: failed to query devices: {e}")
            return None, None

        def _is_valid_input(idx: int) -> bool:
            if idx < 0 or idx >= len(devices):
                return False
            d = devices[idx]
            return int(d.get('max_input_channels', 0)) > 0

        configured = AUDIO_INPUT_DEVICE
        if configured:
            if configured.isdigit():
                idx = int(configured)
                if _is_valid_input(idx):
                    return idx, str(devices[idx].get('name', f'device-{idx}'))
                logging.warning(f"Audio: AUDIO_INPUT_DEVICE index {idx} is not a valid input device")
            else:
                wanted = configured.lower()
                for idx, dev in enumerate(devices):
                    name = str(dev.get('name', ''))
                    if wanted in name.lower() and _is_valid_input(idx):
                        return idx, name
                logging.warning(f"Audio: AUDIO_INPUT_DEVICE '{configured}' not found among input devices")

        try:
            default_in = sd.default.device[0]
            if isinstance(default_in, int) and _is_valid_input(default_in):
                return default_in, str(devices[default_in].get('name', f'device-{default_in}'))
        except Exception:
            pass

        preferred_terms = ('usb', 'mic', 'microphone', 'audio')
        for idx, dev in enumerate(devices):
            name = str(dev.get('name', ''))
            lname = name.lower()
            if _is_valid_input(idx) and any(t in lname for t in preferred_terms):
                return idx, name

        for idx in range(len(devices)):
            if _is_valid_input(idx):
                return idx, str(devices[idx].get('name', f'device-{idx}'))

        return None, None

    def _candidate_sample_rates(device_index: int) -> List[int]:
        candidates: List[int] = []
        try:
            dev_info = sd.query_devices(device_index)
            default_rate = int(round(float(dev_info.get('default_samplerate', 0))))
            if default_rate > 0:
                candidates.append(default_rate)
        except Exception:
            pass

        for rate in (48000, 44100, 32000, 24000, 22050, 16000, 11025, 8000):
            if rate not in candidates:
                candidates.append(rate)
        return candidates

    def _listen(stream, samplerate: int, device_index: int, device_name: str) -> None:
        """Reads the mic until shutdown; raises if the stream fails (mic unplugged)."""
        tracker = BeatTracker(np, samplerate, blocksize)
        active = False; last_log = 0.0
        recent: deque = deque(maxlen=int(PARTY_RECORD_SECONDS * samplerate / blocksize))
        logging.info(
            f"Audio monitor started (device={device_index}: {device_name}, rate={samplerate}Hz)"
        )
        while controller.state.running:
            data, overflowed = stream.read(blocksize)
            if overflowed:
                logging.debug(f"Audio input overflow: {overflowed}")
            if data.size == 0:
                continue
            samples = data[:, 0] if data.ndim > 1 else data
            now = time()
            with controller.state.lock:
                mode = controller.state.current_mode
                party = mode == 'party'
                click = now < controller.state.mode_state.get('click_until', 0.0)
                if not party and active:
                    controller.state.party_status = {}
            if mode == 'audio_vu':
                rms = float(np.sqrt(np.mean(np.square(samples))))
                peak = float(np.max(np.abs(samples)))
                _update_vu_level(controller, max(rms, peak * 0.35))
            # Only analyse while Party is on; the stream keeps running
            # so the mic doesn't need re-opening every time.
            if not party:
                active = False
                continue
            if not active:
                tracker.reset(); active = True; recent.clear()
            recent.append(samples.copy())
            with controller.state.lock:
                do_record = controller.state.mode_state.pop('party_record', False)
            if do_record:
                _save_party_recording(np, recent, samplerate)
            try:
                tracker.process(samples, now, mask_mid=click)
                status = tracker.status(now)
            except Exception as e:
                # Keep the stream alive; start the analysis over.
                logging.error(f"Party tracker error: {e}")
                tracker.reset(); continue
            with controller.state.lock:
                controller.state.party_status = status
            if now - last_log >= PARTY_LOG_INTERVAL:
                last_log = now
                logging.info(
                    f"Party: vibe={status['vibe']} bpm={status['bpm']} conf={status['confidence']} "
                    f"db={status['db']} rel={status['rel_db']}"
                )

    # Find the mic and open it; if it's missing or fails later (unplugged), look
    # again with a growing pause instead of leaving Party and VU dead until a restart.
    retry = AUDIO_RETRY_SECONDS
    try:
        while controller.state.running:
            device_index, device_name = _resolve_input_device()
            if device_index is None:
                logging.warning("Audio: no input-capable audio device found")
            else:
                last_error: Optional[Exception] = None
                tried_rates = _candidate_sample_rates(device_index)
                for samplerate in tried_rates:
                    try:
                        stream = sd.InputStream(device=device_index, channels=1, samplerate=samplerate,
                                                blocksize=blocksize, dtype='float32', latency='low')
                    except Exception as e:
                        last_error = e
                        logging.warning(f"Audio: failed opening InputStream at {samplerate}Hz on device {device_index} ({device_name}): {e}")
                        continue
                    try:
                        with stream:
                            retry = AUDIO_RETRY_SECONDS
                            _listen(stream, samplerate, device_index, str(device_name))
                    except Exception as e:
                        logging.error(f"Audio: stream on {device_name} stopped: {e}")
                    break
                else:
                    logging.error(f"Audio: unable to open device {device_index} ({device_name}), tried rates={tried_rates}. Last error: {last_error}")
            if not controller.state.running:
                break
            with controller.state.lock:
                controller.state.party_status = {}
            logging.info(f"Audio: looking for the mic again in {retry:.0f} s")
            if controller._stop.wait(timeout=retry):
                break
            retry = min(retry * 2, AUDIO_RETRY_MAX_SECONDS)
            try:  # PortAudio only sees a re-plugged device after a rescan
                sd._terminate(); sd._initialize()
            except Exception:
                pass
    finally:
        with controller.state.lock:
            controller.state.party_status = {}
