#!/bin/sh
# Installs autopilot on Mac, Linux, and Android (Termux).
# Usage: curl -LsSf https://github.com/kafle1/autopilot/releases/latest/download/install.sh | sh
set -eu

REPO="kafle1/autopilot"

echo "Setting up autopilot..."

# Termux (Android) uses its own package manager, not uv.
if [ "${PREFIX:-}" != "${PREFIX#*com.termux}" ]; then
    echo "Android (Termux) detected. Android support is experimental."

    if [ -z "${AUTOPILOT_REF:-}" ]; then
        echo "Looking up the latest release..."
        TAG=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n1)
        if [ -z "$TAG" ]; then
            echo "Could not reach GitHub to find the latest release. Check your internet connection and try again." >&2
            exit 1
        fi
    else
        TAG="$AUTOPILOT_REF"
    fi
    echo "Installing autopilot $TAG..."

    pkg install -y python
    pip install --upgrade "https://github.com/$REPO/archive/refs/tags/$TAG.tar.gz"

    echo ""
    if [ -r /dev/tty ]; then
        autopilot setup </dev/tty
    else
        echo "Now run: autopilot setup"
    fi
    exit 0
fi

# Mac and Linux: install uv if it is not already here.
if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
    echo "Installing uv (the tool that runs autopilot)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

if [ -x "$HOME/.local/bin/uv" ]; then
    UV="$HOME/.local/bin/uv"
else
    UV="uv"
fi

if [ -z "${AUTOPILOT_REF:-}" ]; then
    echo "Looking up the latest release..."
    TAG=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n1)
    if [ -z "$TAG" ]; then
        echo "Could not reach GitHub to find the latest release. Check your internet connection and try again." >&2
        exit 1
    fi
else
    TAG="$AUTOPILOT_REF"
fi
echo "Installing autopilot $TAG..."

"$UV" tool install --force --managed-python --python 3.12 "https://github.com/$REPO/archive/refs/tags/$TAG.tar.gz"

echo ""
echo "autopilot is installed."
echo ""
if [ -r /dev/tty ]; then
    "$HOME/.local/bin/autopilot" setup </dev/tty
else
    echo "Now run: autopilot setup"
fi
