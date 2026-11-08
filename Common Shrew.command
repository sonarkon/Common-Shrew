#!/bin/bash
# Common Shrew – double-click launcher for macOS.
# Checks for ffmpeg, Python/Tk and drag & drop support and offers to install what's missing.
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export TK_SILENCE_DEPRECATION=1

ask() {  # ask "Question" -> returns 0 for yes
  local a
  read -r -p "$1 [y/n] " a
  [[ "$a" =~ ^([yYjJ]|yes|Yes|ja|Ja)$ ]]
}
pause_exit() { read -n 1 -s -r -p "Press any key to close …"; exit 1; }

load_brew() {
  for B in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [ -x "$B" ] && eval "$("$B" shellenv)" && return 0
  done
  return 1
}
ensure_brew() {
  command -v brew >/dev/null 2>&1 && return 0
  load_brew && return 0
  echo "Homebrew (package manager) is not installed – it's needed to install this."
  if ask "Install Homebrew now? (asks for your Mac password)"; then
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || return 1
    load_brew
  else
    return 1
  fi
}

# ---------- ffmpeg
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  echo "ffmpeg is not installed."
  if ask "Install ffmpeg now via Homebrew?"; then
    if ensure_brew; then
      brew install ffmpeg || { echo "Installation failed."; pause_exit; }
    else
      echo "Without Homebrew, ffmpeg can't be installed automatically."
      echo "Alternative: https://evermeet.cx/ffmpeg/ (copy ffmpeg + ffprobe to /usr/local/bin)"
      pause_exit
    fi
  else
    echo "The compressor can't work without ffmpeg."
    pause_exit
  fi
fi

# ---------- Python with Tk (Homebrew Python preferred – Apple's system Tk is outdated)
find_brew_py() {
  for PY in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if [ -x "$PY" ] && "$PY" -c "import tkinter" 2>/dev/null; then echo "$PY"; return 0; fi
  done
  return 1
}
find_sys_py() {
  for PY in python3 /usr/bin/python3; do
    if "$PY" -c "import tkinter" 2>/dev/null; then echo "$PY"; return 0; fi
  done
  return 1
}
PY="$(find_brew_py)"
if [ -z "$PY" ] && [ ! -f .use_system_tk ]; then
  echo "For a properly drawn window, Homebrew Python with Tk is recommended"
  echo "(macOS's built-in Tk is outdated and can show blank buttons or windows)."
  if ask "Install python-tk now via Homebrew?" && ensure_brew; then
    brew install python-tk
    PY="$(find_brew_py)"
  elif ask "Don't ask this again?"; then
    touch .use_system_tk
  fi
fi
[ -z "$PY" ] && PY="$(find_sys_py)"
if [ -z "$PY" ]; then
  echo "No Python with Tkinter found. Please run:  brew install python-tk"
  pause_exit
fi

# ---------- Drag & drop (tkinterdnd2 in a local .venv)
VPY=".venv/bin/python"
if [ -x "$VPY" ] && "$VPY" -c "import tkinter, tkinterdnd2" 2>/dev/null; then
  PY="$VPY"
elif [ ! -f .no_dnd ]; then
  echo "Drag & drop needs the small Python package 'tkinterdnd2'."
  if ask "Install it now into a local .venv folder next to this script?"; then
    rm -rf .venv 2>/dev/null
    if "$PY" -m venv .venv && .venv/bin/python -m pip install -q --upgrade pip tkinterdnd2; then
      PY="$VPY"
      echo "Drag & drop enabled."
    else
      echo "Couldn't install tkinterdnd2 – starting without drag & drop."
    fi
  elif ask "Don't ask this again?"; then
    touch .no_dnd
  fi
fi

exec "$PY" common_shrew.py "$@"
