"""What B4 ran on, recorded next to every number it produces (SPEC §0, §9):
CPU, cores, memory, OS, power, and the versions and thread count of the
software doing the embedding."""

import os
import platform
import subprocess
import sys
from importlib import metadata
from typing import Any


def _cpu_name() -> str:
    if sys.platform == "win32":
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
        )
        name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
        return str(name).strip()
    if sys.platform == "linux":
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    return platform.processor() or "unknown"


def _power() -> str:
    if sys.platform != "win32":
        return "unknown"
    try:
        out = subprocess.run(
            ["powercfg", "/getactivescheme"], capture_output=True, text=True, timeout=10
        ).stdout
    except OSError:
        return "unknown"
    return out.split("(")[-1].rstrip(") \r\n") if "(" in out else out.strip()


# Windows 11's power mode (Settings > Power) is an overlay on the power plan:
# the plan can read "Balanced" while the mode is Best performance.
_POWER_MODES = {
    "00000000-0000-0000-0000-000000000000": "Balanced",
    "961cc777-2547-4f9d-8174-7d86181b8a7a": "Best power efficiency",
    "3af9b8d9-7c97-431d-ad78-34a8bfea439f": "Better performance",
    "ded574b5-45a0-4f42-8737-46345c09c238": "Best performance",
}


def _power_mode(on_battery: bool | None) -> str:
    if sys.platform != "win32" or on_battery is None:
        return "unknown"
    import winreg

    name = "ActiveOverlayDcPowerScheme" if on_battery else "ActiveOverlayAcPowerScheme"
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes"
        )
        guid, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return "unknown"
    return _POWER_MODES.get(str(guid).lower(), str(guid))


def _version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "not installed"


def describe(torch_threads: int | None = None) -> dict[str, Any]:
    import psutil

    battery = psutil.sensors_battery()
    frequency = psutil.cpu_freq()
    on_battery = None if battery is None else not battery.power_plugged
    return {
        "cpu": _cpu_name(),
        "cores_physical": psutil.cpu_count(logical=False),
        "cores_logical": os.cpu_count(),
        "cpu_max_mhz": round(frequency.max) if frequency else None,
        "memory_bytes": psutil.virtual_memory().total,
        "os": platform.platform(),
        "power_plan": _power(),
        "power_mode": _power_mode(on_battery),
        "on_battery": on_battery,
        "python": platform.python_version(),
        "torch": _version("torch"),
        "sentence_transformers": _version("sentence-transformers"),
        "torch_threads": torch_threads,
    }


def as_text(info: dict[str, Any]) -> str:
    return "\n".join(f"{key}: {value}" for key, value in info.items())


def clocks() -> tuple[float, float, float] | None:
    """(the fastest core's clock, the lowest clock limit of any core, the
    maximum clock) in MHz, or None where this can't be read. Windows only:
    psutil reports processor 0's clock alone, which falls to 1,200 MHz
    whenever that core idles while a timed thread runs on another. The limit
    is what power or heat throttling lowers."""
    if sys.platform != "win32":
        return None
    import ctypes

    class ProcessorPowerInformation(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulong)
            for name in ("Number", "MaxMhz", "CurrentMhz", "MhzLimit", "MaxIdleState",
                         "CurrentIdleState")
        ]  # fmt: skip

    processors = (ProcessorPowerInformation * (os.cpu_count() or 1))()
    processor_information = 11  # POWER_INFORMATION_LEVEL
    status = ctypes.windll.powrprof.CallNtPowerInformation(  # type: ignore[attr-defined]
        processor_information, None, 0, processors, ctypes.sizeof(processors)
    )
    if status != 0:
        return None
    return (
        max(p.CurrentMhz for p in processors),
        min(p.MhzLimit for p in processors),
        max(p.MaxMhz for p in processors),
    )


class Watch:
    """Watches the machine through a timed run: how busy all its cores were,
    its clocks (machine.clocks), and whether it ran on battery. A run timed
    on one thread keeps one logical core busy (1/8 of this laptop); much more
    than that means something else was using the machine. A clock limit under
    the maximum, or even the fastest core running well under it, means the
    CPU was throttled. Either flags the run, to be noted in the results and
    run once more."""

    BUSY_LIMIT = 25.0  # percent of all logical cores
    CLOCK_FLOOR = 0.85  # of the maximum clock

    def __enter__(self) -> "Watch":
        import psutil

        psutil.cpu_percent(interval=None)  # starts the measurement
        self._clocks: list[tuple[float, float, float]] = []
        self.sample()
        return self

    def sample(self) -> None:
        reading = clocks()
        if reading is not None:
            self._clocks.append(reading)

    def __exit__(self, *exc: object) -> None:
        import psutil

        self.busy = psutil.cpu_percent(interval=None)  # since __enter__
        self.sample()
        battery = psutil.sensors_battery()
        self.on_battery = None if battery is None else not battery.power_plugged

    def result(self) -> dict[str, Any]:
        flags = []
        if self.busy > self.BUSY_LIMIT:
            flags.append(f"system busy {self.busy:.0f} percent, over {self.BUSY_LIMIT:.0f}")
        fastest = limit = maximum = None
        if self._clocks:
            fastest = min(c[0] for c in self._clocks)  # the slowest moment of the fastest core
            limit = min(c[1] for c in self._clocks)
            maximum = max(c[2] for c in self._clocks)
            if limit < self.CLOCK_FLOOR * maximum:
                flags.append(f"clock limited to {limit:.0f} of {maximum:.0f} MHz")
            if fastest < self.CLOCK_FLOOR * maximum:
                flags.append(f"fastest core fell to {fastest:.0f} of {maximum:.0f} MHz")
        if self.on_battery:
            flags.append("on battery")
        return {
            "system_busy_percent": round(self.busy, 1),
            "cpu_mhz_min": fastest,
            "cpu_mhz_limit_min": limit,
            "cpu_mhz_max": maximum,
            "samples": len(self._clocks),
            "on_battery": self.on_battery,
            "flags": flags,
        }
