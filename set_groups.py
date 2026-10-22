"""
eight-dollar / bot / set_groups.py
----------------------------------
Paste the group names, get everything done in one pass.

The Graph API cannot enumerate the groups a page belongs to. It was tried
every way there is, live, today:

    GET /{page-id}/groups              -> (#100) nonexisting field (groups)
    GET /{page-id}/tabs                -> (200)  Permissions error
    GET /{page-id}/owned_groups        -> (#2500) Unknown path
    GET /{page-id}/group_relationships -> (#2500) Unknown path
    GET /{page-id}/feed?fields=sharedposts -> no group ids
    GET /{business-id}/owned_groups    -> (#2500) Unknown path
    GET /{group-vanity}                -> does not resolve for groups

and the page reports `business: null`, so there is no Business Manager to
list groups from either.

So the names have to come from the owner. This takes them as a plain list
and does the rest:

  1. writes them into page_groups.json
  2. resolves each one to its numeric id, with the PAGE token
  3. tests POST /{group-id}/feed on each, with published=false - nothing
     is published, but every check the real call would run
  4. prints a table: resolved? / can we post? / what is blocking it

Only page tokens are used. The personal account is never read.

USAGE
-----
    python set_groups.py --page ethan --file groups.txt
    python set_groups.py --page ethan --stdin          # paste, then Ctrl-D
    python set_groups.py --page ethan --group facebook.com/groups/One
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
GROUPS_FILE = HERE / "page_groups.json"

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
    return s.strip("/").split("?")[0].split("#")[0]


def get(path: str, token: str, **params) -> dict:
    params["access_token"] = token
    import requests
    r = requests.get(f"{GRAPH}{path}", params=params, timeout=30)
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text[:200]}


def post(path: str, token: str, **data) -> dict:
    data["access_token"] = token
    import requests
    r = requests.post(f"{GRAPH}{path}", data=data, timeout=30)
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text[:200]}


def read_names(args) -> list:
    names = []
    if args.group:
        names += list(args.group)
    if args.file:
        p = Path(args.file)
        if not p.exists():
            raise SystemExit(f"No such file: {p}")
        names += p.read_text(encoding="utf-8", errors="replace").splitlines()
    if args.stdin:
        names += sys.stdin.read().splitlines()
    out = []
    for n in names:
        n = str(n).strip()
        if not n or n.startswith("#"):
            continue
        out.append(n)
    return out


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser(
        description="Set the group's target list and test whether the app "
                    "can post into them as the page.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--page", choices=sorted(PAGES), default="ethan")
    ap.add_argument("--file", help="a text file, one group URL or name per line")
    ap.add_argument("--stdin", action="store_true", help="read the list from stdin")
    ap.add_argument("--group", action="append", default=[],
                    help="a single group URL or name (repeatable)")
    ap.add_argument("--no-test", action="store_true",
                    help="write the list without testing")
    args = ap.parse_args()

    names = read_names(args)
    if not names:
        print("No group names given.\n")
        ap.print_help()
        return 1

    tok = page_token()
    p = PAGES[args.page]
    print(f"\n=== {p['name']} ({p['id']}) - {len(names)} group(s) ===\n")

    # write the target list
    data = {}
    if GROUPS_FILE.exists():
        data = json.loads(GROUPS_FILE.read_text(encoding="utf-8-sig"))
    data[args.page] = [{"name": n} for n in names]
    GROUPS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(f"wrote {GROUPS_FILE.name}: {args.page} -> {len(names)} group(s)\n")

    # resolve + test
    print(f"  {'group':<40}{'resolved':<12}{'post':<10}note")
    print("  " + "-" * 96)
    workable = 0
    for n in names:
        slug = slug_of(n)
        res = get(f"/{slug}", tok, fields="id,name,privacy,member_count")
        if "error" in res:
            e = res["error"]
            print(f"  {n[:39]:<40}{'no':<12}{'-':<10}"
                  f"({e.get('code')}) {e.get('message','')[:44]}")
            continue
        gid = str(res.get("id"))
        real = res.get("name", n)
        if args.no_test:
            print(f"  {real[:39]:<40}{gid:<12}{'-':<10}privacy="
                  f"{res.get('privacy','')}")
            workable += 1
            continue
        r = post(f"/{gid}/feed", tok, message="probe", published="false")
        if "error" in r:
            e = r["error"]
            note = f"({e.get('code')}) {e.get('message','')[:52]}"
            print(f"  {real[:39]:<40}{gid:<12}{'BLOCKED':<10}{note}")
        else:
            print(f"  {real[:39]:<40}{gid:<12}{'WORKS':<10}"
                  f"privacy={res.get('privacy','')}")
            workable += 1

    print()
    if workable == len(names) and not args.no_test:
        print(f"ALL {workable} GROUP(S) CAN BE POSTED TO.")
        print(f"  python group_share_page.py --share --page {args.page}")
    elif workable:
        print(f"{workable} of {len(names)} resolved and ready. The rest need "
              f"the page added to them,")
        print("or a business-linked group (Meta Business Suite -> the group "
              "-> link it).")
    else:
        print("None resolved. The page must be a member of the group for the "
              "page token")
        print("to see it: page settings -> Groups -> add.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
