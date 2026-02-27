"""
Appium session manager.

Owns the lifecycle of the WebDriver connection to the Android device.
All other modules receive the raw `driver` attribute and never
construct sessions themselves.
"""

import logging
import time

from appium import webdriver
from appium.options.android.uiautomator2.base import UiAutomator2Options
from selenium.common.exceptions import WebDriverException

logger = logging.getLogger(__name__)

INSTAGRAM_PACKAGE = "com.instagram.android"
INSTAGRAM_ACTIVITY = ".activity.MainTabActivity"


class DriverError(Exception):
    """Raised when the Appium session cannot be established."""


class AppiumDriver:
    """
    Wraps a UIAutomator2 Appium session for a single Android device
    connected via ADB over WiFi.

    Usage
    -----
    with AppiumDriver(config) as drv:
        drv.driver.find_element(...)

    Parameters
    ----------
    config : dict
        Full parsed config.yaml. Reads:
            config['device']['adb_host']
            config['device']['adb_port']
            config['appium']['server_url']
            config['appium']['implicit_wait']
    """

    def __init__(self, config: dict) -> None:
        self._adb_host: str = config["device"]["adb_host"]
        self._adb_port: int = config["device"]["adb_port"]
        self._server_url: str = config["appium"]["server_url"]
        self._implicit_wait: int = config["appium"]["implicit_wait"]
        self.driver: webdriver.Remote | None = None

    def connect(self) -> None:
        """
        Establish the Appium session.

        Sets implicitly_wait to 0 immediately after connection so that
        all element searches use explicit waits only.

        Raises
        ------
        DriverError
            If the Appium server is unreachable or the session cannot start.
        """
        device_name = f"{self._adb_host}:{self._adb_port}"
        logger.info("Connecting to device %s via Appium at %s", device_name, self._server_url)

        options = UiAutomator2Options()
        options.platform_name = "Android"
        options.automation_name = "uiautomator2"
        options.app_package = INSTAGRAM_PACKAGE
        options.app_activity = INSTAGRAM_ACTIVITY
        options.no_reset = True
        options.auto_grant_permissions = True
        options.new_command_timeout = 300
        options.device_name = device_name

        try:
            self.driver = webdriver.Remote(self._server_url, options=options)
            self.driver.implicitly_wait(0)
            logger.info("Appium session established (session ID: %s)", self.driver.session_id)
        except WebDriverException as exc:
            raise DriverError(f"Failed to start Appium session: {exc}") from exc

    def restart_app(self) -> None:
        """
        Terminate and relaunch Instagram without creating a new Appium session.

        Used after crash detection or action-block recovery.
        """
        if self.driver is None:
            logger.warning("restart_app called but driver is None")
            return

        logger.info("Restarting Instagram app...")
        try:
            self.driver.terminate_app(INSTAGRAM_PACKAGE)
            time.sleep(3)
            self.driver.activate_app(INSTAGRAM_PACKAGE)
            time.sleep(4)
            logger.info("Instagram restarted.")
        except WebDriverException as exc:
            logger.warning("Error restarting app: %s", exc)

    def quit(self) -> None:
        """Safely quit the Appium session."""
        if self.driver is not None:
            try:
                self.driver.quit()
                logger.info("Appium session closed.")
            except WebDriverException as exc:
                logger.debug("WebDriverException on quit (ignored): %s", exc)
            finally:
                self.driver = None

    def __enter__(self) -> "AppiumDriver":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.quit()
        return False
