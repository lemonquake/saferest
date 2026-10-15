#!/bin/bash
# SafeRest Discord Bot Launcher (macOS)
# Created by Aljay Leodones for SafeRest

cd "$(dirname "$0")"

echo "=========================================="
echo " Starting SafeRest Discord Bot"
echo " Created by Aljay Leodones"
echo "=========================================="

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "[1/3] Creating Python virtual environment..."
    # The bot needs Python 3.10+; macOS's built-in python3 is 3.9.
    PYTHON=""
    for candidate in python3.13 python3.12 python3.11 python3.10 "$HOME/.local/bin/python3.12"; do
        if command -v "$candidate" >/dev/null 2>&1; then PYTHON="$candidate"; break; fi
    done
    if [ -z "$PYTHON" ]; then
        echo "Error: Python 3.10 or newer is required. Install it from python.org."
        read -p "Press [Enter] to exit..."
        exit 1
    fi
    "$PYTHON" -m venv venv
    if [ $? -ne 0 ]; then
        echo "Error: Failed to create virtual environment. Ensure Python 3.10+ is installed."
        read -p "Press [Enter] to exit..."
        exit 1
    fi
fi

# Activate virtual environment
source venv/bin/activate

# Ensure dependencies are installed.
# The packages are checked locally first: contacting PyPI on every launch is
# what used to leave this step hanging on a slow or blocked connection, and
# nothing needs downloading once the environment is built.
echo "[2/3] Checking dependencies..."
if python3 -c "import importlib.metadata as m, discord, dotenv; m.version('tzdata')" >/dev/null 2>&1; then
    echo "      All packages present - skipping download."
else
    echo "      Some packages are missing - installing from requirements.txt..."
    python3 -m pip install -r requirements.txt --disable-pip-version-check --no-input --timeout 30 --retries 2
    if [ $? -ne 0 ]; then
        echo ""
        echo "Error: Could not install the dependencies."
        echo "Check your internet connection, then run this launcher again."
        read -p "Press [Enter] to exit..."
        exit 1
    fi
fi

# Launch bot
echo "[3/3] Launching bot..."
python3 main.py

# Keep terminal open if bot stops
echo ""
echo "Bot process has stopped."
read -p "Press [Enter] to close this window..."
