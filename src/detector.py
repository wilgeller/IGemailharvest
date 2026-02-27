"""
Contact button detection on Instagram profile pages.

Uses case-insensitive XPath matching via the translate() function so the
logic is resilient to Instagram's mixed-case button labels ("Contact",
"CONTACT", "contact options", etc.) and does not rely on brittle resource IDs.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import WebDriverException

logger = logging.getLogger(__name__)

# Buttons we want to tap (checked in priority order)
TAPPABLE_LABELS = ["contact options", "contact", "email"]

# Buttons that indicate the profile has no email-accessible surface
SKIP_LABELS = ["call", "message", "follow", "following"]

# Full uppercase alphabet for XPath translate()
_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_LOWER = "abcdefghijklmnopqrstuvwxyz"


@dataclass
class DetectionResult:
    """
    Value object returned by find_contact_button().

    Attributes
    ----------
    element      : WebElement or None
    label_type   : str or None   "contact" | "contact options" | "email"
    should_skip  : bool          True if no email surface was found
    skip_reason  : str or None   e.g. "no_contact_button", "only_call_button"
    """

    element: object = field(default=None, repr=False)
    label_type: Optional[str] = None
    should_skip: bool = False
    skip_reason: Optional[str] = None


def find_contact_button(raw_driver) -> DetectionResult:
    """
    Scan the current screen for a tappable contact-related button.

    Search order
    ------------
    1. Try each label in TAPPABLE_LABELS (priority order).
       Return the first match as DetectionResult(element=..., label_type=...).
    2. If none found, check for SKIP_LABELS.
       If present → DetectionResult(should_skip=True, skip_reason='only_{label}_button').
    3. If nothing found at all →
       DetectionResult(should_skip=True, skip_reason='no_contact_button').

    Parameters
    ----------
    raw_driver : webdriver.Remote

    Returns
    -------
    DetectionResult
    """
    # --- Pass 1: look for tappable labels ---
    # Instagram renders action buttons as a clickable parent container with a
    # non-clickable child TextView for the label. We try two strategies:
    #   1a. Clickable container that contains a matching text child (parent-first)
    #   1b. Text node that is itself clickable (direct match)
    # Whichever finds something first wins.
    for label in TAPPABLE_LABELS:
        # Strategy 1a: clickable parent whose descendant text matches
        xpath_parent = (
            f'//*[@clickable="true" and '
            f'.//*[contains(translate(@text,"{_UPPER}","{_LOWER}"),"{label}")]]'
        )
        try:
            elements = raw_driver.find_elements(AppiumBy.XPATH, xpath_parent)
            if elements:
                logger.debug("Found tappable button (parent match): %r", label)
                return DetectionResult(element=elements[0], label_type=label)
        except WebDriverException as exc:
            logger.debug("Parent XPath query failed for label %r: %s", label, exc)

        # Strategy 1b: text node is itself clickable (fallback)
        xpath_direct = _xpath_contains_lower("@text", label)
        try:
            elements = raw_driver.find_elements(AppiumBy.XPATH, xpath_direct)
            for el in elements:
                try:
                    clickable = el.get_attribute("clickable")
                    if clickable and clickable.lower() == "true":
                        logger.debug("Found tappable button (direct match): %r", label)
                        return DetectionResult(element=el, label_type=label)
                except WebDriverException:
                    continue
        except WebDriverException as exc:
            logger.debug("Direct XPath query failed for label %r: %s", label, exc)
            continue

    # --- Pass 2: check for skip-only labels ---
    for label in SKIP_LABELS:
        xpath = _xpath_contains_lower("@text", label)
        try:
            elements = raw_driver.find_elements(AppiumBy.XPATH, xpath)
            if elements:
                logger.debug("Skip label found: %r — no email surface.", label)
                return DetectionResult(
                    should_skip=True,
                    skip_reason=f"only_{label.replace(' ', '_')}_button",
                )
        except WebDriverException:
            continue

    # --- Pass 3: nothing found ---
    logger.debug("No contact or skip buttons found on this profile.")
    return DetectionResult(should_skip=True, skip_reason="no_contact_button")


def _xpath_contains_lower(attr: str, value: str) -> str:
    """
    Build a case-insensitive XPath contains() expression using translate().

    Example
    -------
    _xpath_contains_lower("@text", "contact")
    → '//*[contains(translate(@text,"ABC...Z","abc...z"),"contact")]'
    """
    return (
        f'//*[contains('
        f'translate({attr},"{_UPPER}","{_LOWER}"),'
        f'"{value}"'
        f')]'
    )
