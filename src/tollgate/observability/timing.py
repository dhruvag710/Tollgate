"""Per-request stage timings, for seeing where the gateway's time goes.

RequestIDMiddleware starts a Timings for every request; code times its stages
with `stage()`. With SERVER_TIMING set, responses report them in a
Server-Timing header (durations in milliseconds), alongside:

- `other`: time inside the gateway not covered by any stage (framework,
  middleware, building and starting the response)
- `gateway`: everything except waiting on the provider (stages named
  `upstream-*`), the gateway's own overhead
- `total`: from arrival until the header is sent
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager

UPSTREAM_PREFIX = "upstream-"


class Timings:
    def __init__(self, started_at: float) -> None:
        self.started_at = started_at
        self._stages: dict[str, float] = {}

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.add(name, time.perf_counter() - start)

    def add(self, name: str, seconds: float) -> None:
        self._stages[name] = self._stages.get(name, 0.0) + seconds

    def server_timing(self) -> str:
        total = time.perf_counter() - self.started_at
        upstream = sum(s for n, s in self._stages.items() if n.startswith(UPSTREAM_PREFIX))
        metrics = {
            **self._stages,
            "other": total - sum(self._stages.values()),
            "gateway": total - upstream,
            "total": total,
        }
        return ", ".join(f"{name};dur={seconds * 1000:.3f}" for name, seconds in metrics.items())
