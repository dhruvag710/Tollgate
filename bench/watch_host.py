"""Watches the host while a Docker benchmark runs (B3, B5): one JSON line every
INTERVAL seconds with how busy all its cores were, every core's clock and
clock limit (bench/cache_eval/machine.py), whether it ran on battery, and on
Windows how much memory it could still commit (Docker's VM counts against it,
page cache and all: run out, and new threads fail), until STOP_FILE appears.
bench/run_v2.sh starts it around each k6 run, and bench/v2_report.py flags a
run whose clocks were limited, that ran on battery, or that came close to the
commit limit.

    python bench/watch_host.py OUT.jsonl STOP_FILE [INTERVAL]
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "cache_eval"))

from machine import clocks


def commit_mb() -> tuple[float, float] | None:
    """(committed, the commit limit) in MB, on Windows."""
    if sys.platform != "win32":
        return None
    import ctypes

    size_t, ulong = ctypes.c_size_t, ctypes.c_ulong

    class PerformanceInformation(ctypes.Structure):
        _fields_ = [
            ("cb", ulong), ("CommitTotal", size_t), ("CommitLimit", size_t),
            ("CommitPeak", size_t), ("PhysicalTotal", size_t), ("PhysicalAvailable", size_t),
            ("SystemCache", size_t), ("KernelTotal", size_t), ("KernelPaged", size_t),
            ("KernelNonpaged", size_t), ("PageSize", size_t), ("HandleCount", ulong),
            ("ProcessCount", ulong), ("ThreadCount", ulong),
        ]  # fmt: skip

    info = PerformanceInformation()
    info.cb = ctypes.sizeof(info)
    psapi = ctypes.windll.psapi  # type: ignore[attr-defined]
    if not psapi.GetPerformanceInfo(ctypes.byref(info), info.cb):
        return None
    page = info.PageSize / 2**20
    return info.CommitTotal * page, info.CommitLimit * page


def main() -> None:
    import psutil

    if sys.argv[1:] == ["--commit"]:  # just the commit charge, once
        commit = commit_mb()
        print("n/a" if commit is None else f"committed {commit[0]:,.0f} of {commit[1]:,.0f} MB")
        return
    out, stop = Path(sys.argv[1]), Path(sys.argv[2])
    interval = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0
    psutil.cpu_percent(interval=None)
    with out.open("w", encoding="utf-8") as f:
        while not stop.exists():
            time.sleep(interval)
            reading = clocks()
            battery = psutil.sensors_battery()
            commit = commit_mb()
            sample = {
                "t": time.time(),
                "busy": psutil.cpu_percent(interval=None),
                "mhz_fastest": reading[0] if reading else None,
                "mhz_limit": reading[1] if reading else None,
                "mhz_max": reading[2] if reading else None,
                "on_battery": None if battery is None else not battery.power_plugged,
                "commit_mb": None if commit is None else round(commit[0]),
                "commit_limit_mb": None if commit is None else round(commit[1]),
            }
            f.write(json.dumps(sample) + "\n")
            f.flush()


if __name__ == "__main__":
    main()
