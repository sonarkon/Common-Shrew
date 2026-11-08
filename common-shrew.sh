#!/usr/bin/env bash
# Common Shrew – launcher for Linux.
# Checks for ffmpeg, Python/Tk and drag & drop support and offers to install what's missing.
cd "$(dirname "$(readlink -f "$0")")" || exit 1

ask() {  # ask "Question" -> 0 for yes
  local a
  read -r -p "$1 [y/n] " a
  [[ "$a" =~ ^([yYjJ]|yes|Yes|ja|Ja)$ ]]
}
die() { echo "$1"; read -r -p "Press Enter to close …" _; exit 1; }

# package manager: install command + package names for ffmpeg / tkinter / venv
if command -v apt-get >/dev/null; then
  PM="sudo apt-get install -y"; P_FF="ffmpeg"; P_TK="python3-tk"; P_VENV="python3-venv"
elif command -v dnf >/dev/null; then
  PM="sudo dnf install -y"; P_FF="ffmpeg"; P_TK="python3-tkinter"; P_VENV=""
elif command -v pacman >/dev/null; then
  PM="sudo pacman -S --noconfirm"; P_FF="ffmpeg"; P_TK="tk"; P_VENV=""
elif command -v zypper >/dev/null; then
  PM="sudo zypper install -y"; P_FF="ffmpeg"; P_TK="python3-tk"; P_VENV=""
else
  PM=""
fi
install_pkg() {  # install_pkg <what> <package>
  if [ -z "$PM" ] || [ -z "$2" ]; then
    echo "Please install $1 with your package manager."; return 1
  fi
  if ask "Install $1 now? ($PM $2 – asks for your password)"; then
    $PM $2
  else
    return 1
  fi
}

# ---------- ffmpeg
if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  echo "ffmpeg is not installed."
  install_pkg ffmpeg "$P_FF" || die "The compressor can't work without ffmpeg."
  command -v ffmpeg >/dev/null || die "ffmpeg still not found."
  # (Fedora: ffmpeg with x265 lives in RPM Fusion – see README if this fails)
fi

# ---------- Python with Tk
PY="$(command -v python3)"
[ -z "$PY" ] && die "Python 3 is not installed."
if ! "$PY" -c "import tkinter" 2>/dev/null; then
  echo "Python's Tkinter (needed for the window) is missing."
  install_pkg "Tkinter" "$P_TK" || die "Please install Tkinter (e.g. $P_TK)."
fi

# ---------- Drag & drop (tkinterdnd2 in a local .venv)
VPY=".venv/bin/python"
if [ -x "$VPY" ] && "$VPY" -c "import tkinter, tkinterdnd2" 2>/dev/null; then
  PY="$VPY"
elif [ ! -f .no_dnd ]; then
  echo "Drag & drop needs the small Python package 'tkinterdnd2'."
  if ask "Install it now into a local .venv folder next to this script?"; then
    rm -rf .venv
    if ! "$PY" -m venv .venv 2>/dev/null; then
      install_pkg "python3-venv" "$P_VENV" && "$PY" -m venv .venv
    fi
    if [ -x "$VPY" ] && "$VPY" -m pip install -q --upgrade pip tkinterdnd2; then
      PY="$VPY"; echo "Drag & drop enabled."
    else
      echo "Couldn't install tkinterdnd2 – starting without drag & drop."
    fi
  elif ask "Don't ask this again?"; then
    touch .no_dnd
  fi
fi

exec "$PY" common_shrew.py "$@"
