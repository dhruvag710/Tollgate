"""Per-request stage timings, for seeing where the gateway's time goes.

RequestIDMiddleware starts a Timings for every request; code times its stages
with `stage()`. With SERVER_TIMING set, responses report them in a
Server-Timing header (durations in milliseconds), alongside:

- `other`: time inside the gateway not covered by any stage (framework,
  middleware, building and starting the response)
- `gateway`: everything except waiting on the provider (stages named
  `upstream-*`), the gateway's own overhead
- `total`: from arrival until the header is sent

and any notes code made with `note()` (as `name;desc=...`), such as how the
semantic cache's lookup ended. Notes are fixed labels, never request content.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager

UPSTREAM_PREFIX = "upstream-"


class Timings:
    def __init__(self, started_at: float) -> None:
        self.started_at = started_at
        self._stages: dict[str, float] = {}
        self._notes: dict[str, str] = {}

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.add(name, time.perf_counter() - start)

    def add(self, name: str, seconds: float) -> None:
        self._stages[name] = self._stages.get(name, 0.0) + seconds

    def note(self, name: str, label: str) -> None:
        """Report a label (a token: letters, digits, dashes) with the timings."""
        self._notes[name] = label

    def seconds(self, name: str) -> float:
        """A stage's time so far (0 if it hasn't run)."""
        return self._stages.get(name, 0.0)

    def upstream_seconds(self) -> float:
        """Time spent waiting on the provider so far."""
        return sum(s for n, s in self._stages.items() if n.startswith(UPSTREAM_PREFIX))

    def server_timing(self) -> str:
        total = time.perf_counter() - self.started_at
        upstream = self.upstream_seconds()
        metrics = {
            **self._stages,
            "other": total - sum(self._stages.values()),
            "gateway": total - upstream,
            "total": total,
        }
        entries = [f"{name};dur={seconds * 1000:.3f}" for name, seconds in metrics.items()]
        entries += [f"{name};desc={label}" for name, label in self._notes.items()]
        return ", ".join(entries)
