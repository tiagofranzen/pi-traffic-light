# Traffic Light Plugin for SimHub

A SimHub plugin that sends rev lights, race start lights / flags and the hotlap delta to the traffic light over UDP. It sends everything all the time; the traffic light's Racing tile picks what to show (Start, Revs or Hotlap), so there is nothing to switch in SimHub.

## What it sends

- **Rev lights**: green at the first shift light, green + yellow at the second, all three at redline, plus the RPM as a percentage of redline for the bar.
- **Race start / flags**: iRacing's standing-start lights (red = set, yellow = ready, green = go), otherwise the yellow or green flag. Other games: yellow and green flags.
- **Hotlap**: live delta to your best lap, and whether the current lap is invalid.

## Installation

1. Build the project in Visual Studio
2. The post-build event will automatically copy the DLL to your SimHub installation
3. Restart SimHub
4. Enable the "Traffic Light Plugin (Flags & Revs)" in the plugins section

## Configuration

- **IP Address**: the traffic light's Pi (default: 127.0.0.1)
- **Port**: UDP port, 9001 on the Pi (default: 12345)

The settings page shows what is being sent right now (revs, start/flags, delta and lap validity) and has test buttons to send red, yellow, green or off.

## UDP Protocol

One UTF-8 text message per SimHub tick:

`<rev_state>:<rev_pct>:<delta>:<invalid>:<flag_state>`

- `rev_state`: `black`, `green`, `green-yellow` or `all_on` (shift lights).
- `rev_pct`: current RPM as a percentage of redline (0-100, e.g. `42.7`). Sent every tick, including `0`, so the bar always has a fresh value.
- `delta`: live delta to your best lap in seconds (e.g. `-0.284` = ahead). Empty while there is no reference lap. iRacing uses its own `LapDeltaToBestLap`; other games use SimHub's delta to the session best.
- `invalid`: `1` from the moment the current lap is invalidated until the next lap starts: off track (iRacing track surface), black flag, black flag warning, meatball, disqualification, or SimHub's own lap-invalidated flag. Otherwise `0`.
- `flag_state`: `black`, `red`, `yellow` or `green` (race start lights / flags).

The Pi also accepts shorter messages from older versions; the test buttons send a single colour.

## Technical Requirements

- .NET Framework 4.8
- SimHub installation
- Visual Studio 2017 or later for building

## Dependencies

- SimHub.Plugins
- GameReaderCommon
- WPF (Windows Presentation Foundation)

## Building

1. Clone this repository
2. Open `Traffic_Light_Plugin.sln` in Visual Studio
3. Build the solution
4. The DLL will be automatically copied to SimHub

## License

Copyright © 2025

## Contributing

Feel free to submit issues and enhancement requests!