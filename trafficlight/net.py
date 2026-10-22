"""Networking helpers: the shared polling loop and the network monitor."""
from __future__ import annotations
import threading
import requests
import os
import logging
from time import time
import socket
import struct
import ipaddress
from typing import Optional
from .config import NETWORK_POLL_INTERVAL, POLL_RETRY_SECONDS, POLL_STALE_INTERVALS

_sessions = threading.local()

def http() -> requests.Session:
    """This thread's HTTP session: keeps connections to the APIs open between polls
    instead of a new connection and TLS handshake for every request."""
    if not hasattr(_sessions, 's'):
        _sessions.s = requests.Session()
    return _sessions.s

def _poll(controller, name: str, fetch, interval: float, store) -> None:
    """Calls fetch() every interval and hands a good (non-empty) result to store() under the lock.
    A failed fetch keeps the last good value and retries soon (30 s, doubling up to the
    interval) - so a network hiccup, or Wi-Fi not being up yet at boot, doesn't blank a mode
    for a whole interval. Only when the value is POLL_STALE_INTERVALS intervals old is it
    dropped and the mode shows "no data"."""
    last_ok = 0.0; retry = POLL_RETRY_SECONDS; cleared = True
    while controller.state.running:
        try:
            value = fetch()
        except Exception as e:
            logging.warning(f"{name}: fetch failed: {e}"); value = None
        now = time()
        if value:
            with controller.state.lock: store(value)
            last_ok = now; retry = POLL_RETRY_SECONDS; cleared = False
            wait = interval
        else:
            if not cleared and now - last_ok > interval * POLL_STALE_INTERVALS:
                with controller.state.lock: store({})
                cleared = True
                logging.warning(f"{name}: no fresh data for {now - last_ok:.0f} s, dropped")
            wait = min(retry, interval); retry = min(retry * 2, interval)
            logging.info(f"{name}: no data, retrying in {wait:.0f} s")
        controller._stop.wait(timeout=wait)

def _get_interface_ipv4(ifname: str) -> Optional[str]:
    """Return IPv4 address for an interface (Linux), or None."""
    try:
        import fcntl  # Linux only; elsewhere (UI work on a PC) there's no interface to ask
    except ImportError:
        return None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        ifreq = struct.pack('256s', ifname.encode('utf-8')[:15])
        # SIOCGIFADDR = 0x8915
        res = fcntl.ioctl(sock.fileno(), 0x8915, ifreq)
        return socket.inet_ntoa(res[20:24])
    except OSError:
        return None
    finally:
        try:
            sock.close()
        except Exception:
            pass

def _is_network_connected() -> bool:
    """True if any non-loopback interface is up and has a non-link-local IPv4."""
    try:
        for ifname in os.listdir('/sys/class/net'):
            if ifname == 'lo':
                continue
            try:
                with open(f'/sys/class/net/{ifname}/operstate', 'r', encoding='utf-8') as f:
                    operstate = f.read().strip()
                if operstate != 'up':
                    continue
            except Exception:
                continue

            ip = _get_interface_ipv4(ifname)
            if not ip:
                continue
            try:
                addr = ipaddress.ip_address(ip)
                if addr.version == 4 and not addr.is_link_local:
                    return True
            except ValueError:
                continue
    except Exception:
        return False
    return False

def network_monitor(controller):
    # A Wi-Fi drop switches to the traffic light cycle; when it's back, the mode
    # picked before the drop returns.
    with controller.state.lock:
        prev: Optional[bool] = controller.state.network_connected  # main() already set the mode for this
    while controller.state.running:
        try:
            connected = _is_network_connected()
        except Exception:
            connected = False
        with controller.state.lock:
            controller.state.network_connected = connected
            if connected != prev:
                if connected:
                    controller.restore_choice()
                    logging.info(f"Network up -> back to {controller.state.target_mode}")
                else:
                    controller.state.target_mode = 'auto'
                    logging.info("Network down -> target auto")
                prev = connected
                controller._wake.set()
        controller._stop.wait(timeout=NETWORK_POLL_INTERVAL)
