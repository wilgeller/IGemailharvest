"""
Daily verify + sequence pipeline. Runs on Pi 3 via cron.

Stages:
  1. Pull rows from the Google Sheet results tab that have an email but no
     mv_result yet.
  2. Bulk-verify them via the MillionVerifier file API (upload -> poll -> download).
  3. Write mv_result back to the sheet per row.
  4. Push "good" emails into the Instantly campaign, stamp sequenced_at.
  5. Post a Slack summary (successes, partial failures, hard failures).

State is the sheet itself: blank mv_result / sequenced_at cells are the work
queue, so a failed run is picked up by the next one. Python 3.9 compatible,
stdlib + requests only.
"""

import csv
import io
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import requests
import yaml

logger = logging.getLogger("pipeline")

MV_BULK_BASE = "https://bulkapi.millionverifier.com/bulkapi/v2"
MV_API_BASE = "https://api.millionverifier.com/api/v3"
INSTANTLY_BASE = "https://api.instantly.ai/api/v2"

MV_POLL_INTERVAL_S = 60
MV_POLL_TIMEOUT_S = 3600


# ---------------------------------------------------------------------------
# Google Sheets (raw REST, reuses the harvester's credentials.json/token.json)
# ---------------------------------------------------------------------------

class SheetsClient:
    def __init__(self, token_path: str, sheet_id: str) -> None:
        self._token_path = token_path
        self._sheet_id = sheet_id
        self._access_token = None  # type: Optional[str]

    def _refresh(self) -> None:
        with open(self._token_path) as f:
            tok = json.load(f)
        resp = requests.post(
            tok.get("token_uri", "https://oauth2.googleapis.com/token"),
            data={
                "client_id": tok["client_id"],
                "client_secret": tok["client_secret"],
                "refresh_token": tok["refresh_token"],
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        resp.raise_for_status()
        self._access_token = resp.json()["access_token"]

    def _request(self, method: str, path: str, **kwargs):
        if self._access_token is None:
            self._refresh()
        url = "https://sheets.googleapis.com/v4/spreadsheets/%s%s" % (self._sheet_id, path)
        for attempt in range(2):
            headers = {"Authorization": "Bearer %s" % self._access_token}
            resp = requests.request(method, url, headers=headers, timeout=60, **kwargs)
            if resp.status_code == 401 and attempt == 0:
                self._refresh()
                continue
            resp.raise_for_status()
            return resp.json()

    def get_values(self, a1_range: str) -> list:
        data = self._request("GET", "/values/%s" % a1_range)
        return data.get("values", [])

    def update_values(self, a1_range: str, values: list) -> None:
        self._request(
            "PUT",
            "/values/%s?valueInputOption=RAW" % a1_range,
            json={"values": values},
        )

    def batch_update_values(self, updates: list) -> None:
        """updates: list of (a1_range, values) tuples."""
        if not updates:
            return
        self._request(
            "POST",
            "/values:batchUpdate",
            json={
                "valueInputOption": "RAW",
                "data": [{"range": r, "values": v} for r, v in updates],
            },
        )


def col_letter(index: int) -> str:
    """0-based column index -> A1 letter(s)."""
    letters = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


# ---------------------------------------------------------------------------
# MillionVerifier
# ---------------------------------------------------------------------------

def mv_credits(api_key: str) -> Optional[int]:
    try:
        resp = requests.get(MV_API_BASE + "/credits", params={"api": api_key}, timeout=30)
        resp.raise_for_status()
        return int(resp.json().get("credits", 0))
    except Exception as exc:
        logger.warning("Could not fetch MV credits: %s", exc)
        return None


def mv_verify_bulk(api_key: str, emails: list) -> dict:
    """Upload emails, poll until finished, return {email: result}.

    Result values from MV: ok, catch_all, unknown, disposable, invalid.
    """
    file_contents = "\n".join(emails).encode("utf-8")
    resp = requests.post(
        MV_BULK_BASE + "/upload",
        params={"key": api_key},
        files={"file_contents": ("emails.txt", file_contents)},
        timeout=120,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("error"):
        raise RuntimeError("MV upload error: %s" % body["error"])
    file_id = body["file_id"]
    logger.info("MV upload accepted, file_id=%s (%d emails)", file_id, len(emails))

    deadline = time.time() + MV_POLL_TIMEOUT_S
    while True:
        resp = requests.get(
            MV_BULK_BASE + "/fileinfo",
            params={"key": api_key, "file_id": file_id},
            timeout=30,
        )
        resp.raise_for_status()
        info = resp.json()
        status = info.get("status", "")
        logger.info("MV file %s: status=%s percent=%s", file_id, status, info.get("percent"))
        if status == "finished":
            break
        if status in ("canceled", "failed"):
            raise RuntimeError("MV verification %s for file %s" % (status, file_id))
        if time.time() > deadline:
            raise RuntimeError("MV verification timed out after %ds" % MV_POLL_TIMEOUT_S)
        time.sleep(MV_POLL_INTERVAL_S)

    resp = requests.get(
        MV_BULK_BASE + "/download",
        params={"key": api_key, "file_id": file_id, "filter": "all"},
        timeout=120,
    )
    resp.raise_for_status()

    results = {}
    reader = csv.DictReader(io.StringIO(resp.text))
    for row in reader:
        # MV download headers vary in case; normalize.
        norm = {k.strip().lower(): (v or "").strip() for k, v in row.items()}
        email = norm.get("email", "")
        result = norm.get("result", "") or norm.get("quality", "")
        if email:
            results[email.lower()] = result.lower()
    if not results:
        raise RuntimeError("MV download returned no parseable rows")
    return results


# ---------------------------------------------------------------------------
# Instantly
# ---------------------------------------------------------------------------

def instantly_push(api_key: str, campaign_id: str, leads: list) -> list:
    """Push leads (dicts with email/handle/profile_url) to a campaign.

    Returns the sublist of leads that were accepted.
    """
    pushed = []
    headers = {"Authorization": "Bearer %s" % api_key}
    for lead in leads:
        payload = {
            "campaign": campaign_id,
            "email": lead["email"],
            "custom_variables": {
                "handle": lead.get("handle", ""),
                "profile_url": lead.get("profile_url", ""),
            },
        }
        try:
            resp = requests.post(
                INSTANTLY_BASE + "/leads", headers=headers, json=payload, timeout=30
            )
            resp.raise_for_status()
            pushed.append(lead)
        except Exception as exc:
            logger.warning("Instantly push failed for %s: %s", lead["email"], exc)
    return pushed


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def slack_notify(webhook_url: str, text: str) -> None:
    if not webhook_url:
        return
    try:
        requests.post(webhook_url, json={"text": text}, timeout=10)
    except Exception as exc:
        logger.warning("Slack notify failed: %s", exc)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def load_state(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(path: str, state: dict) -> None:
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def run(config: dict) -> str:
    sheet_cfg = config["sheet"]
    mv_cfg = config["millionverifier"]
    inst_cfg = config["instantly"]
    slack_url = config.get("slack_webhook_url", "")
    state_path = config.get("state_path", "output/.pipeline_state.json")
    state = load_state(state_path)

    sheets = SheetsClient(sheet_cfg["token_path"], sheet_cfg["sheet_id"])
    tab = sheet_cfg["results_tab"]

    rows = sheets.get_values("%s!A1:ZZ" % tab)
    if not rows:
        raise RuntimeError("Results tab '%s' returned no rows (auth/tab-name problem?)" % tab)

    header = [h.strip() for h in rows[0]]

    def col_idx(name: str) -> int:
        for i, h in enumerate(header):
            if h.lower() == name.lower():
                return i
        return -1

    handle_i = col_idx(sheet_cfg["handle_column"])
    email_i = col_idx(sheet_cfg["email_column"])
    if handle_i < 0 or email_i < 0:
        raise RuntimeError(
            "Could not find columns '%s'/'%s' in header %r"
            % (sheet_cfg["handle_column"], sheet_cfg["email_column"], header)
        )

    # Ensure pipeline columns exist, creating them at the end of the header.
    mv_i = col_idx("mv_result")
    seq_i = col_idx("sequenced_at")
    new_headers = []
    if mv_i < 0:
        mv_i = len(header) + len(new_headers)
        new_headers.append("mv_result")
    if seq_i < 0:
        seq_i = len(header) + len(new_headers)
        new_headers.append("sequenced_at")
    if new_headers:
        start = len(header)
        sheets.update_values(
            "%s!%s1:%s1" % (tab, col_letter(start), col_letter(start + len(new_headers) - 1)),
            [new_headers],
        )
        logger.info("Added header columns: %s", new_headers)

    def cell(row: list, i: int) -> str:
        return row[i].strip() if i < len(row) else ""

    # Collect unverified rows; dedupe by email (first occurrence wins).
    pending = {}  # email -> {row_num, handle, email}
    duplicates = []  # row numbers marked duplicate
    for n, row in enumerate(rows[1:], start=2):
        email = cell(row, email_i).lower()
        if not email or cell(row, mv_i):
            continue
        if email in pending:
            duplicates.append(n)
        else:
            pending[email] = {"row": n, "email": email, "handle": cell(row, handle_i)}

    mv_col = col_letter(mv_i)
    seq_col = col_letter(seq_i)

    if duplicates:
        sheets.batch_update_values(
            [("%s!%s%d" % (tab, mv_col, n), [["duplicate"]]) for n in duplicates]
        )

    if not pending:
        streak = state.get("empty_streak", 0) + 1
        state["empty_streak"] = streak
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        save_state(state_path, state)
        msg = "Pipeline: no new emails to verify."
        if streak >= 2:
            msg += " ⚠️ %d runs in a row with nothing to process — check Sheets auth/columns." % streak
            slack_notify(slack_url, msg)
        return msg
    state["empty_streak"] = 0

    # Credits check before spending.
    credits = mv_credits(mv_cfg["api_key"])
    low_credit_note = ""
    if credits is not None:
        logger.info("MV credits remaining: %d", credits)
        if credits < len(pending):
            raise RuntimeError(
                "MillionVerifier has %d credits but %d emails need verifying — top up."
                % (credits, len(pending))
            )
        if credits - len(pending) < mv_cfg.get("min_credits_warn", 500):
            low_credit_note = "\n⚠️ MillionVerifier credits low: %d left after this run." % (
                credits - len(pending)
            )

    # Verify.
    emails = sorted(pending.keys())
    logger.info("Verifying %d emails via MillionVerifier", len(emails))
    results = mv_verify_bulk(mv_cfg["api_key"], emails)

    updates = []
    good_leads = []
    counts = {}
    for email, info in pending.items():
        result = results.get(email, "unknown")
        counts[result] = counts.get(result, 0) + 1
        updates.append(("%s!%s%d" % (tab, mv_col, info["row"]), [[result]]))
        if result == "ok":
            good_leads.append(
                {
                    "email": email,
                    "handle": info["handle"],
                    "profile_url": "https://instagram.com/%s" % info["handle"],
                    "row": info["row"],
                }
            )
    sheets.batch_update_values(updates)
    logger.info("Verification results: %s", counts)

    # Push good leads to Instantly.
    push_note = ""
    pushed = []
    if good_leads:
        pushed = instantly_push(inst_cfg["api_key"], inst_cfg["campaign_id"], good_leads)
        now = datetime.now(timezone.utc).isoformat()
        sheets.batch_update_values(
            [("%s!%s%d" % (tab, seq_col, l["row"]), [[now]]) for l in pushed]
        )
        if len(pushed) < len(good_leads):
            push_note = "\n⚠️ Instantly rejected %d of %d leads — see pipeline log." % (
                len(good_leads) - len(pushed),
                len(good_leads),
            )

    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state_path, state)

    summary = "Pipeline: verified %d emails — %s. Pushed %d to Instantly.%s%s" % (
        len(pending),
        ", ".join("%d %s" % (v, k) for k, v in sorted(counts.items())),
        len(pushed),
        push_note,
        low_credit_note,
    )
    slack_notify(slack_url, "✅ " + summary if "⚠️" not in summary else "⚠️ " + summary)
    return summary


def main() -> int:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config_pipeline.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    log_dir = config.get("log_dir", "output/logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(
        log_dir, "pipeline_%s.log" % datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path), logging.StreamHandler()],
    )

    try:
        summary = run(config)
        logger.info(summary)
        return 0
    except Exception as exc:
        logger.exception("Pipeline run failed")
        slack_notify(
            config.get("slack_webhook_url", ""),
            "🚨 Pipeline run failed: %s" % exc,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
