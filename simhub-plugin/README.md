# Traffic Light Plugin for SimHub

A SimHub plugin that sends traffic light signals via UDP for racing simulation games. This plugin can operate in two modes: monitoring race flags or engine rev lights.

## Features

### Dual Mode Operation
- **Flag Mode**: Monitors race flags (yellow, green, red) from racing games
- **Rev Light Mode**: Monitors engine RPMs and triggers lights based on shift points

### Game Support
- **iRacing**: Full support with detailed flag detection and race start sequences
- **Generic**: Basic flag support for other racing games through SimHub

### Hardware Integration
- Sends UDP packets to control external LED/light hardware
- Configurable IP address and port settings
- Progressive rev light patterns (green → green+yellow → all lights)

## Installation

1. Build the project in Visual Studio
2. The post-build event will automatically copy the DLL to your SimHub installation
3. Restart SimHub
4. Enable the "Traffic Light Plugin (Flags & Revs)" in the plugins section

## Configuration

### UDP Settings
- **IP Address**: Target device IP (default: 127.0.0.1)
- **Port**: UDP port number (default: 12345)

### Mode Selection
- **Flag Mode**: Responds to race flags and start sequences
- **Rev Light Mode**: Responds to engine RPM thresholds

### Manual Testing
Use the built-in test buttons to verify your hardware setup:
- Send Red
- Send Yellow  
- Send Green
- Turn Lights Off

## UDP Protocol

The plugin sends simple UTF-8 text messages, one every tick, in the form `"<state>:<rev_pct>"` where `rev_pct` is the current RPM as a percentage of redline (0-100, e.g. `42.7`). It is sent on every tick, including `0`, so the bar always has a fresh value to display.

### Flag Mode Messages
- `"red:<rev_pct>"` - Red flag or race start preparation
- `"yellow:<rev_pct>"` - Yellow flag or race start ready
- `"green:<rev_pct>"` - Green flag or race start go
- `"black:<rev_pct>"` - All lights off

### Rev Light Mode Messages
- `"green:<rev_pct>"` - First shift light threshold reached
- `"green-yellow:<rev_pct>"` - Second shift light threshold reached
- `"all_on:<rev_pct>"` - Redline threshold reached
- `"black:<rev_pct>"` - Below all thresholds

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