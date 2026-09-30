#!/usr/bin/env python3
"""
Reopen Freshdesk tickets whose "Follow up date" custom field equals today's
date (Asia/Kolkata timezone).

Runs entirely outside Freshworks and outside Claude — designed to be fired
by GitHub Actions on a fixed cron schedule, with no AI safety classifier and
no dependency on any developer machine being online.

Rules:
  - Skip tickets already Open (status 2) — no duplicate note, not counted.
  - Skip tickets that are Closed (status 5) — never reopen a closed ticket.
  - Every other status gets set to Open, with a private note explaining why.

Exit code is non-zero if any ticket that should have been updated failed to
update, so a failure is visible as a red X on the GitHub Actions run (and,
if you enable notifications, an email/Slack alert) instead of silently
looking like success.
"""

import os
import sys
import json
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta

FRESHDESK_DOMAIN = os.environ.get("FRESHDESK_DOMAIN", "b360.freshdesk.com")
API_KEY = os.environ["FRESHDESK_API_KEY"]  # required — set as a GitHub Actions secret
FOLLOW_UP_FIELD = "cf_follow_up_date"

STATUS_OPEN = 2
STATUS_CLOSED = 5

IST = timezone(timedelta(hours=5, minutes=30))


def today_ist():
    return datetime.now(IST).strftime("%Y-%m-%d")


def _request(method, path, body=None, params=None):
    url = f"https://{FRESHDESK_DOMAIN}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")

    import base64
    auth = base64.b64encode(f"{API_KEY}:X".encode("utf-8")).decode("ascii")
    req.add_header("Authorization", f"Basic {auth}")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.getcode()
            payload = resp.read().decode("utf-8")
            return status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8")
        return e.code, (json.loads(payload) if payload else {"error": payload})


def find_due_tickets(today):
    tickets = []
    page = 1
    while page <= 10:  # Freshdesk search API hard cap: 300 results (30/page)
        query = f"\"{FOLLOW_UP_FIELD}:'{today}'\""
        status, result = _request(
            "GET", "/api/v2/search/tickets", params={"query": query, "page": page}
        )
        if status != 200:
            print(f"ERROR: search page {page} failed ({status}): {result}", file=sys.stderr)
            break
        page_results = result.get("results", [])
        tickets.extend(page_results)
        if len(page_results) < 30:
            break
        page += 1
        time.sleep(0.5)  # be gentle with Freshdesk's rate limits
    return tickets


def reopen_ticket(ticket_id, today):
    status, body = _request(
        "PUT", f"/api/v2/tickets/{ticket_id}", body={"status": STATUS_OPEN}
    )
    if status not in (200, 201) or body.get("status") != STATUS_OPEN:
        return False, f"status update failed ({status}): {body}"

    note_status, note_body = _request(
        "POST",
        f"/api/v2/tickets/{ticket_id}/notes",
        body={
            "body": f"Auto-reopened by scheduled check: Follow up date ({today}) reached.",
            "private": True,
        },
    )
    if note_status != 201:
        return False, f"note failed ({note_status}): {note_body}"

    return True, None


def main():
    today = today_ist()
    print(f"Checking follow-up date == {today} (Asia/Kolkata) on {FRESHDESK_DOMAIN}")

    tickets = find_due_tickets(today)
    print(f"Found {len(tickets)} ticket(s) due today.")

    reopened, skipped_open, skipped_closed, failed = [], [], [], []

    for t in tickets:
        tid = t["id"]
        status = t.get("status")
        if status == STATUS_OPEN:
            skipped_open.append(tid)
            continue
        if status == STATUS_CLOSED:
            skipped_closed.append(tid)
            continue

        ok, err = reopen_ticket(tid, today)
        if ok:
            reopened.append(tid)
            print(f"  reopened #{tid}")
        else:
            failed.append((tid, err))
            print(f"  FAILED #{tid}: {err}", file=sys.stderr)

    print("\n--- Summary ---")
    print(f"Checked: {today}")
    print(f"Due today: {len(tickets)}")
    print(f"Reopened: {len(reopened)} {reopened}")
    print(f"Already open (skipped): {len(skipped_open)} {skipped_open}")
    print(f"Closed (skipped): {len(skipped_closed)} {skipped_closed}")
    print(f"Failed: {len(failed)} {failed}")

    if failed:
        sys.exit(1)  # non-zero exit => GitHub Actions marks the run as failed


if __name__ == "__main__":
    main()
