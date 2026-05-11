#!/bin/bash
# ============================================
#   RaanuTradingBot — one-click starter (Mac)
# ============================================

# Change to the directory containing this script
cd "$(dirname "$0")"

echo ""
echo "============================================"
echo "  RaanuTradingBot - Trade 212 Bot"
echo "============================================"
echo ""

# Prefer python3, fall back to python
PYTHON=$(command -v python3 || command -v python)
if [ -z "$PYTHON" ]; then
    echo "ERROR: Python is not installed."
    echo "Install it from https://python.org/downloads or run: brew install python"
    exit 1
fi

echo "Using Python: $PYTHON ($($PYTHON --version 2>&1))"
echo ""

# First-time setup: install dependencies if not already there
if ! $PYTHON -c "import fastapi" 2>/dev/null; then
    echo "First-time setup: installing Python packages..."
    $PYTHON -m pip install -r requirements.txt
    if [ $? -ne 0 ]; then
        echo "Package installation failed. Try: pip3 install -r requirements.txt"
        exit 1
    fi
    echo ""
fi

# Check .env exists
if [ ! -f ".env" ]; then
    echo "WARNING: .env file not found."
    echo "Copy .env.example to .env and add your Trade 212 API key:"
    echo "  cp .env.example .env"
    echo "  nano .env"
    echo ""
fi

# Open dashboard in browser after a short delay (background)
(sleep 3 && open http://localhost:8000) &

echo "Starting server..."
echo "  Dashboard: http://localhost:8000"
echo "  AlgoDash:  http://localhost:8000/algo"
echo ""
echo "Press Ctrl+C to stop."
echo ""

$PYTHON server.py
