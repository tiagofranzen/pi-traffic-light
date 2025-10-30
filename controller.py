"""
Main traffic light controller.
Centralized management of hardware state and mode logic.
"""
import logging
from time import time, sleep
from typing import Dict, Optional, Protocol, List

from traffic_light.state import SharedState
from traffic_light.config import gpio_config, timing_config
import traffic_light.modes as modes

logger = logging.getLogger(__name__)


class LEDInterface(Protocol):
    def on(self) -> None: ...
    def off(self) -> None: ...


class HardwareLED:
    def __init__(self, pin: int, active_high: bool = False):
        try:
            from gpiozero import LED
            self._led = LED(pin, active_high=active_high)
            logger.info(f"Initialized LED on GPIO pin {pin}")
        except Exception as e:
            logger.error(f"Failed to initialize LED on pin {pin}: {e}")
            raise
    def on(self) -> None: self._led.on()
    def off(self) -> None: self._led.off()


class MockLED:
    def __init__(self, pin: int, active_high: bool = False):
        self.pin = pin
        self.active_high = active_high
        self.is_on = False
        logger.info(f"Initialized MOCK LED on pin {pin}")
    def on(self) -> None:
        self.is_on = True
        logger.debug(f"Mock LED {self.pin}: ON")
    def off(self) -> None:
        self.is_on = False
        logger.debug(f"Mock LED {self.pin}: OFF")


class LightHardware:
    def __init__(self, red_pin: int, yellow_pin: int, green_pin: int,
                 active_high: bool = False, use_mock: bool = False):
        led_class = MockLED if use_mock else HardwareLED
        self.red = led_class(red_pin, active_high)
        self.yellow = led_class(yellow_pin, active_high)
        self.green = led_class(green_pin, active_high)
        self.all_lights: List[LEDInterface] = [self.red, self.yellow, self.green]
        logger.info("Light hardware initialized successfully")
    def set_state(self, color: str) -> None:
        for l in self.all_lights: l.off()
        if color == "red": self.red.on()
        elif color == "yellow": self.yellow.on()
        elif color == "green": self.green.on()
        elif color == "red_and_yellow":
            self.red.on(); self.yellow.on()
        elif color == "all_on":
            self.red.on(); self.yellow.on(); self.green.on()
        elif color == "green-yellow":
            self.green.on(); self.yellow.on()
    def all_off(self) -> None:
        for l in self.all_lights: l.off()
    def test_sequence(self, duration: float = 0.2) -> None:
        from time import sleep as _sleep
        logger.info("Running light test sequence...")
        for l in self.all_lights:
            l.on(); _sleep(duration); l.off()
        logger.info("Light test sequence complete")
    def cleanup(self) -> None:
        try:
            import RPi.GPIO as GPIO
            self.all_off(); GPIO.cleanup(); logger.info("GPIO cleanup complete")
        except Exception as e:
            logger.warning(f"GPIO cleanup failed: {e}")


class TrafficLightController:
    """
    Main controller for the traffic light system.
    Manages hardware, state, and mode logic without global variables.
    """
    
    def __init__(self, use_mock_hardware: bool = False):
        """
        Initialize the traffic light controller.
        
        Args:
            use_mock_hardware: If True, use mock LEDs for testing
        """
        logger.info("Initializing Traffic Light Controller...")
        
        # Initialize state
        self.state = SharedState()
        
        # Initialize hardware
        self.hardware = LightHardware(
            red_pin=gpio_config.RED_PIN,
            yellow_pin=gpio_config.YELLOW_PIN,
            green_pin=gpio_config.GREEN_PIN,
            active_high=gpio_config.ACTIVE_HIGH,
            use_mock=use_mock_hardware
        )
        
        # Mode handlers mapping
        self.mode_handlers = {
            "auto": modes.handle_auto_mode,
            "party": modes.handle_party_mode,
            "emergency": modes.handle_emergency_mode,
            "sos": modes.handle_sos_mode,
            "s_bahn": modes.handle_s_bahn_mode,
            "biergarten": modes.handle_biergarten_mode,
            "racing": modes.handle_racing_mode,
            "space": modes.handle_space_mode,
            "stau": modes.handle_stau_mode
        }
        
        logger.info("Controller initialized successfully")
    
    def set_light_state(self, color: str) -> None:
        """
        Thread-safe method to set the physical light state.
        
        Args:
            color: Color/pattern to set ('red', 'yellow', 'green', etc.)
        """
        with self.state.lock:
            if self.state.current_color == color:
                logger.debug(f"Light state already {color}; skipping GPIO update")
                return
            prev = self.state.current_color
            try:
                self.hardware.set_state(color)
                self.state.current_color = color
                logger.info(f"GPIO light state changed from {prev} to {color}")
            except Exception as e:
                logger.error(f"Failed to set GPIO state to {color}: {e}")
    
    def set_mode(self, mode: str) -> None:
        """
        Thread-safe method to change operating mode.
        
        Args:
            mode: Mode to switch to
        """
        with self.state.lock:
            # Toggle off if already in this mode
            if self.state.current_mode == mode:
                self.state.target_mode = 'idle'
                logger.info(f"Toggling off mode: {mode}")
            else:
                self.state.target_mode = mode
                logger.info(f"Switching to mode: {mode}")
    
    def set_manual_color(self, color: str) -> None:
        """
        Set manual color (switches to manual mode).
        
        Args:
            color: Color to set manually
        """
        with self.state.lock:
            # Toggle off if clicking same color in manual mode
            if self.state.current_mode == 'manual' and self.state.current_color == color:
                self.state.target_manual_color = 'off'
            else:
                self.state.target_manual_color = color
            
            self.state.target_mode = 'manual'
            logger.info(f"Manual mode: {color}")
    
    def get_status(self) -> Dict:
        """
        Get current system status in a thread-safe manner.
        
        Returns:
            Dictionary containing current status
        """
        with self.state.lock:
            return self.state.get_status_snapshot()
    
    def run_initialization_sequence(self) -> None:
        """Run startup light test sequence"""
        logger.info("Running initialization sequence...")
        self.hardware.test_sequence(duration=0.2)
        logger.info("Initialization sequence complete")
    
    def run(self) -> None:
        """
        Main controller loop - manages mode transitions and delegates to handlers.
        This runs in its own thread.
        """
        logger.info("Starting main controller loop")
        
        # Initialize with green light
        with self.state.lock:
            self.set_light_state("green")
            self.state.last_state_change_time = time()
        
        while self.state.running:
            loop_sleep = timing_config.CONTROLLER_LOOP_SLEEP
            
            with self.state.lock:
                # Handle mode transitions
                if self.state.current_mode != self.state.target_mode:
                    self._transition_to_mode(self.state.target_mode)
                
                # Handle manual mode
                if self.state.current_mode == 'manual':
                    self.set_light_state(self.state.target_manual_color)
                
                # Calculate elapsed time
                elapsed = time() - self.state.last_state_change_time
                
                # Delegate to mode handler
                handler = self.mode_handlers.get(self.state.current_mode)
                if handler:
                    custom_sleep = handler(self, elapsed)
                    if custom_sleep is not None:
                        loop_sleep = custom_sleep
            
            sleep(loop_sleep)
        
        logger.info("Main controller loop stopped")
    
    def _transition_to_mode(self, new_mode: str) -> None:
        """
        Handle transition to a new mode (internal method, lock must be held).
        
        Args:
            new_mode: Mode to transition to
        """
        logger.info(f"Transitioning from {self.state.current_mode} to {new_mode}")
        
        self.state.current_mode = new_mode
        self.state.last_state_change_time = time()
        
        # Mode-specific initialization
        if new_mode == 'auto':
            self.set_light_state('red')
            self.state.mode_state['next_auto_state'] = 'red_and_yellow'
        elif new_mode == 'sos':
            self.state.mode_state['sos_index'] = 0
            self.set_light_state('off')
        elif new_mode == 'racing':
            self.state.mode_state['race_step'] = 0
            self.set_light_state('off')
        elif new_mode == 'idle':
            self.set_light_state('off')
    
    def shutdown(self) -> None:
        """Gracefully shutdown the controller"""
        logger.info("Shutting down controller...")
        
        with self.state.lock:
            self.state.running = False
        
        # Turn off all lights
        self.hardware.all_off()
        
        # Cleanup GPIO
        self.hardware.cleanup()
        
        logger.info("Controller shutdown complete")
