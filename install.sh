#!/bin/sh
# Installs autopilot on Mac, Linux, and Android (Termux).
# Usage: curl -LsSf https://github.com/kafle1/autopilot/releases/latest/download/install.sh | sh
set -eu

REPO="kafle1/autopilot"

echo "Setting up autopilot..."

# Termux (Android) uses its own package manager, not uv.
if [ -n "${PREFIX:-}" ] && [ "$PREFIX" != "${PREFIX#*com.termux}" ]; then
    echo "Android (Termux) detected. Android support is experimental."

    if [ -z "${AUTOPILOT_REF:-}" ]; then
        echo "Looking up the latest release..."
        TAG=$(curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/$REPO/releases/latest" 2>/dev/null | sed -n 's|.*/releases/tag/||p')
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
    if (: </dev/tty) 2>/dev/null; then  # -r passes even when there is no terminal to open
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

# the release page redirect, not the api, which allows only 60 calls an hour per address
if [ -z "${AUTOPILOT_REF:-}" ]; then
    echo "Looking up the latest release..."
    TAG=$(curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/$REPO/releases/latest" 2>/dev/null | sed -n 's|.*/releases/tag/||p')
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
if (: </dev/tty) 2>/dev/null; then  # -r passes even when there is no terminal to open
    "$HOME/.local/bin/autopilot" setup </dev/tty
else
    echo "Now run: autopilot setup"
fi
