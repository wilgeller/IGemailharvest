"""
Instagram profile navigation.

Opens profile pages via deep link and verifies they loaded correctly.
Uses a dual-XPath strategy to tolerate Instagram's frequent A/B UI changes.
"""

import logging
import subprocess
import time

from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger(__name__)

# XPaths tried in order — first match wins.
# Instagram A/B tests layouts so multiple fallbacks are needed.
_PROFILE_LOADED_XPATHS = [
    # Action bar title (username)
    '//android.widget.TextView[@resource-id="com.instagram.android:id/action_bar_title"]',
    # Follower/following count row
    '//*[@resource-id="com.instagram.android:id/row_profile_header_textview_post_count"]',
    # Profile picture (present on all profile types including private)
    '//*[@resource-id="com.instagram.android:id/row_profile_header_imageview_profile_picture"]',
]

# Text fragments that indicate an action block
_ACTION_BLOCK_TEXTS = [
    "Try Again Later",
    "We restrict certain activity",
    "Action Blocked",
]


class LoadFailedError(Exception):
    """
    Raised when a profile cannot be loaded after all retries.

    Parameters
    ----------
    handle : str
    reason : str   e.g. "load_failed" or "action_block"
    """

    def __init__(self, handle: str, reason: str) -> None:
        super().__init__(f"Failed to load profile '{handle}': {reason}")
        self.handle = handle
        self.reason = reason


class NavigationError(Exception):
    """Raised for unexpected Appium errors during navigation (driver crash, etc.)."""


def navigate_to_profile(
    driver_obj,        # AppiumDriver — used only for restart_app()
    raw_driver,        # webdriver.Remote
    handle: str,
    config: dict,
) -> None:
    """
    Navigate to the Instagram profile for `handle` and wait for it to render.

    Strategy
    --------
    Attempt 1: open deeplink, wait for profile header.
    Attempt 2: call driver_obj.restart_app(), then retry deeplink.
    If both attempts fail: raise LoadFailedError.
    After each successful page load: check for action block overlay.

    Parameters
    ----------
    driver_obj : AppiumDriver
    raw_driver : webdriver.Remote
    handle : str
        Instagram username (no @ prefix).
    config : dict
        Full config. Reads config['delays']['profile_load_timeout'] and
        config['delays']['profile_load_poll'].

    Raises
    ------
    LoadFailedError
    NavigationError
    """
    timeout = float(config["delays"]["profile_load_timeout"])
    poll = float(config["delays"]["profile_load_poll"])
    adb_host = config["device"]["adb_host"]
    adb_port = int(config["device"]["adb_port"])

    max_attempts = 3  # initial + 2 retries

    for attempt in range(max_attempts):
        if attempt > 0:
            logger.info(
                "Retrying profile '%s' (attempt %d/%d)...", handle, attempt + 1, max_attempts
            )
            driver_obj.restart_app()
            time.sleep(2)

        try:
            _open_deeplink(raw_driver, handle, adb_host, adb_port)
        except NavigationError:
            if attempt == max_attempts - 1:
                raise
            continue

        if _wait_for_profile_header(raw_driver, timeout, poll):
            # Profile loaded — check for action block before returning
            if _check_action_block(raw_driver):
                logger.warning("Action block detected for handle '%s'.", handle)
                raise LoadFailedError(handle, "action_block")
            logger.debug("Profile '%s' loaded successfully.", handle)
            return

        logger.warning("Timeout waiting for profile '%s' header (attempt %d).", handle, attempt + 1)

    raise LoadFailedError(handle, "load_failed")


def _open_deeplink(raw_driver, handle: str, adb_host: str, adb_port: int) -> None:
    """
    Open the Instagram deep link for `handle`.

    Tries driver.get() first; falls back to ADB am start if that raises.
    """
    url = f"instagram://user?username={handle}"
    try:
        raw_driver.get(url)
        return
    except WebDriverException as exc:
        logger.debug("driver.get() failed (%s), trying ADB fallback.", exc)

    # ADB fallback
    try:
        result = subprocess.run(
            [
                "adb", "-s", f"{adb_host}:{adb_port}",
                "shell", "am", "start",
                "-a", "android.intent.action.VIEW",
                "-d", url,
                "com.instagram.android",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            raise NavigationError(
                f"ADB am start failed for '{handle}': {result.stderr.strip()}"
            )
    except subprocess.TimeoutExpired as exc:
        raise NavigationError(f"ADB am start timed out for '{handle}'") from exc


def _wait_for_profile_header(raw_driver, timeout: float, poll: float) -> bool:
    """
    Poll for any of the known profile-loaded XPaths.

    Returns True if an element is found within `timeout` seconds, False otherwise.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for xpath in _PROFILE_LOADED_XPATHS:
            try:
                elements = raw_driver.find_elements(AppiumBy.XPATH, xpath)
                if elements:
                    return True
            except WebDriverException:
                pass
        time.sleep(poll)
    return False


def _check_action_block(raw_driver) -> bool:
    """
    Scan visible text nodes for action-block indicator strings.

    Returns True if an action block is present.
    """
    try:
        elements = raw_driver.find_elements(AppiumBy.XPATH, "//*[@text]")
        for el in elements:
            try:
                text = el.get_attribute("text") or ""
                for fragment in _ACTION_BLOCK_TEXTS:
                    if fragment.lower() in text.lower():
                        logger.debug("Action block text found: %r", text)
                        return True
            except WebDriverException:
                continue
    except WebDriverException:
        pass
    return False
