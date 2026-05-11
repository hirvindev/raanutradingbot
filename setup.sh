#!/bin/bash
# ============================================
#   RaanuTradingBot — first-time Mac setup
# ============================================

cd "$(dirname "$0")"

echo ""
echo "============================================"
echo "  RaanuTradingBot — Mac Setup"
echo "============================================"
echo ""

PYTHON=$(command -v python3 || command -v python)
if [ -z "$PYTHON" ]; then
    echo "ERROR: Python not found."
    echo "Install from https://python.org/downloads or: brew install python"
    exit 1
fi

# [1/4] Install Python packages
echo "[1/4] Installing Python packages..."
$PYTHON -m pip install -r requirements.txt
if [ $? -ne 0 ]; then
    echo "pip install failed. Try running: pip3 install -r requirements.txt"
    exit 1
fi
echo "      Done."
echo ""

# [2/4] Create .env if missing
echo "[2/4] Checking .env file..."
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo "      Created .env from .env.example"
        echo "      >>> Edit .env and add your Trade 212 API key <<<"
    else
        cat > .env << 'EOF'
T212_API_KEY=your_api_key_here
T212_MODE=demo
EOF
        echo "      Created .env — edit it and add your T212 API key."
    fi
else
    echo "      .env already exists — skipping."
fi
echo ""

# [3/4] Fix T212_MODE if set to 'practice'
echo "[3/4] Checking T212_MODE in .env..."
if grep -q "T212_MODE=practice" .env; then
    sed -i '' 's/T212_MODE=practice/T212_MODE=demo/' .env
    echo "      Fixed: changed T212_MODE=practice → T212_MODE=demo"
else
    echo "      OK."
fi
echo ""

# [4/4] Make scripts executable
echo "[4/4] Making shell scripts executable..."
chmod +x start.sh setup.sh
echo "      Done."
echo ""

echo "============================================"
echo "  Setup complete!"
echo "============================================"
echo ""
echo "  Next steps:"
echo "  1. Edit .env and set your Trade 212 API key:"
echo "       nano .env"
echo ""
echo "  2. Start the bot:"
echo "       ./start.sh"
echo ""
echo "  Dashboard will open at: http://localhost:8000"
echo ""
