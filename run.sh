#!/bin/bash
# Quick start script for Traffic Light Control System

set -e  # Exit on error

echo "============================================"
echo "Traffic Light Control System - Quick Start"
echo "============================================"
echo ""

# Check if running on Raspberry Pi
if [ ! -d "/sys/class/gpio" ]; then
    echo "⚠️  Warning: GPIO not detected. Will use mock hardware."
    echo ""
fi

# Check environment variables
echo "🔍 Checking environment variables..."
missing_vars=0

if [ -z "$DB_CLIENT_ID" ]; then
    echo "  ❌ DB_CLIENT_ID not set (S-Bahn monitor will be disabled)"
    missing_vars=$((missing_vars + 1))
else
    echo "  ✅ DB_CLIENT_ID set"
fi

if [ -z "$DB_CLIENT_SECRET" ]; then
    echo "  ❌ DB_CLIENT_SECRET not set (S-Bahn monitor will be disabled)"
    missing_vars=$((missing_vars + 1))
else
    echo "  ✅ DB_CLIENT_SECRET set"
fi

if [ -z "$OWM_API_KEY" ]; then
    echo "  ❌ OWM_API_KEY not set (Weather monitor will be disabled)"
    missing_vars=$((missing_vars + 1))
else
    echo "  ✅ OWM_API_KEY set"
fi

if [ -z "$GOOGLE_MAPS_API_KEY" ]; then
    echo "  ❌ GOOGLE_MAPS_API_KEY not set (Traffic monitor will be disabled)"
    missing_vars=$((missing_vars + 1))
else
    echo "  ✅ GOOGLE_MAPS_API_KEY set"
fi

echo ""

if [ $missing_vars -gt 0 ]; then
    echo "ℹ️  $missing_vars API key(s) missing. Some monitors will be disabled."
    echo "   Set them in ~/traffic_light_env.sh and run: source ~/traffic_light_env.sh"
    echo ""
fi

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Check Python version
echo "🐍 Checking Python..."
python3_version=$(python3 --version 2>&1)
echo "  Using: $python3_version"
echo ""

# Check dependencies
echo "📦 Checking dependencies..."
if python3 -c "import gpiozero" 2>/dev/null; then
    echo "  ✅ gpiozero installed"
else
    echo "  ❌ gpiozero not found. Install with: pip3 install gpiozero"
    exit 1
fi

if python3 -c "import requests" 2>/dev/null; then
    echo "  ✅ requests installed"
else
    echo "  ❌ requests not found. Install with: pip3 install requests"
    exit 1
fi

echo ""
echo "============================================"
echo "🚀 Starting Traffic Light Control System..."
echo "============================================"
echo ""
echo "📍 Web Interface: http://$(hostname -I | awk '{print $1}'):8000"
echo "📝 Logs: ~/traffic_light_logs/traffic_light.log"
echo ""
echo "Press Ctrl+C to stop"
echo ""

# Run the main application
cd "$SCRIPT_DIR/.."
exec python3 traffic_light/main.py
