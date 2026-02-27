"""
Human-like behaviour layer.

Adds scrolling and browsing activity that makes the session look like a
real user rather than a bot firing deeplinks back-to-back.
"""

import logging
import random
import time

from selenium.common.exceptions import WebDriverException

logger = logging.getLogger(__name__)


def scroll_profile_naturally(raw_driver) -> None:
    """
    Simulate a human reading a profile before looking for the contact button.

    Does 1–3 small downward scrolls with pauses, then occasionally scrolls
    back up slightly, as if skimming the bio and recent posts.
    """
    try:
        size = raw_driver.get_window_size()
        w, h = size["width"], size["height"]
    except WebDriverException:
        return

    num_scrolls = random.randint(1, 3)
    for _ in range(num_scrolls):
        try:
            raw_driver.execute_script(
                "mobile: scrollGesture",
                {
                    "left": w // 4,
                    "top": h // 3,
                    "width": w // 2,
                    "height": h // 3,
                    "direction": "down",
                    "percent": random.uniform(0.25, 0.55),
                },
            )
        except WebDriverException:
            break
        time.sleep(random.uniform(0.6, 1.8))

    # 40% chance to scroll back up slightly (like re-reading something)
    if random.random() < 0.40:
        try:
            raw_driver.execute_script(
                "mobile: scrollGesture",
                {
                    "left": w // 4,
                    "top": h // 3,
                    "width": w // 2,
                    "height": h // 3,
                    "direction": "up",
                    "percent": random.uniform(0.15, 0.35),
                },
            )
        except WebDriverException:
            pass
        time.sleep(random.uniform(0.4, 1.0))


def warm_up_session(raw_driver) -> None:
    """
    Browse the Instagram home feed for a few seconds before starting the
    harvest loop. Establishes a normal-looking session history rather than
    jumping straight into profile deeplinks.
    """
    logger.info("Warming up session — browsing home feed...")
    try:
        raw_driver.get("instagram://")
        time.sleep(random.uniform(4, 8))

        size = raw_driver.get_window_size()
        w, h = size["width"], size["height"]

        num_scrolls = random.randint(2, 4)
        for _ in range(num_scrolls):
            raw_driver.execute_script(
                "mobile: scrollGesture",
                {
                    "left": w // 4,
                    "top": h // 4,
                    "width": w // 2,
                    "height": h // 4,
                    "direction": "down",
                    "percent": random.uniform(0.4, 0.8),
                },
            )
            time.sleep(random.uniform(1.5, 4.0))

        logger.info("Warm-up complete.")
    except WebDriverException as exc:
        logger.debug("Warm-up skipped (WebDriverException): %s", exc)
