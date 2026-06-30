"""
WNBA Minutes Projector — persistent runner.

Starts Streamlit and keeps it running. Monitors key Python files for
changes; when a change is detected, clears stale caches and restarts
Streamlit so every code deploy takes effect automatically.

Run this script once (via Task Scheduler or directly) and leave it.
It handles everything: startup, crash recovery, and hot-reload on code changes.
"""

import os
import sys
import time
import glob
import shutil
import subprocess
from pathlib import Path

BASE_DIR   = Path(__file__).parent
APP_FILE   = BASE_DIR / "app.py"
DATA_DIR   = BASE_DIR / "data"
PYC_DIR    = BASE_DIR / "__pycache__"
SENTINEL   = BASE_DIR / ".last_clear_stamp"

PYTHON = sys.executable

# Find streamlit.exe — check known locations in priority order
_candidates = [
    Path(r"C:\Users\kar.patel\AppData\Local\Packages\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\LocalCache\local-packages\Python313\Scripts\streamlit.exe"),
    Path(PYTHON).parent / "streamlit.exe",
    Path(PYTHON).parent.parent / "Scripts" / "streamlit.exe",
]
STREAMLIT = next((str(p) for p in _candidates if p.exists()), "streamlit")

WATCH_FILES = [
    BASE_DIR / "app.py",
    BASE_DIR / "model.py",
    BASE_DIR / "season_stats.py",
    BASE_DIR / "wnba_scraper.py",
    BASE_DIR / "backtest.py",
]

CACHE_PATTERNS = ["season_*.json", "espn_roster_*.json", "schedule_*.json"]


def _get_mtime_stamp() -> str:
    """Combined mtime of all watched files."""
    parts = []
    for f in WATCH_FILES:
        try:
            parts.append(f"{f.name}:{f.stat().st_mtime:.3f}")
        except FileNotFoundError:
            pass
    return "|".join(parts)


def clear_caches():
    """Delete stale data caches and bytecode."""
    for pat in CACHE_PATTERNS:
        for f in DATA_DIR.glob(pat):
            try:
                f.unlink()
            except Exception:
                pass
    if PYC_DIR.exists():
        try:
            shutil.rmtree(PYC_DIR)
        except Exception:
            pass
    print("[runner] Caches cleared.")


def start_streamlit() -> subprocess.Popen:
    """Launch Streamlit and return the process handle."""
    cmd = [
        STREAMLIT, "run", str(APP_FILE),
        "--server.headless", "true",
        "--server.port", "8501",
        "--server.address", "0.0.0.0",   # listen on all network interfaces
        "--server.fileWatcherType", "none",
        "--server.runOnSave", "false",
        "--browser.gatherUsageStats", "false",
    ]
    print(f"[runner] Starting Streamlit...")
    return subprocess.Popen(cmd, cwd=str(BASE_DIR))


def main():
    DATA_DIR.mkdir(exist_ok=True)

    last_stamp = ""
    proc = None

    while True:
        current_stamp = _get_mtime_stamp()
        code_changed  = current_stamp != last_stamp

        # Start or restart if needed
        if proc is None or proc.poll() is not None or code_changed:
            if code_changed and last_stamp:
                print(f"[runner] Code change detected — restarting...")
            elif proc is not None and proc.poll() is not None:
                print(f"[runner] Streamlit crashed (exit {proc.poll()}) — restarting...")

            # Kill existing process
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

            clear_caches()
            last_stamp = _get_mtime_stamp()  # re-read after clear in case app.py mtime updated
            proc = start_streamlit()
            print(f"[runner] Streamlit PID {proc.pid} — http://localhost:8501")

        time.sleep(3)  # poll every 3 seconds


if __name__ == "__main__":
    main()
