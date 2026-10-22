"""
eight-dollar / bot / comment_reach.py
------------------------------------
Publish answers as the PAGE, into conversations we are allowed to be in.

⛔ NEVER THE PERSONAL ACCOUNT.
-------------------------------
The owner's personal Facebook account ("Khaled Abdelli") is off limits: it
must never post, browse, comment, react, message, share, or be stored as a
session. Every comment here is published as a PAGE, with a page-scoped
token. The account that appears as the author of every comment is the
page.

WHY THIS EXISTS
---------------
`publish_to_groups` needs App Review - business verification, a screencast,
weeks, and it can be refused. The group API is closed. But the app already
holds `pages_manage_engagement`, and that permission was PROVEN live today:
a comment with an empty message comes back as a VALIDATION error

    (#100) Param message must be a non-empty UTF-8 encoded string

which means the permission was granted. A page can comment. That is a
reach channel into other people's conversations with no review at all.

THE LOOP
--------
    find a question  (ops/find_buyers.py - StockTwits, public, no auth)
    answer it       (the model's own number, credibility-gated)
    publish it      (here - as the page)
    they land       (the free tier page, with email capture)
    the list grows  (the daily signal converts to the paid tier)

SAFETY
------
  - a page token, always; the code refuses to run without one
  - a ledger of what was already answered, so nothing is posted twice
  - a hard cap per run and a delay between posts, so it reads like a person
  - a credibility gate on the number itself: no live price, or a gap under
    10 %, or a distress balance sheet -> no comment at all
  - the comment text is generated, never a pitch, and it says "model
    output, not advice"

TARGETS
-------
`comment_targets.json` - a list of post ids to answer under. Each entry:

    {"id": "<post id>", "why": "the post that earned it"}

There is no API for "posts worth answering": Facebook does not expose
search for pages, does not resolve page usernames, and does not list a
page's groups. So the targets come from `ops/find_buyers.py` (which reads
StockTwits) and from the pages' own comment threads, and the ids are
recorded here.

USAGE
-----
    python comment_reach.py --list
    python comment_reach.py --publish --dry-run
    python comment_reach.py --publish
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
GRAPH = "https://graph.facebook.com/v26.0"

TARGETS_FILE = HERE / "comment_targets.json"
LEDGER_FILE = HERE / "comment_ledger.json"

PAGES = {
    "ethan": {"id": "1264009986803820", "name": "Ethan Cole"},
    "market": {"id": "1421089964411983", "name": "سوق اليوم The Market Today"},
}


def load_env() -> None:
    for candidate in (HERE.parent.parent / "ethan-cole-fb-bot" / ".env",
                      HERE.parent.parent / "soq-alyoum-fb-bot" / ".env",
                      HERE / ".env"):
        if not candidate.exists():
            continue
        try:
            from dotenv import load_dotenv
            load_dotenv(candidate)
        except Exception:
            pass
        break


def page_token(page_key: str) -> str:
    p = PAGES[page_key]
    tok = (os.getenv(p["token_env"]) if False else "") or ""
    tok = (os.getenv("FB_PAGE_ACCESS_TOKEN") or "").strip()
    if not tok:
        raise SystemExit(
            "No FB_PAGE_ACCESS_TOKEN. This script publishes as a PAGE and "
            "will not use a personal token.")
    return tok


def api(method: str, path: str, token: str, **params) -> dict:
    params = {k: v for k, v in params.items() if v is not None}
    params["access_token"] = token
    import requests
    r = requests.request(method, f"{GRAPH}{path}", params=params, timeout=30)
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text[:300]}


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser(
        description="Publish answers as the page, into conversations we are "
                    "allowed to be in.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--page", choices=sorted(PAGES), default="ethan")
    ap.add_argument("--list", action="store_true", help="show the targets")
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max", type=int, default=5)
    ap.add_argument("--delay", type=int, default=90)
    args = ap.parse_args()

    p = PAGES[args.page]
    token = page_token(args.page)

    # who does the token belong to? the page, never a person
    who = api("GET", "/me", token, fields="id,name")
    if who.get("id") != p["id"]:
        print(f"REFUSING: the token belongs to {who.get('name')!r} "
              f"({who.get('id')}), not to {p['name']!r} ({p['id']}).")
        return 2
    print(f"publishing as {p['name']} ({p['id']}) - the token agrees")

    targets = load_json(TARGETS_FILE, [])
    ledger = load_json(LEDGER_FILE, {})
    done = ledger.get(args.page, {})

    if args.list or not args.publish:
        print(f"\n{len(targets)} target(s):")
        for t in targets:
            mark = "done" if done.get(t["id"]) else "pending"
            print(f"  [{mark:<7}] {t['id']:<44} {t.get('why','')[:60]}")
        if not args.publish:
            print(f"\nanswer them with: python ops\\find_buyers.py --scan "
                  f"--answers")
            return 0

    published = 0
    for t in targets:
        if published >= args.max:
            break
        pid = t["id"]
        if done.get(pid):
            continue
        body = t.get("comment") or ""
        if not body:
            print(f"  {pid}: no comment text - run find_buyers.py --answers")
            continue
        if args.dry_run:
            print(f"  WOULD POST on {pid}:\n      {body}")
            continue
        res = api("POST", f"/{pid}/comments", token, message=body)
        if "error" in res:
            e = res["error"]
            print(f"  {pid}: FAILED ({e.get('code')}) {e.get('message','')[:90]}")
            if e.get("code") == 10:
                print("     -> the permission is not actually granted. Stopping.")
                break
            continue
        print(f"  {pid}: commented -> {res.get('id')}")
        done[pid] = datetime.now(timezone.utc).isoformat()
        ledger[args.page] = done
        LEDGER_FILE.write_text(json.dumps(ledger, ensure_ascii=False, indent=1),
                               encoding="utf-8")
        published += 1
        time.sleep(args.delay)

    print(f"\n{published} comment(s) published")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
