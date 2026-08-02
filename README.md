# Traffic Light

A real desktop traffic light, wired to a Raspberry Pi, that lights up for
whatever actually matters that day — a train arriving, a UV warning, a
storm on the sun, or your team winning. One Python file drives the GPIO
LEDs and serves a mobile-first web UI to switch modes and watch live state
from your phone.

<p align="center">
  <img src="docs/screenshots/auto.png" width="260" alt="Auto mode">
  <img src="docs/screenshots/uv.png" width="260" alt="UV mode with value strip">
  <img src="docs/screenshots/gremio.png" width="260" alt="Grêmio easter egg">
</p>

## Features

- **12 modes**, each driving red/yellow/green from a different real-world
  signal — see the table below.
- **LED value strip** — for modes backed by a real scalar (not just a
  3-state color), a segmented bar next to the lights shows exactly where
  the value sits, always tinted to match whatever the main light is
  currently showing.
- **Grêmio easter egg** — Grêmio mode reskins the panel in the club's own
  colors and shows a face (happy / neutral / angry) built right into the
  lit circle, reacting to the last match result.
- **Modern mobile web UI** — glass-panel design, icon mode tiles, safe-area
  aware layout for iPhone home-screen shortcuts, and a live connection
  indicator.
- **Real-time, low-overhead updates** — the UI is pushed state over
  Server-Sent Events instead of polling, so it stays live without hammering
  the Pi or your phone's battery.
- **iRacing / SimHub integration** — Racing mode listens for flag colors
  (and optionally a rev/shift-light percentage) over UDP from a sim rig on
  the same network.
- **Single file, no framework** — `traffic_light_single.py` is the whole
  backend, hardware layer, and web UI (HTML/CSS/JS embedded). Nothing to
  build; just run it.

## Modes

| Mode | Lights up based on | Value strip | Needs |
|---|---|:-:|---|
| Auto | Fixed red → yellow → green → red+yellow cycle | | |
| Manual | Whatever color you tap in the UI | | |
| Emergency | Blinking yellow | | |
| SOS | Morse-style SOS blink pattern | | |
| S-Bahn | Minutes until the next train | ✓ | `DB_CLIENT_ID` / `DB_CLIENT_SECRET` |
| Stau (traffic) | Live commute delay | ✓ | `TOMTOM_API_KEY` (or `GOOGLE_MAPS_API_KEY`) |
| Biergarten | Temperature, weather, time of day | | `OWM_API_KEY` |
| UV | Current UV index | ✓ | — (Open-Meteo, free) |
| Space | Kp geomagnetic index (aurora/storm risk) | ✓ | — (NOAA, free) |
| Party | Fast random color flicker | | |
| Racing | Live flag color from iRacing via SimHub (UDP) | ✓ | a SimHub plugin sending UDP to port 9001 |
| Audio VU | Microphone level, beat-reactive | ✓ | a USB mic + `sounddevice` |
| Grêmio | Last match result (win/draw/loss) | | — (ESPN, free) |

Modes without an API key still work — they just won't have live data to
react to (S-Bahn, Stau, Biergarten specifically need a key to do anything
useful).

## Hardware

Three LEDs (or a relay board) wired to the Pi's GPIO, active-low by default:

| Color | BCM pin |
|---|---|
| Red | 22 |
| Yellow | 27 |
| Green | 17 |

Change pins/polarity at the top of `traffic_light_single.py`
(`RED_PIN`, `YELLOW_PIN`, `GREEN_PIN`, `ACTIVE_HIGH`). If GPIO init fails
(wrong pins, no Pi, running on a dev machine), it falls back to a mock
hardware layer automatically so the web UI still works.

## Setup

```bash
git clone https://github.com/tiagofranzen/pi-traffic-light.git
cd pi-traffic-light
pip install -r requirements.txt   # add --break-system-packages on newer Raspberry Pi OS
python3 traffic_light_single.py
```

Open `http://<pi-ip>:8000` — or add it to your iPhone's home screen for a
full-screen app-like shortcut.

### Environment variables (all optional)

| Variable | Enables |
|---|---|
| `DB_CLIENT_ID`, `DB_CLIENT_SECRET` | S-Bahn mode (Deutsche Bahn API) |
| `OWM_API_KEY` | Biergarten mode (OpenWeatherMap) |
| `TOMTOM_API_KEY` | Stau mode + transit commute time (free tier) |
| `GOOGLE_MAPS_API_KEY` | Stau mode fallback if no TomTom key |
| `AUDIO_INPUT_DEVICE` | Pick a specific mic for Audio VU (name substring or device index) |

## Adding a mode

1. Write `handle_<name>_mode(controller, elapsed)` next to the others —
   read whatever state you need off `controller.state`, call
   `controller.set_light_state(color)`, optionally return a custom sleep
   interval.
2. Register it in `TrafficLightController.mode_handlers`.
3. Add a button in the `mode-buttons` grid in `_HTML` (icon + label,
   `onclick="handleModeClick('name')"`).
4. If it has a real scalar value worth showing, add a case to
   `computeBarPct()` in the same file's embedded JS.

## API

- `GET /` — the web UI
- `GET /status` — current state as JSON (polling-friendly)
- `GET /events` — the same state as a Server-Sent Events stream (what the
  UI actually uses)
- `GET /?action=set_mode&mode=<name>` — switch mode (repeat to toggle off)
- `GET /?action=set_color&color=<red|yellow|green>` — manual override

## License

MIT — see [LICENSE](LICENSE).
