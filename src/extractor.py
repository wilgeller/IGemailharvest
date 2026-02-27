"""
Email extraction from Instagram contact bottom sheets.

Primary method: scan the accessibility tree for text nodes containing an
email address pattern.

Fallback method: long-press the email element, copy to clipboard via UI,
then read the clipboard with ADB shell.
"""

import logging
import re
import subprocess
import time
from typing import Optional

from appium.webdriver.common.appiumby import AppiumBy
from appium.webdriver.extensions.clipboard import ClipboardContentType
from selenium.common.exceptions import WebDriverException

logger = logging.getLogger(__name__)

EMAIL_REGEX = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    re.IGNORECASE,
)

# Seconds to wait for the bottom sheet to appear after tapping
_SHEET_WAIT = 4.0
_SHEET_POLL = 0.3

# Resource ID fragments that indicate the bottom sheet is present
_BOTTOM_SHEET_IDS = [
    "bottom_sheet",
    "design_bottom_sheet",
    "contact_options",
]

# Text fragments that indicate a permission dialog (dismiss before reading sheet)
_PERMISSION_DIALOG_TEXTS = ["Allow", "Deny", "ALLOW", "DENY"]
_PERMISSION_DENY_IDS = [
    "com.android.packageinstaller:id/permission_deny_button",
    "com.android.permissioncontroller:id/permission_deny_button",
]


class ExtractionError(Exception):
    """Raised when the ADB subprocess call fails unexpectedly."""


def tap_and_wait_for_sheet(raw_driver, element) -> bool:
    """
    Tap `element` and wait for the contact bottom sheet to appear.

    Before tapping, dismisses any pending permission dialogs that would
    prevent the sheet from rendering (e.g., "Allow Instagram to make calls?").

    Returns True if the sheet appeared, False if it timed out.

    Parameters
    ----------
    raw_driver : webdriver.Remote
    element    : WebElement — the contact/email button to tap
    """
    _dismiss_permission_dialog(raw_driver)

    try:
        element.click()
    except WebDriverException as exc:
        logger.warning("Failed to tap contact button: %s", exc)
        return False

    # Poll for sheet presence
    deadline = time.monotonic() + _SHEET_WAIT
    while time.monotonic() < deadline:
        if _is_sheet_present(raw_driver):
            logger.debug("Bottom sheet detected.")
            return True
        time.sleep(_SHEET_POLL)

    logger.debug("Timed out waiting for bottom sheet.")
    return False


def extract_email_from_tree(raw_driver) -> Optional[str]:
    """
    Scan all text-bearing nodes in the current accessibility tree for
    a valid email address.

    Returns the first match found, or None.

    Parameters
    ----------
    raw_driver : webdriver.Remote
    """
    try:
        elements = raw_driver.find_elements(AppiumBy.XPATH, "//*[@text]")
    except WebDriverException as exc:
        logger.debug("find_elements failed during tree scan: %s", exc)
        return None

    for el in elements:
        try:
            text = el.get_attribute("text") or ""
            match = EMAIL_REGEX.search(text)
            if match:
                email = match.group(0)
                logger.debug("Email found in tree: %s", email)
                return email
        except WebDriverException:
            continue

    return None


def extract_email_clipboard(
    raw_driver,
    adb_host: str,
    adb_port: int,
) -> Optional[str]:
    """
    Clipboard fallback: long-press the email text element, copy it, and
    read the clipboard via ADB.

    Steps
    -----
    1. Re-scan tree to find the element containing an email.
    2. Long-press via UIAutomator2 gesture.
    3. Tap "Copy" from the context menu.
    4. Read clipboard: try Appium get_clipboard() first (Android 13+ safe),
       fall back to ADB service call Parcel decode for older devices.
    5. Apply EMAIL_REGEX to the clipboard text.

    Returns the email string or None if clipboard read fails.

    Parameters
    ----------
    raw_driver : webdriver.Remote
    adb_host   : str
    adb_port   : int

    Raises
    ------
    ExtractionError — only on hard subprocess failures (not on empty clipboard)
    """
    email_element = _find_email_element(raw_driver)
    if email_element is None:
        logger.debug("No email element found for clipboard fallback.")
        return None

    # Long-press to trigger selection handles
    try:
        raw_driver.execute_script(
            "mobile: longClickGesture",
            {"elementId": email_element.id, "duration": 1500},
        )
    except WebDriverException as exc:
        logger.debug("Long-press failed: %s", exc)
        return None

    time.sleep(1.5)

    # Tap "Copy" from the context menu
    _tap_copy_menu(raw_driver)

    time.sleep(0.5)

    # Read clipboard — try Appium's get_clipboard() first (works on Android 13+),
    # then fall back to the ADB Parcel decode for older devices / edge cases.
    clipboard_text = _read_clipboard_appium(raw_driver)
    if clipboard_text is None:
        logger.debug("Appium clipboard read returned empty — trying ADB Parcel fallback.")
        clipboard_text = _read_clipboard_adb(adb_host, adb_port)

    if clipboard_text is None:
        return None

    match = EMAIL_REGEX.search(clipboard_text)
    if match:
        email = match.group(0)
        logger.debug("Email retrieved from clipboard: %s", email)
        return email

    return None


def dismiss_sheet(raw_driver) -> None:
    """Dismiss the contact bottom sheet by pressing the Android back button."""
    try:
        raw_driver.back()
    except WebDriverException as exc:
        logger.debug("Back press failed (sheet may already be dismissed): %s", exc)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _is_sheet_present(raw_driver) -> bool:
    """
    Return True if a bottom sheet or email address is visible.

    Checks both resource-ID fragments and the presence of an email regex match.
    """
    try:
        # Check for known bottom-sheet resource IDs
        for id_fragment in _BOTTOM_SHEET_IDS:
            xpath = f'//*[contains(@resource-id, "{id_fragment}")]'
            if raw_driver.find_elements(AppiumBy.XPATH, xpath):
                return True

        # Check for an email address in any visible text node
        elements = raw_driver.find_elements(AppiumBy.XPATH, "//*[@text]")
        for el in elements:
            try:
                text = el.get_attribute("text") or ""
                if EMAIL_REGEX.search(text):
                    return True
            except WebDriverException:
                continue
    except WebDriverException:
        pass
    return False


def _dismiss_permission_dialog(raw_driver) -> None:
    """
    Dismiss system permission dialogs that may overlay the bottom sheet.
    """
    for deny_id in _PERMISSION_DENY_IDS:
        try:
            buttons = raw_driver.find_elements(AppiumBy.ID, deny_id)
            if buttons:
                buttons[0].click()
                logger.debug("Dismissed permission dialog via %s", deny_id)
                time.sleep(0.5)
                return
        except WebDriverException:
            continue


def _find_email_element(raw_driver):
    """
    Return the first WebElement whose text contains an email address, or None.
    """
    try:
        elements = raw_driver.find_elements(AppiumBy.XPATH, "//*[@text]")
        for el in elements:
            try:
                text = el.get_attribute("text") or ""
                if EMAIL_REGEX.search(text):
                    return el
            except WebDriverException:
                continue
    except WebDriverException:
        pass
    return None


def _tap_copy_menu(raw_driver) -> None:
    """Tap the 'Copy' option from the text selection context menu."""
    copy_xpaths = [
        '//*[@text="Copy"]',
        '//*[contains(@content-desc, "Copy")]',
    ]
    for xpath in copy_xpaths:
        try:
            elements = raw_driver.find_elements(AppiumBy.XPATH, xpath)
            if elements:
                elements[0].click()
                logger.debug("Tapped 'Copy' from context menu.")
                return
        except WebDriverException:
            continue
    logger.debug("Could not find 'Copy' in context menu.")


def _read_clipboard_appium(raw_driver) -> Optional[str]:
    """
    Read clipboard via Appium's UIAutomator2 get_clipboard().

    This runs inside the UIAutomator2 server process and is not subject to
    the shell-user clipboard restrictions introduced in Android 13 (API 33).
    Returns the clipboard string, or None if empty or on any error.
    """
    try:
        text = raw_driver.get_clipboard(ClipboardContentType.PLAINTEXT)
        return text if text else None
    except Exception as exc:
        logger.debug("Appium get_clipboard() failed: %s", exc)
        return None


def _read_clipboard_adb(adb_host: str, adb_port: int) -> Optional[str]:
    """
    Read the clipboard content via ADB service call.

    Returns the decoded string content or None on failure.

    The `service call clipboard 2` command returns a Parcel-encoded response:
        Result: Parcel(00000000 0000000N HHHHHHHH HHHHHHHH ...)
    Decode steps:
        1. Extract hex groups between parentheses.
        2. Join into bytes: bytes.fromhex("".join(groups))
        3. Bytes 0-3: status (skip)
        4. Bytes 4-7: string length in UTF-16 code units (skip)
        5. Bytes 8+: UTF-16LE encoded string content
        6. Decode as utf-16-le, strip null chars.
    """
    try:
        result = _run_adb(
            adb_host, adb_port,
            "shell", "service call clipboard 2 s16 com.android.shell",
        )
    except ExtractionError as exc:
        logger.debug("ADB clipboard service call failed: %s", exc)
        return None

    # Parse the Parcel hex output
    # Example: "Result: Parcel(00000000 00000013 00680065 ...)\n"
    parcel_match = re.search(r"Parcel\((.*?)\)", result, re.DOTALL)
    if not parcel_match:
        logger.debug("Could not parse Parcel from ADB output: %r", result)
        return None

    hex_groups = parcel_match.group(1).split()
    if len(hex_groups) < 3:
        # Too short to contain a string
        return None

    try:
        raw_bytes = bytes.fromhex("".join(hex_groups))
        # Skip the 8-byte Parcel header (status + length fields)
        payload = raw_bytes[8:]
        text = payload.decode("utf-16-le", errors="ignore").rstrip("\x00")
        return text if text else None
    except (ValueError, UnicodeDecodeError) as exc:
        logger.debug("Parcel decode failed: %s", exc)
        return None


def _run_adb(adb_host: str, adb_port: int, *args: str) -> str:
    """
    Run an ADB command against the connected device.

    Returns stdout as a string.
    Raises ExtractionError on non-zero return code or subprocess error.
    """
    cmd = ["adb", "-s", f"{adb_host}:{adb_port}"] + list(args)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            raise ExtractionError(
                f"ADB command failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        return result.stdout
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError("ADB command timed out") from exc
    except FileNotFoundError as exc:
        raise ExtractionError("adb not found in PATH") from exc
