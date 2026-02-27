"""
Randomized delay and idle-pause management.

Delays use a Gaussian distribution centred between min and max, clamped
to the range. This produces timing that clusters naturally around a mean
rather than being flat across the range — harder to detect statistically
than uniform random.
"""

import logging
import random
import time

logger = logging.getLogger(__name__)


def delay(min_s: float, max_s: float) -> None:
    """
    Sleep for a Gaussian-distributed duration clamped to [min_s, max_s].

    Mean = midpoint of the range. Sigma = 1/4 of the range width so that
    ~95% of draws fall within bounds before clamping.
    """
    mid = (min_s + max_s) / 2
    sigma = (max_s - min_s) / 4
    duration = random.gauss(mid, sigma)
    duration = max(min_s, min(max_s, duration))
    logger.debug("Sleeping %.2fs", duration)
    time.sleep(duration)


def micro_pause() -> None:
    """Very short random hesitation (0.3–1.2s) to break up mechanical sequences."""
    time.sleep(random.uniform(0.3, 1.2))


class ThrottleManager:
    """
    Tracks profiles processed and inserts human-like pauses.

    Parameters
    ----------
    config : dict
        Full parsed config.yaml. Reads config['delays'].
    """

    def __init__(self, config: dict) -> None:
        d = config["delays"]
        self._between = d["between_profiles"]
        self._after_load = d["after_load"]
        self._after_tap = d["after_tap"]
        self._after_extraction = d["after_extraction"]
        self._idle = d["idle_pause"]
        self._idle_freq = d["idle_frequency"]

        self._count = 0
        self._next_idle = random.randint(self._idle_freq[0], self._idle_freq[1])

    def between_profiles(self) -> None:
        """Randomized pause between profile navigations."""
        delay(self._between[0], self._between[1])

    def after_load(self) -> None:
        """Randomized pause after profile page loads before scanning."""
        delay(self._after_load[0], self._after_load[1])

    def after_tap(self) -> None:
        """Randomized pause after tapping a button before reading UI."""
        delay(self._after_tap[0], self._after_tap[1])

    def after_extraction(self) -> None:
        """Randomized pause after email extraction before dismissal."""
        delay(self._after_extraction[0], self._after_extraction[1])

    def maybe_idle_pause(self) -> None:
        """Insert a long idle pause if the profile count threshold is reached."""
        if self._count >= self._next_idle:
            dur = random.uniform(self._idle[0], self._idle[1])
            logger.info("Idle pause for %.1fs after %d profiles", dur, self._count)
            time.sleep(dur)
            self._count = 0
            self._next_idle = random.randint(self._idle_freq[0], self._idle_freq[1])

    def mark_profile_done(self) -> None:
        """
        Increment the profile counter and trigger an idle pause if due.
        Call once at the end of each profile loop iteration.
        """
        self._count += 1
        self.maybe_idle_pause()
