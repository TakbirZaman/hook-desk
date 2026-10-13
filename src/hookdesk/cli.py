"""Tiny sender for manual testing: signs a JSON body and POSTs it to a running hookdesk."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

from hookdesk.security import sign


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hookdesk-send", description=__doc__)
    p.add_argument("source", help="webhook source name, e.g. stripe")
    p.add_argument("json_body", help='JSON object, e.g. \'{"hello": "world"}\'')
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--event-id", default=None)
    args = p.parse_args(argv)

    secret = os.environ.get("HOOKDESK_WEBHOOK_SECRET")
    if not secret:
        print("set HOOKDESK_WEBHOOK_SECRET", file=sys.stderr)
        return 2
    try:
        json.loads(args.json_body)
    except json.JSONDecodeError as err:
        print(f"invalid JSON: {err}", file=sys.stderr)
        return 2

    body = args.json_body.encode()
    ts = str(int(time.time()))
    headers = {"X-Timestamp": ts, "X-Signature": sign(secret, ts, body)}
    if args.event_id:
        headers["X-Event-Id"] = args.event_id
    r = httpx.post(f"{args.url}/hooks/{args.source}", content=body, headers=headers, timeout=10)
    print(r.status_code, r.text)
    return 0 if r.is_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
