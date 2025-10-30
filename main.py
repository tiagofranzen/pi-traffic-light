#!/usr/bin/env python3
"""
Traffic Light Control System
Main entry point with proper logging and signal handling.
"""
import logging
import signal
import sys
import threading
from pathlib import Path

# Add parent directory to path to allow imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from traffic_light.controller import TrafficLightController
from traffic_light import monitors
from traffic_light.web_server import run_web_server


def setup_logging(log_level: str = "INFO") -> None:
    """
    Configure logging system.
    
    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
    """
    # Create logs directory if it doesn't exist
    log_dir = Path.home() / "traffic_light_logs"
    log_dir.mkdir(exist_ok=True)
    
    log_file = log_dir / "traffic_light.log"
    
    # Configure logging
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout)
        ]
    )
    
    # Reduce noise from some modules
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)
    
    logger = logging.getLogger(__name__)
    logger.info(f"Logging initialized - log file: {log_file}")


def main():
    """Main entry point"""
    # Setup logging
    setup_logging()
    logger = logging.getLogger(__name__)
    
    logger.info("=" * 60)
    logger.info("Traffic Light Control System Starting")
    logger.info("=" * 60)
    
    # Initialize controller
    try:
        controller = TrafficLightController(use_mock_hardware=False)
    except Exception as e:
        logger.error(f"Failed to initialize controller: {e}")
        logger.info("Trying with mock hardware for testing...")
        try:
            controller = TrafficLightController(use_mock_hardware=True)
        except Exception as e2:
            logger.error(f"Failed to initialize with mock hardware: {e2}")
            sys.exit(1)
    
    # Setup signal handlers for graceful shutdown
    def signal_handler(sig, frame):
        logger.info(f"Received signal {sig}, shutting down...")
        controller.shutdown()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Run initialization sequence
    try:
        controller.run_initialization_sequence()
    except Exception as e:
        logger.error(f"Initialization sequence failed: {e}")
    
    # Start all background threads
    threads = [
        threading.Thread(target=controller.run, name="Controller", daemon=True),
        threading.Thread(target=monitors.s_bahn_monitor, args=(controller,), name="S-Bahn", daemon=True),
        threading.Thread(target=monitors.weather_monitor, args=(controller,), name="Weather", daemon=True),
        threading.Thread(target=monitors.space_weather_monitor, args=(controller,), name="Space", daemon=True),
        threading.Thread(target=monitors.traffic_monitor, args=(controller,), name="Traffic", daemon=True),
        threading.Thread(target=monitors.iracing_udp_listener, args=(controller,), name="iRacing", daemon=True),
    ]
    
    logger.info("Starting background threads...")
    for thread in threads:
        thread.start()
        logger.info(f"  Started: {thread.name}")
    
    logger.info("All threads started successfully")
    logger.info("=" * 60)
    
    # Run web server (blocking)
    try:
        run_web_server(controller)
    except Exception as e:
        logger.error(f"Web server error: {e}")
        controller.shutdown()
        sys.exit(1)


if __name__ == "__main__":
    main()
