"""The three lamps: real GPIO through gpiozero, or a mock when there is no Pi."""
from __future__ import annotations
from time import sleep
from typing import List, Protocol
from .config import ACTIVE_HIGH, GREEN_PIN, RED_PIN, YELLOW_PIN

LIGHT_STATES = ('off', 'red', 'yellow', 'green', 'red_and_yellow', 'green-yellow', 'all_on')
_STATE_MAP = {
    'off':            (False, False, False),
    'red':            (True,  False, False),
    'yellow':         (False, True,  False),
    'green':          (False, False, True),
    'red_and_yellow': (True,  True,  False),
    'green-yellow':   (False, True,  True),
    'all_on':         (True,  True,  True),
}

class LEDInterface(Protocol):
    def on(self) -> None: ...
    def off(self) -> None: ...
    def close(self) -> None: ...

class HardwareLED:
    def __init__(self, pin: int, active_high: bool = False):
        from gpiozero import LED  # Lazy import
        self._led = LED(pin, active_high=active_high)
    def on(self) -> None: self._led.on()
    def off(self) -> None: self._led.off()
    def close(self) -> None: self._led.close()

class MockLED:
    def __init__(self, pin: int, active_high: bool = False):
        self.pin = pin; self.active_high = active_high; self.is_on = False
    def on(self) -> None: self.is_on = True
    def off(self) -> None: self.is_on = False
    def close(self) -> None: pass

class LightHardware:
    def __init__(self, use_mock: bool = False):
        led_cls = MockLED if use_mock else HardwareLED
        created: List[LEDInterface] = []
        try:
            self.red = led_cls(RED_PIN, ACTIVE_HIGH); created.append(self.red)
            self.yellow = led_cls(YELLOW_PIN, ACTIVE_HIGH); created.append(self.yellow)
            self.green = led_cls(GREEN_PIN, ACTIVE_HIGH); created.append(self.green)
        except Exception:
            # Release any pins this attempt already claimed so a retry isn't
            # blocked by our own leaked handles on top of the original error.
            for led in created:
                try: led.close()
                except Exception: pass
            raise
        self.all_lights: List[LEDInterface] = [self.red, self.yellow, self.green]
        self._active_tuple = (False, False, False)
    def set_state(self, color: str) -> None:
        target = _STATE_MAP.get(color, (False, False, False))
        for led, was_on, want_on in zip(self.all_lights, self._active_tuple, target):
            if was_on and not want_on:
                led.off()
        for led, was_on, want_on in zip(self.all_lights, self._active_tuple, target):
            if not was_on and want_on:
                led.on()
        self._active_tuple = target
    def all_off(self) -> None:
        for l in self.all_lights: l.off()
        self._active_tuple = (False, False, False)
    def test_sequence(self, duration: float = 0.15) -> None:
        for l in self.all_lights: l.on(); sleep(duration); l.off()
        self._active_tuple = (False, False, False)
    def cleanup(self) -> None:
        # The pins belong to gpiozero, so it releases them (RPi.GPIO doesn't know them, and doesn't work on a Pi 5).
        for led in self.all_lights:
            try: led.off(); led.close()
            except Exception: pass
