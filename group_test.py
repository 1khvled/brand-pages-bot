"""
eight-dollar / bot / group_test.py
----------------------------------
One-shot test: does the app post into a group as the PAGE, with no App
Review?

WHY THIS EXISTS
---------------
`POST /{page-id}/sharedposts` is blocked:

    (#10) Application does not have the capability to make this API call.

That endpoint needs `publish_to_groups`, which means App Review - business
verification, a screencast, weeks, and it can be refused. The other route
is the classic Groups API:

    POST /{group-id}/feed

Meta's own "Facebook Groups API for Business" is built on that, and it runs
on `pages_manage_posts` - a permission this app ALREADY HOLDS. A page
posting into a group it belongs to does not need `publish_to_groups`.

The only thing that has to be true first is that the page is a member of
the group. There is no API for listing a page's groups - the Graph API
answers "(#100) Tried accessing nonexisting field (groups)" - so the owner
adds the page by hand, once, and this script does the rest.

WHAT IT DOES
------------
    python group_test.py --group <group-url-or-name> --page ethan

  1. Resolves the group name to its numeric id, using the PAGE token. This
     works once the page is a member of the group.
  2. Reads the group's feed with the page token - can it even see it?
  3. Calls `POST /{group-id}/feed` with `published=false`. Nothing is
     published; it exercises every check the real call would.
  4. Calls `POST /{page-id}/sharedposts` the same way, for comparison.
  5. Prints the verdict and what it would take to fix it.

Only page tokens are used. The personal account is never signed in, read,
or stored.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
GRAPH = "https://graph.facebook.com/v26.0"

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


def page_token() -> str:
    tok = (os.getenv("FB_PAGE_ACCESS_TOKEN") or "").strip()
    if not tok:
        raise SystemExit("No FB_PAGE_ACCESS_TOKEN in the environment.")
    return tok


def slug_of(name: str) -> str:
    s = str(name or "").strip()
    m = re.search(r"facebook\.com/groups/([^/?#]+)", s)
    if m:
        return m.group(1)
    s = re.sub(r"^https?://(www\.)?facebook\.com/groups/", "", s)
    return s.strip("/").split("?")[0]


def get(path: str, token: str, **params) -> dict:
    params["access_token"] = token
    import requests
    r = requests.get(f"{GRAPH}{path}", params=params, timeout=30)
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text[:300]}


def post(path: str, token: str, **data) -> dict:
    data["access_token"] = token
    import requests
    r = requests.post(f"{GRAPH}{path}", data=data, timeout=30)
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text[:300]}


def err(b: dict) -> str:
    e = b.get("error", {})
    return f"({e.get('code')}) {e.get('message', '')}"


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser(
        description="Test whether the app can post into a group as the page.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--group", required=True,
                    help="the group's URL or name, e.g. "
                         "facebook.com/groups/Some-Group")
    ap.add_argument("--page", choices=sorted(PAGES), default="ethan")
    args = ap.parse_args()

    tok = page_token()
    p = PAGES[args.page]
    slug = slug_of(args.group)
    print(f"\n=== testing: {p['name']} -> {slug} ===\n")

    # 1. resolve
    print("1. resolve the group id")
    res = get(f"/{slug}", tok, fields="id,name,privacy,member_count")
    if "error" in res:
        print("   FAILED:", err(res))
        print("   If the page is already a member of the group, this should"
              " work. If it does not:")
        print("   - the group name is wrong (copy the URL from the group's"
              " address bar), or")
        print("   - the page has not been added to the group yet:")
        print("     page settings -> Groups -> add, then re-run.")
        return 1
    gid = str(res.get("id"))
    print(f"   ok: id={gid}  {res.get('name')!r}  {res.get('privacy')}  "
          f"members={res.get('member_count')}")

    # 2. read
    print("\n2. read the group's feed with the page token")
    feed = get(f"/{gid}/feed", tok, fields="id,message,from", limit=1)
    if "error" in feed:
        print("   FAILED:", err(feed))
    else:
        rows = feed.get("data") or []
        print(f"   ok: {len(rows)} post(s) visible")

    # 3. the classic Groups API, nothing published
    print("\n3. POST /{group-id}/feed with the PAGE token (published=false)")
    res = post(f"/{gid}/feed", tok, message="probe", published="false")
    if "error" in res:
        print("   BLOCKED:", err(res))
        code = res["error"].get("code")
        low = (res["error"].get("message") or "").lower()
        if code == 10:
            print("   -> the classic Groups API is closed for this app too.")
            print("      Next: Meta Business Suite -> the group -> link it to"
                  " the Business")
            print("      and add the page. That is the 'Facebook Groups API"
                  " for Business'")
            print("      route, which runs on pages_manage_posts - no App"
                  " Review.")
        elif "member" in low or "not" in low and "group" in low:
            print("   -> the mechanics are right and the permission is"
                  " there: the page is")
            print("      simply not a member of this group. Add it: page"
                  " settings -> Groups.")
    else:
        print(f"   WORKS: {res.get('id')} (published=false, nothing went out)")

    # 4. sharedposts for comparison
    print("\n4. POST /{page-id}/sharedposts with the PAGE token (published=false)")
    res = post(f"/{p['id']}/sharedposts", tok, message="probe",
               link="https://example.com", published="false")
    if "error" in res:
        print("   BLOCKED:", err(res))
    else:
        print(f"   WORKS: {res.get('id')} (published=false, nothing went out)")

    print("\n=== verdict ===")
    print("If step 3 worked, add the group to bot/page_groups.json and run:")
    print(f"    python group_share_page.py --share --page {args.page}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
