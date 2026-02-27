"""
CSV output writer with deduplication and optional webhook delivery.

The CSV file is opened in append mode at initialization and flushed after
every row to ensure no data loss on unexpected termination.
"""

import csv
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

CSV_HEADERS = [
    "handle",
    "profile_url",
    "email",
    "status",
    "account_used",
    "timestamp",
]


class OutputWriter:
    """
    Append-mode CSV writer with resume support and optional webhook.

    Parameters
    ----------
    config : dict
        Full parsed config.yaml. Reads config['output'].
    """

    def __init__(self, config: dict) -> None:
        self._csv_path: str = config["output"]["csv_path"]
        self._webhook_url: str = config["output"].get("webhook_url", "")
        self._processed: set[str] = set()
        self._file = None
        self._writer = None

    def initialize(self) -> None:
        """
        Prepare the CSV file for writing.

        - Creates parent directories if they don't exist.
        - If the file doesn't exist, creates it and writes the header row.
        - If the file exists, reads all existing handles into the dedup set.
        - Opens the file in append mode for subsequent writes.
        """
        path = Path(self._csv_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        if not path.exists():
            with path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
                writer.writeheader()
            logger.info("Created output CSV: %s", self._csv_path)
        else:
            # Load existing handles for deduplication / resume support
            with path.open("r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    handle = row.get("handle", "").strip()
                    if handle:
                        self._processed.add(handle)
            logger.info(
                "Loaded %d already-processed handles from %s",
                len(self._processed),
                self._csv_path,
            )

        self._file = path.open("a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=CSV_HEADERS)

    def already_processed(self, handle: str) -> bool:
        """Return True if this handle was found in an existing output CSV row."""
        return handle in self._processed

    def write_result(
        self,
        handle: str,
        profile_url: str,
        email: Optional[str],
        status: str,
        account_used: str,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """
        Write one result row and flush immediately.

        If status is 'success' and a webhook URL is configured, fires the
        webhook (errors are logged, not raised).
        """
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)

        row = {
            "handle": handle,
            "profile_url": profile_url,
            "email": email or "",
            "status": status,
            "account_used": account_used,
            "timestamp": timestamp.isoformat(),
        }
        self._writer.writerow(row)
        self._file.flush()
        self._processed.add(handle)

        logger.info("[%s] handle=%s email=%s", status, handle, email or "—")

        if status == "success" and self._webhook_url:
            self.webhook_post(self._webhook_url, handle, email or "", timestamp)

    def webhook_post(
        self,
        url: str,
        handle: str,
        email: str,
        timestamp: datetime,
    ) -> None:
        """POST a JSON payload to the configured webhook URL."""
        payload = {
            "handle": handle,
            "email": email,
            "profile_url": f"https://instagram.com/{handle}",
            "timestamp": timestamp.isoformat(),
        }
        try:
            resp = requests.post(url, json=payload, timeout=5)
            resp.raise_for_status()
            logger.debug("Webhook delivered for %s (HTTP %d)", handle, resp.status_code)
        except Exception as exc:
            logger.warning("Webhook delivery failed for %s: %s", handle, exc)

    def close(self) -> None:
        """Flush and close the CSV file handle."""
        if self._file and not self._file.closed:
            self._file.flush()
            self._file.close()

    def __enter__(self) -> "OutputWriter":
        self.initialize()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False
