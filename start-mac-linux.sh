#!/usr/bin/env bash
# ===================================================================
#  AP Intelligence - one-click start for macOS and Linux
#  Run:  bash start-mac-linux.sh     (or:  chmod +x start-mac-linux.sh && ./start-mac-linux.sh)
#  Installs what is missing, then opens the workbench at http://localhost:8000
# ===================================================================
set -uo pipefail
cd "$(dirname "$0")"

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[91m'; GREEN=$'\033[92m'; YELLOW=$'\033[93m'; OFF=$'\033[0m'

echo
echo "  =========================================================="
echo "    ${BOLD}AP Intelligence — Invoice Matching & Fraud Detection${OFF}"
echo "  =========================================================="
echo

# ---- find Python --------------------------------------------------
PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      PY="$candidate"; break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo "  ${RED}[X]${OFF} Python 3.10 or newer was not found."
  echo
  echo "      macOS :  brew install python"
  echo "      Ubuntu:  sudo apt-get install -y python3 python3-pip"
  echo "      Other :  https://www.python.org/downloads/"
  echo
  exit 1
fi
echo "  Python $("$PY" --version 2>&1 | awk '{print $2}') found  ${DIM}($(command -v "$PY"))${OFF}"

# ---- Tesseract: needed for OCR, not a Python package --------------
if command -v tesseract >/dev/null 2>&1; then
  echo "  Tesseract OCR $("tesseract" --version 2>&1 | head -1 | awk '{print $2}') found"
else
  echo
  echo "  ${YELLOW}[!]${OFF} Tesseract OCR is not installed. The app will start, but it cannot"
  echo "      read invoices without it."
  echo
  echo "      macOS :  brew install tesseract"
  echo "      Ubuntu:  sudo apt-get install -y tesseract-ocr"
  echo
  if [ -t 0 ]; then
    read -r -p "      Continue anyway? [y/N] " reply
    case "$reply" in [yY]*) ;; *) exit 1 ;; esac
  fi
fi

# ---- Python packages ---------------------------------------------
echo
echo "  Checking Python packages (first run takes a minute)..."
if ! "$PY" -m pip install --quiet --disable-pip-version-check -r requirements.txt 2>/tmp/ap_pip.log; then
  if grep -q "externally-managed-environment" /tmp/ap_pip.log 2>/dev/null; then
    echo
    echo "  ${YELLOW}This Python is externally managed (recent Debian/Ubuntu or Homebrew).${OFF}"
    echo "  Creating a virtual environment instead..."
    "$PY" -m venv .venv && . .venv/bin/activate && PY=python
    "$PY" -m pip install --quiet --disable-pip-version-check -r requirements.txt || {
      echo "  ${RED}[X]${OFF} Package install failed. See /tmp/ap_pip.log"; exit 1; }
  else
    echo "  ${RED}[X]${OFF} Package install failed:"
    tail -5 /tmp/ap_pip.log
    echo "  Try: $PY -m pip install -r requirements.txt"
    exit 1
  fi
fi
echo "  Packages ready."

# ---- launch ------------------------------------------------------
echo
"$PY" run.py "$@"
