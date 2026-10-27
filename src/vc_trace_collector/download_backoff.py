"""Bounded pacing policy for resumable download batch orchestration."""

from dataclasses import dataclass


@dataclass
class DownloadBackoff:
    """Return seconds to wait, or None to pause a repeatedly failing batch.

    A known unavailable item is not retried. Unknown failures count toward the
    circuit breaker, so missing diagnostics cannot cause an unbounded cascade.
    Callers persist each outcome and recheck disk/budgets on every retry.
    """

    consecutive_failures: int = 0

    def record(self, error: str | None) -> int | None:
        if error is None:
            self.consecutive_failures = 0
            return 5
        lower = error.casefold()
        if any(marker in lower for marker in (
            "video unavailable", "private video", "video is private",
            "has been removed", "copyright", "members-only",
        )):
            self.consecutive_failures = 0
            return 5
        self.consecutive_failures += 1
        if self.consecutive_failures >= 4:
            return None
        return 60 * 2 ** (self.consecutive_failures - 1)
