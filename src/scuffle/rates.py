"""Time-weighted transfer rates, independent of callback frequency."""
from collections import deque
import time


class RateSmoother:
    def __init__(self, *, window: float = 8, warmup: float = 2, clock=time.monotonic):
        self.window, self.warmup, self.clock = window, warmup, clock
        self.samples = deque()

    def update(self, completed: int) -> float:
        now = self.clock()
        if not self.samples or completed < self.samples[-1][1]:
            self.samples.clear()
            self.samples.append((now, completed))
            return 0.0
        if now == self.samples[-1][0]:
            self.samples[-1] = now, completed
            return 0.0 if len(self.samples) == 1 else self._rate(now, completed)
        self.samples.append((now, completed))
        while len(self.samples) > 2 and self.samples[1][0] <= now - self.window:
            self.samples.popleft()
        return self._rate(now, completed)

    def _rate(self, now: float, completed: int) -> float:
        start, amount = self.samples[0]
        elapsed = now - start
        if elapsed < self.warmup:
            return 0.0
        if elapsed > self.window:
            end, next_amount = self.samples[1]
            amount += (next_amount - amount) * (now - self.window - start) / (end - start)
            elapsed = self.window
        return max(0.0, (completed - amount) / elapsed)
