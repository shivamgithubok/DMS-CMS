#!/usr/bin/env bash
# Create the Python environment and build the cabin screen.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required." >&2
  exit 1
fi

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

if command -v gcc >/dev/null 2>&1 && [[ -d third_party/lvgl ]]; then
  make -C ui
else
  echo "Skipping the cabin screen build. Install gcc and libx11-dev, and keep third_party/lvgl in place."
fi

echo
echo "Setup finished."
echo "Next: source .venv/bin/activate"
echo "Then: python pipeline/run_lvgl_demo.py"
