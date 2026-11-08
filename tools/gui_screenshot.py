#!/usr/bin/env python3
"""Headless GUI smoke test: opens the app with some files, takes a screenshot, quits.

Linux:  xvfb-run -a -s "-screen 0 1220x930x24" python3 tools/gui_screenshot.py out.png samples/*
macOS:  python3 tools/gui_screenshot.py out.png samples/*      (uses `screencapture`)
Needs a Python with Tkinter (+ optionally tkinterdnd2) and ImageMagick `import` on Linux.
"""
import os
import subprocess
import sys
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import common_shrew as cs  # noqa: E402

out = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "screenshot.png")
sys.argv = ["common_shrew.py"] + sys.argv[2:]
cs.webbrowser.open = lambda url: print("would open", url)
_orig = tk.Misc.mainloop


def shot():
    if sys.platform == "darwin":
        subprocess.run(["screencapture", "-x", out])
    else:
        subprocess.run(["import", "-window", "root", out])
    print("screenshot:", out)


def mainloop(self, n=0):
    self.after(5000, shot)          # time for probing + thumbnails
    self.after(5600, self.destroy)
    _orig(self, n)


tk.Misc.mainloop = mainloop
cs.run_gui()
