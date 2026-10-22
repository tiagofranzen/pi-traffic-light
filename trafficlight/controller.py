"""The controller loop that runs the modes, and the program start."""
from __future__ import annotations
import os
import sys
import signal
import logging
import tempfile
import threading
from time import time, sleep
import json
from typing import Dict, Optional
from .clock import CHIME_LATENCY_SECONDS, WESTMINSTER_BELLS, handle_clock_mode, play_chime, stop_chime
from .commute import handle_s_bahn_mode, handle_stau_mode, s_bahn_monitor, traffic_monitor, transit_monitor
from .config import CONTROLLER_LOOP_SLEEP, LAST_MODE_FILE
from .gremio import gremio_monitor, handle_gremio_mode
from .hardware import LIGHT_STATES, LightHardware
from .modes import handle_auto_mode, handle_emergency_mode, handle_sos_mode
from .net import _is_network_connected, network_monitor
from .party import AUDIO_VU_FLOOR_SEED, AUDIO_VU_RELAY_IGNORE_SECONDS, PARTY_CLICK_MASK_SECONDS, audio_monitor, handle_audio_vu_mode, handle_party_mode
from .racing import RACING_INTROS, handle_hotlap_mode, handle_race_start_mode, handle_racing_mode, simhub_udp_listener
from .state import SharedState
from .weather import environment_monitor, handle_biergarten_mode, handle_rain_mode, handle_space_mode, handle_temp_mode, handle_uv_mode, space_weather_monitor
from .web import _start_web

class TrafficLightController:
    def __init__(self, use_mock_hardware: bool = False):
        self.state = SharedState()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.hardware = self._init_hardware(use_mock_hardware)
        self.mode_handlers = {
            'auto': handle_auto_mode,
            'party': handle_party_mode,
            'emergency': handle_emergency_mode,
            'sos': handle_sos_mode,
            's_bahn': handle_s_bahn_mode,
            'biergarten': handle_biergarten_mode,
            'racing': handle_racing_mode,
            'hotlap': handle_hotlap_mode,
            'race_start': handle_race_start_mode,
            'space': handle_space_mode,
            'stau': handle_stau_mode,
            'uv': handle_uv_mode,
            'gremio': handle_gremio_mode,
            'clock': handle_clock_mode,
            'audio_vu': handle_audio_vu_mode,
            'temp': handle_temp_mode,
            'rain': handle_rain_mode,
        }
    def _init_hardware(self, use_mock_hardware: bool) -> LightHardware:
        if use_mock_hardware:
            return LightHardware(use_mock=True)
        # gpiozero/lgpio has a quirk where a *failed* pin claim (e.g. the previous
        # process's GPIO lines not released yet) can leave that pin's internal
        # registry entry permanently stuck for the rest of this process's life —
        # retrying the construction again never clears it. So the real fix is to
        # avoid ever failing the first attempt: give the kernel a moment up front.
        sleep(1.0)
        last_error: Optional[Exception] = None
        attempts = 15
        for attempt in range(attempts):
            try:
                hardware = LightHardware(use_mock=False)
                logging.info("Hardware initialized (mock=False)")
                return hardware
            except Exception as e:
                last_error = e
                logging.warning(f"Hardware init attempt {attempt + 1}/{attempts} failed: {e}; retrying")
                try:
                    # A failed construction can leave gpiozero's own internal pin
                    # registry holding a stale entry we never got a reference to
                    # (and so can't .close() ourselves); reset it before retrying.
                    from gpiozero import Device
                    Device.close_all()
                except Exception:
                    pass
                sleep(0.5)
        logging.error(f"Hardware init failed after retries: {last_error}; using mock")
        return LightHardware(use_mock=True)
    def set_light_state(self, color: str) -> None:
        with self.state.lock:
            if self.state.current_color == color or not self.state.running: return
            prev = self.state.current_color
            try:
                self.hardware.set_state(color); self.state.current_color = color
                # The relay click is audible to the mic; Party masks it out,
                # the VU meter ignores the mic until it has rung out.
                now = time()
                self.state.mode_state['click_until'] = now + PARTY_CLICK_MASK_SECONDS
                if self.state.current_mode == 'audio_vu':
                    self.state.mode_state['audio_vu_last_step_time'] = now
                    self.state.mode_state['audio_vu_ignore_until'] = now + AUDIO_VU_RELAY_IGNORE_SECONDS
                logging.info(f"Light: {prev} -> {color}")
            except Exception as e:
                logging.error(f"Failed to set light {color}: {e}")
    def set_mode(self, mode: str) -> None:
        if mode not in self.mode_handlers and mode not in ('manual', 'idle'):
            raise ValueError(f"unknown mode {mode!r}")
        with self.state.lock:
            if self.state.current_mode == mode:
                self.state.target_mode = 'idle'; logging.info(f"Toggle off mode {mode}")
            else:
                self.state.target_mode = mode; logging.info(f"Switch to mode {mode}")
            self._remember_choice()
        self._wake.set()
    def set_manual_color(self, color: str) -> None:
        if color not in LIGHT_STATES:
            raise ValueError(f"unknown color {color!r}")
        with self.state.lock:
            if self.state.current_mode == 'manual' and self.state.current_color == color:
                self.state.target_manual_color = 'off'
            else:
                self.state.target_manual_color = color
            self.state.target_mode = 'manual'; logging.info(f"Manual color {color}")
            self._remember_choice()
        self._wake.set()
    def _remember_choice(self) -> None:
        # Caller holds the lock.
        self.state.chosen_mode = self.state.target_mode; self.state.chosen_color = self.state.target_manual_color
        self._save_settings()
    def _save_settings(self) -> None:
        # Caller holds the lock.
        payload = {
            'mode': self.state.chosen_mode,
            'color': self.state.chosen_color,
            'chime_volume': self.state.chime_volume,
            'chime_volume_on': self.state.chime_volume_on,
        }
        try:
            target_dir = os.path.dirname(os.path.abspath(LAST_MODE_FILE)) or "."
            fd, tmp_path = tempfile.mkstemp(prefix=".tl_mode_", suffix=".tmp", dir=target_dir)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(payload, f)
                os.replace(tmp_path, LAST_MODE_FILE)
            except Exception:
                try: os.unlink(tmp_path)
                except OSError: pass
                raise
        except OSError as e:
            logging.warning(f"Could not save the mode to {LAST_MODE_FILE}: {e}")
    def load_choice(self) -> None:
        try:
            with open(LAST_MODE_FILE, encoding='utf-8') as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return
        mode, color = saved.get('mode'), saved.get('color', 'off')
        if mode == 'westminster':
            mode = 'clock'  # was a separate Clock sub-mode
        with self.state.lock:
            for key in ('chime_volume', 'chime_volume_on'):
                if isinstance(saved.get(key), (int, float)):
                    setattr(self.state, key, max(0.0, min(float(saved[key]), 1.0)))
        if (mode in self.mode_handlers or mode in ('manual', 'idle')) and color in LIGHT_STATES:
            with self.state.lock:
                self.state.chosen_mode, self.state.chosen_color = mode, color
            logging.info(f"Last mode: {mode}" + (f" ({color})" if mode == 'manual' else ''))
    def restore_choice(self) -> None:
        # Caller holds the lock.
        self.state.target_mode = self.state.chosen_mode; self.state.target_manual_color = self.state.chosen_color
    def set_chime_volume(self, volume: Optional[float], preview: bool = False) -> None:
        """volume 0..1 (0 = mute); None toggles mute. preview rings one bell at the new level."""
        with self.state.lock:
            if volume is None:
                volume = 0.0 if self.state.chime_volume > 0 else self.state.chime_volume_on
            volume = max(0.0, min(volume, 1.0))
            self.state.chime_volume = volume
            if volume > 0:
                self.state.chime_volume_on = volume
            self._save_settings()
            chiming = bool(self.state.mode_state.get('clock_seq'))
        logging.info(f"Clock volume {round(volume * 100)}%")
        if preview and volume > 0 and not chiming:
            play_chime([(CHIME_LATENCY_SECONDS, WESTMINSTER_BELLS[0], 1.0)], volume)
    def test_chime(self, quarter: int, hour12: int) -> None:
        with self.state.lock:
            self.state.mode_state['clock_force'] = (quarter, hour12)
            if self.state.target_mode != 'clock':
                self.state.target_mode = 'clock'; self._remember_choice()
        self._wake.set()
    def trigger_race_start(self) -> None:
        with self.state.lock:
            self.state.mode_state['race_start_force'] = True
            if self.state.target_mode != 'race_start':
                self.state.target_mode = 'race_start'; self._remember_choice()
        self._wake.set()
    def get_status(self) -> Dict:
        with self.state.lock: return self.state.snapshot()
    def run_initialization_sequence(self):
        logging.info("Init sequence...")
        self.hardware.test_sequence(); logging.info("Init sequence done")
    def run(self):
        logging.info("Controller loop start")
        with self.state.lock:
            # Initialize to the requested target mode to avoid a brief startup flash.
            if self.state.target_mode == 'manual':
                self.state.current_mode = 'manual'
                self.set_light_state(self.state.target_manual_color)
            elif self.state.target_mode == 'idle':
                self.state.current_mode = 'idle'
                self.set_light_state('off')
            else:
                # Default behavior: start green and let the controller loop/handlers take over.
                self.set_light_state('green')
            self.state.last_state_change_time = time()
        while self.state.running:
            self._wake.clear()
            slp = CONTROLLER_LOOP_SLEEP
            with self.state.lock:
                if self.state.current_mode != self.state.target_mode:
                    self._transition_to_mode(self.state.target_mode)
                if self.state.current_mode == 'manual':
                    self.set_light_state(self.state.target_manual_color)
                elapsed = time() - self.state.last_state_change_time
                handler = self.mode_handlers.get(self.state.current_mode)
            # Run handler without holding the big lock so HTTP /status
            # requests aren't blocked by per-tick work. set_light_state and
            # other state writers acquire the lock themselves as needed.
            if handler:
                cs = handler(self, elapsed)
                if cs is not None:
                    slp = cs
            self._wake.wait(timeout=slp)
        logging.info("Controller loop stopped")
    def _transition_to_mode(self, new_mode: str):
        logging.info(f"Transition {self.state.current_mode} -> {new_mode}")
        stop_chime()  # leaving Clock mid-chime silences the bells
        self.state.current_mode = new_mode; self.state.last_state_change_time = time()
        if new_mode == 'auto':
            # Start in green so the first visible cycle isn't a 20s red hold.
            self.set_light_state('green'); self.state.mode_state['next_auto_state'] = 'red'
        elif new_mode == 'sos':
            self.state.mode_state['sos_index'] = 0
            # Apply the first pattern step on entry; handler advances after its duration.
            self.set_light_state(self.state.mode_state['sos_pattern'][0]['state'])
        elif new_mode == 'race_start':
            self.state.mode_state.update(race_start_force=True, race_prev_flag='black', intro_seq=None)
        elif new_mode in RACING_INTROS:
            self.state.mode_state.update(intro_seq=RACING_INTROS[new_mode], intro_idx=-1, intro_until=0.0)
        elif new_mode == 'audio_vu':
            now = time(); ms = self.state.mode_state
            self.set_light_state('off'); self.state.audio_vu_level = 0.0
            ms.update(audio_vu_last=now, audio_vu_ignore_until=now + AUDIO_VU_RELAY_IGNORE_SECONDS,
                      audio_vu_last_step_time=now, audio_vu_floor=AUDIO_VU_FLOOR_SEED, audio_vu_last_update=0.0)
        elif new_mode == 'party':
            self.state.party_status = {}
            for k in ('party_last_beat', 'party_last_switch'): self.state.mode_state[k] = 0.0
            self.state.mode_state['party_beat_count'] = 0
        elif new_mode == 'clock':
            # No last chime yet: the handler chimes the current time right away.
            self.state.mode_state['clock_seq'] = None; self.state.mode_state['clock_last_key'] = None
        elif new_mode == 'idle':
            self.set_light_state('off')
    def shutdown(self):
        logging.info("Shutdown initiated")
        stop_chime()
        # Under the lock, so no handler is mid-switch; set_light_state refuses once running is off.
        with self.state.lock:
            self.state.running = False
            self.hardware.cleanup()
        self._stop.set()
        self._wake.set()
        logging.info("Shutdown complete")

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.info("Starting single-file traffic light")
    ctrl = TrafficLightController(use_mock_hardware=False)

    # Prime network state once before threads start so the first tick has
    # a correct target mode; the network_monitor thread keeps it in sync.
    # Offline it runs the traffic light cycle; online, whatever was picked last.
    ctrl.load_choice()
    try:
        connected = _is_network_connected()
        with ctrl.state.lock:
            ctrl.state.network_connected = connected
            if connected:
                ctrl.restore_choice()
            else:
                ctrl.state.target_mode = 'auto'
    except Exception:
        pass

    try:
        ctrl.run_initialization_sequence()
    except Exception as e:
        logging.error(f"Init sequence error: {e}")
    stop_event = threading.Event()
    def _sig(sig, frame):
        logging.info(f"Signal {sig} received")
        stop_event.set()
    signal.signal(signal.SIGINT, _sig); signal.signal(signal.SIGTERM, _sig)
    threads = [
        threading.Thread(target=ctrl.run, name='controller', daemon=True),
        threading.Thread(target=s_bahn_monitor, args=(ctrl,), name='s_bahn', daemon=True),
        threading.Thread(target=space_weather_monitor, args=(ctrl,), name='space', daemon=True),
        threading.Thread(target=traffic_monitor, args=(ctrl,), name='traffic', daemon=True),
        threading.Thread(target=simhub_udp_listener, args=(ctrl,), name='simhub', daemon=True),
        threading.Thread(target=audio_monitor, args=(ctrl,), name='audio', daemon=True),
        threading.Thread(target=network_monitor, args=(ctrl,), name='network', daemon=True),
        threading.Thread(target=transit_monitor, args=(ctrl,), name='transit', daemon=True),
        threading.Thread(target=environment_monitor, args=(ctrl,), name='environment', daemon=True),
        threading.Thread(target=gremio_monitor, args=(ctrl,), name='gremio', daemon=True),
        threading.Thread(target=_start_web, args=(ctrl,), name='web', daemon=True),  # web blocks; daemon so shutdown doesn't hang
    ]
    for t in threads:
        t.start(); logging.info(f"Started thread {t.name}")
    try:
        while not stop_event.is_set():
            stop_event.wait(timeout=1.0)
    except KeyboardInterrupt:
        pass
    ctrl.shutdown()
    for t in threads:
        if t.name in ('controller', 'audio') and t.is_alive():
            t.join(timeout=2.0)
