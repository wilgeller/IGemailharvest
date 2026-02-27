#!/usr/bin/env python3
"""
Instagram Contact Email Harvester — entry point.

Usage
-----
python main.py [--config config.yaml] [--input input/profiles.csv]
               [--output output/emails.csv] [--limit N]
"""

import argparse
import csv
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from selenium.common.exceptions import WebDriverException

from src.account_manager import AccountManager, AllAccountsExhaustedError
from src.detector import find_contact_button
from src.driver import AppiumDriver, DriverError
from src.extractor import (
    ExtractionError,
    dismiss_sheet,
    extract_email_clipboard,
    extract_email_from_tree,
    tap_and_wait_for_sheet,
)
from src.human import scroll_profile_naturally, warm_up_session
from src.navigator import LoadFailedError, NavigationError, navigate_to_profile
from src.output_writer import OutputWriter
from src.throttle import ThrottleManager


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Harvest contact emails from Instagram business profiles."
    )
    parser.add_argument(
        "--config", default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "--input", default="input/profiles.csv",
        help="Path to input CSV with 'handle' column (default: input/profiles.csv)",
    )
    parser.add_argument(
        "--output", default=None,
        help="Override output CSV path from config",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Maximum number of profiles to process in this run",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(log_dir: str) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = Path(log_dir) / f"harvest_{stamp}.log"

    fmt = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Console: INFO and above
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(fmt))
    root.addHandler(console)

    # File: DEBUG and above (full detail)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(fmt))
    root.addHandler(fh)

    logging.getLogger(__name__).info("Log file: %s", log_file)


# ---------------------------------------------------------------------------
# Config + Input
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except FileNotFoundError:
        print(f"ERROR: config file not found: {path}", file=sys.stderr)
        sys.exit(1)
    except yaml.YAMLError as exc:
        print(f"ERROR: invalid YAML in {path}: {exc}", file=sys.stderr)
        sys.exit(1)
    if not cfg:
        print(f"ERROR: config file is empty: {path}", file=sys.stderr)
        sys.exit(1)
    return cfg


def load_handles(csv_path: str, limit: Optional[int]) -> list[str]:
    handles = []
    try:
        with open(csv_path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if "handle" not in (reader.fieldnames or []):
                print(
                    f"ERROR: input CSV '{csv_path}' must have a 'handle' column.",
                    file=sys.stderr,
                )
                sys.exit(1)
            for row in reader:
                handle = (row.get("handle") or "").strip()
                if handle:
                    handles.append(handle)
    except FileNotFoundError:
        print(f"ERROR: input file not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    if limit is not None:
        handles = handles[:limit]

    return handles


# ---------------------------------------------------------------------------
# Main harvest loop
# ---------------------------------------------------------------------------

def _run_harvest_loop(
    handles: list[str],
    drv: AppiumDriver,
    writer: OutputWriter,
    account_mgr: AccountManager,
    throttle: ThrottleManager,
    config: dict,
    logger: logging.Logger,
) -> None:
    adb_host = config["device"]["adb_host"]
    adb_port = int(config["device"]["adb_port"])
    total = len(handles)

    for i, handle in enumerate(handles, start=1):
        logger.info("--- [%d/%d] %s ---", i, total, handle)

        # Resume support: skip already-written handles
        if writer.already_processed(handle):
            logger.info("Skipping '%s' (already in output CSV).", handle)
            continue

        profile_url = f"https://instagram.com/{handle}"

        # Determine active account (may raise AllAccountsExhaustedError)
        account_name = account_mgr.current_account_name()

        # ---- Navigation ----
        try:
            navigate_to_profile(drv, drv.driver, handle, config)
        except LoadFailedError as exc:
            status = exc.reason  # "load_failed" or "action_block"
            logger.warning("[%s] %s", status, handle)
            writer.write_result(handle, profile_url, None, status, account_name)
            if exc.reason == "action_block":
                logger.warning("Action block — pausing 30 minutes and rotating account.")
                import time; time.sleep(30 * 60)
            throttle.mark_profile_done()
            continue
        except (NavigationError, WebDriverException) as exc:
            logger.error("Driver error navigating to '%s': %s", handle, exc)
            logger.info("Restarting Instagram and retrying '%s'...", handle)
            drv.restart_app()
            try:
                navigate_to_profile(drv, drv.driver, handle, config)
            except Exception as retry_exc:
                logger.error("Retry also failed for '%s': %s", handle, retry_exc)
                writer.write_result(
                    handle, profile_url, None, "app_restarted", account_name
                )
                throttle.mark_profile_done()
                continue

        throttle.after_load()
        scroll_profile_naturally(drv.driver)

        # ---- Button detection ----
        result = find_contact_button(drv.driver)
        if result.should_skip:
            status = "no_contact_button"
            logger.info("[%s] %s (reason: %s)", status, handle, result.skip_reason)
            writer.write_result(handle, profile_url, None, status, account_name)
            account_mgr.record_profile()
            throttle.mark_profile_done()
            throttle.between_profiles()
            continue

        # ---- Tap contact button ----
        sheet_appeared = tap_and_wait_for_sheet(drv.driver, result.element)
        if not sheet_appeared:
            logger.warning("[popup_failed] %s", handle)
            dismiss_sheet(drv.driver)
            writer.write_result(handle, profile_url, None, "popup_failed", account_name)
            account_mgr.record_profile()
            throttle.mark_profile_done()
            throttle.between_profiles()
            continue

        throttle.after_tap()

        # ---- Email extraction — primary ----
        email = extract_email_from_tree(drv.driver)
        extraction_status = "success" if email else None

        # ---- Email extraction — clipboard fallback ----
        if email is None:
            logger.debug("Tree scan found no email — trying clipboard fallback.")
            try:
                email = extract_email_clipboard(drv.driver, adb_host, adb_port)
                if email:
                    logger.info("[fallback_triggered] %s → %s", handle, email)
                    extraction_status = "success"
                else:
                    extraction_status = "no_email_in_contact"
            except ExtractionError as exc:
                logger.warning("[extraction_failed] %s: %s", handle, exc)
                extraction_status = "extraction_failed"

        if email is None and extraction_status is None:
            extraction_status = "no_email_in_contact"

        throttle.after_extraction()
        dismiss_sheet(drv.driver)

        # ---- Write result ----
        writer.write_result(handle, profile_url, email, extraction_status, account_name)
        account_mgr.record_profile()
        throttle.mark_profile_done()
        throttle.between_profiles()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    config = load_config(args.config)

    if args.output:
        config["output"]["csv_path"] = args.output

    setup_logging(config.get("log_dir", "output/logs"))
    logger = logging.getLogger("main")

    handles = load_handles(args.input, args.limit)
    logger.info("Loaded %d handles from %s", len(handles), args.input)

    if not handles:
        logger.info("No handles to process. Exiting.")
        return

    account_mgr = AccountManager(config)
    throttle = ThrottleManager(config)

    try:
        with OutputWriter(config) as writer, AppiumDriver(config) as drv:
            warm_up_session(drv.driver)
            try:
                _run_harvest_loop(
                    handles, drv, writer, account_mgr, throttle, config, logger
                )
            except KeyboardInterrupt:
                logger.info("KeyboardInterrupt — shutting down gracefully.")
            except AllAccountsExhaustedError:
                logger.warning("All accounts exhausted — run complete for today.")
    except DriverError as exc:
        logger.error("Could not start Appium session: %s", exc)
        logger.error(
            "Ensure Appium server is running (`appium`) and the device is connected."
        )
        sys.exit(1)

    logger.info("Run complete.")


if __name__ == "__main__":
    main()
