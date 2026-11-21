"""In-memory login failure limiter, keyed by ``f"{email}|{ip}"``."""

from collections import defaultdict
from datetime import datetime, timedelta


class LoginRateLimiter:
    def __init__(self, max_failures: int = 5, window: timedelta = timedelta(minutes=15)) -> None:
        self.max_failures = max_failures
        self.window = window
        self._failures: defaultdict[str, list[datetime]] = defaultdict(list)

    def _prune(self, key: str, now: datetime) -> list[datetime]:
        cutoff = now - self.window
        recent = [t for t in self._failures.get(key, ()) if t > cutoff]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def is_blocked(self, key: str, now: datetime) -> bool:
        return len(self._prune(key, now)) >= self.max_failures

    def record_failure(self, key: str, now: datetime) -> None:
        self._prune(key, now)
        self._failures[key].append(now)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)
