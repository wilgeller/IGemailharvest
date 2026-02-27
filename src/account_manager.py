"""
Multi-account rotation with daily caps and persistent state.

State is stored in a JSON sidecar file. Writes are atomic (tempfile + os.replace)
so the state cannot be corrupted by an unexpected process termination.
"""

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import date, timezone, datetime
from typing import Optional

logger = logging.getLogger(__name__)


class AllAccountsExhaustedError(Exception):
    """Raised when every configured account has hit its daily cap."""


@dataclass
class Account:
    username: str
    daily_cap: int


class AccountManager:
    """
    Manages multi-account rotation with per-account daily caps.

    Parameters
    ----------
    config : dict
        Full parsed config.yaml. Reads config['accounts'] and
        config['account_state_path'].
    """

    def __init__(self, config: dict) -> None:
        self._accounts: list[Account] = [
            Account(username=a["username"], daily_cap=a["daily_cap"])
            for a in config["accounts"]
        ]
        self._state_path: str = config.get("account_state_path", ".account_state.json")
        self._state: dict = {}
        self._current_index: int = 0

        self._load_state()

        # Ensure every account has a state entry
        for acct in self._accounts:
            if acct.username not in self._state:
                self._state[acct.username] = {
                    "count_today": 0,
                    "last_reset_date": str(date.today()),
                }
        self._save_state()

        # Reset counts for any account that hasn't been touched today
        for acct in self._accounts:
            self._maybe_reset_day(acct.username)

        # Advance past any already-exhausted accounts
        try:
            self.get_active_account()
        except AllAccountsExhaustedError:
            pass

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        """Load the JSON state file, creating an empty state if absent."""
        if os.path.exists(self._state_path):
            try:
                with open(self._state_path, "r", encoding="utf-8") as f:
                    self._state = json.load(f)
                logger.debug("Loaded account state from %s", self._state_path)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "Could not read account state (%s), starting fresh.", exc
                )
                self._state = {}
        else:
            self._state = {}

    def _save_state(self) -> None:
        """Atomically write the state dict to disk."""
        dir_ = os.path.dirname(self._state_path) or "."
        fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2)
            os.replace(tmp_path, self._state_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # Day-reset logic
    # ------------------------------------------------------------------

    def _maybe_reset_day(self, username: str) -> None:
        """Zero out count_today if last_reset_date is before today (local)."""
        today = str(date.today())
        entry = self._state.get(username, {})
        if entry.get("last_reset_date") != today:
            logger.info(
                "Resetting daily count for %s (was %d on %s)",
                username,
                entry.get("count_today", 0),
                entry.get("last_reset_date", "unknown"),
            )
            self._state[username] = {"count_today": 0, "last_reset_date": today}
            self._save_state()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_active_account(self) -> Account:
        """
        Return the current active account.

        Advances the internal index past exhausted accounts.
        Raises AllAccountsExhaustedError if none are available.
        """
        start = self._current_index
        total = len(self._accounts)

        for _ in range(total):
            idx = self._current_index % total
            acct = self._accounts[idx]

            # Check for midnight rollover before evaluating exhaustion
            self._maybe_reset_day(acct.username)

            count = self._state[acct.username]["count_today"]
            if count < acct.daily_cap:
                return acct

            # This account is exhausted — try the next one
            logger.info(
                "Account %s exhausted (%d/%d), rotating.",
                acct.username,
                count,
                acct.daily_cap,
            )
            self._current_index = (self._current_index + 1) % total

            # If we've looped back to the start, all are exhausted
            if self._current_index == start:
                break

        raise AllAccountsExhaustedError(
            "All configured accounts have hit their daily caps."
        )

    def record_profile(self) -> Account:
        """
        Increment the daily count for the active account and save state.

        Returns the account that was credited (before any rotation).
        After incrementing, checks whether the account is now exhausted
        and advances the index if so.
        """
        acct = self.get_active_account()
        self._state[acct.username]["count_today"] += 1
        self._save_state()
        logger.debug(
            "Recorded profile for %s (%d/%d)",
            acct.username,
            self._state[acct.username]["count_today"],
            acct.daily_cap,
        )

        # Pre-advance index if now exhausted so next call is fast
        if self._state[acct.username]["count_today"] >= acct.daily_cap:
            logger.info(
                "Account %s reached daily cap (%d).", acct.username, acct.daily_cap
            )
            self._current_index = (self._current_index + 1) % len(self._accounts)

        return acct

    def current_account_name(self) -> str:
        """Return the active account username without side effects."""
        return self.get_active_account().username
