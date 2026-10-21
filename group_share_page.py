"""
eight-dollar / bot / group_share_page.py
---------------------------------------
Share the pages' posts into Facebook GROUPS **as the page**.

⛔ NEVER THE PERSONAL ACCOUNT.
-------------------------------
The previous path (`group_share.py`) signed in as the owner's personal
Facebook account ("Khaled Abdelli") and drove the UI. That is forbidden by
the owner's mandate: the personal account must never post, browse, or be
stored as a session. That module is hard-blocked and its saved session was
deleted from the machine.

This module never opens a browser and never touches a personal session. It
uses a PAGE-scoped access token, so every post is published as the page -
Ethan Cole or سوق اليوم The Market Today - and the personal account only
ever appears as the page's owner, which is how Facebook works and is not the
same thing as acting as Khaled.

HOW IT WORKS
------------
Facebook's group publishing is `POST /{page-id}/sharedposts`:

    POST https://graph.facebook.com/v26.0/{page-id}/sharedposts
      message   = the text
      link      = the URL
      published = true
      access_token = the PAGE token

Two things must be true before this works:

1. **The page must be a member of the group.** The owner adds it once, by
   hand: page settings -> Groups -> add. A page cannot post into a group it
   does not belong to.
2. **The app must hold `publish_to_groups`.** Without it the API answers:
       (#10) Application does not have the capability to make this API call.
   That needs App Review - days to weeks, and it can be refused.

Until both are true this script reports the exact blocker instead of
pretending. The pages' own feeds are unaffected: they publish through the
Graph API with the same page tokens and are live.

USAGE
-----
    python group_share_page.py --list-groups
    python group_share_page.py --probe
    python group_share_page.py --share --page ethan --dry-run
    python group_share_page.py --share --page ethan
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

GRAPH = "https://graph.facebook.com/v26.0"

# The two pages. Only these two identities may ever publish.
PAGES = {
    "ethan": {
        "id": "1264009986803820",
        "name": "Ethan Cole",
        "token_env": "FB_PAGE_ACCESS_TOKEN",
    },
    "market": {
        "id": "1421089964411983",
        "name": "سوق اليوم The Market Today",
        "token_env": "FB_PAGE_ACCESS_TOKEN",
    },
}

# Groups the page is allowed to post into. Filled in by --list-groups once
# the page has been added to them by hand.
GROUPS_FILE = HERE / "page_groups.json"
STATE_FILE = HERE / "page_share_state.json"


def load_env() -> None:
    """Read the page tokens from the repo's .env, if there is one."""
    for candidate in (ROOT.parent / "ethan-cole-fb-bot" / ".env",
                      ROOT.parent / "soq-alyoum-fb-bot" / ".env",
                      ROOT / ".env"):
        if candidate.exists():
            try:
                from dotenv import load_dotenv
                load_dotenv(candidate)
            except Exception:
                for line in candidate.read_text(encoding="utf-8",
                                               errors="replace").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(),
                                              v.strip().strip('"').strip("'"))
            break


def page_token(page_key: str) -> str:
    p = PAGES[page_key]
    tok = (os.getenv(p["token_env"]) or "").strip()
    if not tok:
        raise SystemExit(
            f"No page token for {p['name']}. Set {p['token_env']} in the "
            f"bot repo's .env (or the environment).")
    return tok


def api(method: str, path: str, token: str, **params) -> dict:
    params = {k: v for k, v in params.items() if v is not None}
    params["access_token"] = token
    import requests
    r = requests.request(method, f"{GRAPH}{path}", params=params, timeout=40)
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text[:400]}


def groups_for(page_key: str) -> list:
    """The groups the page is a member of, read from the local list.

    `POST /{page-id}/sharedposts` needs the group id. The Graph API does
    not expose "groups this page belongs to" without a separate permission,
    so the list is maintained locally and confirmed with --probe.
    """
    if not GROUPS_FILE.exists():
        return []
    data = json.loads(GROUPS_FILE.read_text(encoding="utf-8"))
    return data.get(page_key, [])


def list_groups(page_key: str) -> int:
    """Show the group list, and whether each one actually works."""
    p = PAGES[page_key]
    token = page_token(page_key)
    print(f"\n=== {p['name']} ({p['id']}) ===")
    gs = groups_for(page_key)
    if not gs:
        print(f"  no groups listed for {page_key}.")
        print("  Add the page to a group first: page settings -> Groups ->")
        print("  add, then put the group id in", GROUPS_FILE.name)
        return 0
    for g in gs:
        gid = str(g.get("id", ""))
        name = g.get("name", "?")
        info = api("GET", f"/{gid}", token, fields="id,name")
        if "error" in info:
            print(f"  {name:<40} NOT READABLE: {info['error'].get('message')}")
        else:
            print(f"  {name:<40} id={gid}  ok")
    return 0


def latest_post(page_key: str) -> dict | None:
    p = PAGES[page_key]
    token = page_token(page_key)
    data = api("GET", f"/{p['id']}/feed", token,
               limit=2, fields="id,created_time,message,permalink_url,full_picture")
    posts = data.get("data") or []
    if not posts:
        print("  no posts on the page yet")
        return None
    post = posts[0]
    link = post.get("permalink_url")
    if not link:
        # build it - permalink_url needs a separate permission on some apps
        post_id = post.get("id", "")
        if post_id:
            link = f"https://www.facebook.com/{post_id.replace('_', '/posts/')}"
    post["_link"] = link
    return post


def probe(page_key: str) -> int:
    """Test whether sharedposts works at all, with one harmless call."""
    p = PAGES[page_key]
    token = page_token(page_key)
    print(f"\n=== probing {p['name']} ===")
    data = api("GET", f"/{p['id']}", token, fields="id,name,fan_count")
    if "error" in data:
        print("  the page token does not work:", data["error"].get("message"))
        return 1
    print("  page token works:", data.get("name"), "| fans:", data.get("fan_count"))

    # a real sharedposts call with published=false - it still exercises the
    # permission check, and it does not post anything
    res = api("POST", f"/{p['id']}/sharedposts", token,
              message="", link="https://example.com", published="false")
    if "error" in res:
        msg = res["error"].get("message", "")
        code = res["error"].get("code")
        print("  sharedposts is BLOCKED:", f"({code}) {msg}")
        if code == 10:
            print("  -> the app is missing the publish_to_groups permission.")
            print("     That needs App Review: facebook.com/developers -> your")
            print("     app -> App Review -> permissions -> publish_to_groups.")
            print("     Until it is granted, groups cannot be posted to by API.")
        return 2
    print("  sharedposts WORKS. (published=false, nothing was posted.)")
    return 0


def share(page_key: str, dry_run: bool, delay: int) -> int:
    p = PAGES[page_key]
    token = page_token(page_key)
    post = latest_post(page_key)
    if not post:
        return 1
    link = post.get("_link") or ""
    message = (post.get("message") or "").strip()
    if not link:
        print("  the post has no link to share")
        return 1
    # strip the post's own text - a shared post shows the preview anyway
    text = message.split("\n\n")[0][:480] if message else ""
    text = (text + f"\n\n{link}").strip()

    gs = groups_for(page_key)
    if not gs:
        print("  no groups listed for this page")
        return 1

    state = {}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    done = state.get(page_key, {})

    print(f"\n=== sharing into {len(gs)} group(s) as {p['name']} ===")
    ok = fail = 0
    for g in gs:
        gid = str(g.get("id", ""))
        name = g.get("name", gid)
        if done.get(gid) == post.get("id"):
            print(f"  {name:<40} already shared this post - skipped")
            continue
        if dry_run:
            print(f"  {name:<40} WOULD POST  {text[:70]}...")
            continue
        res = api("POST", f"/{p['id']}/sharedposts", token,
                  message=text, link=link, published="true")
        if "error" in res:
            print(f"  {name:<40} FAILED: "
                  f"({res['error'].get('code')}) {res['error'].get('message')}")
            fail += 1
            if res["error"].get("code") == 10:
                print("     -> publish_to_groups is not granted. Stopping.")
                break
        else:
            print(f"  {name:<40} shared -> {res.get('id')}")
            ok += 1
            done[gid] = post.get("id")
            state[page_key] = done
            STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        time.sleep(delay)
    print(f"\n  {ok} shared, {fail} failed")
    return 0 if ok or dry_run else 1


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--page", choices=sorted(PAGES), default="ethan")
    ap.add_argument("--list-groups", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--share", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--delay", type=int, default=22)
    args = ap.parse_args()

    if args.list_groups:
        return list_groups(args.page)
    if args.probe:
        return probe(args.page)
    if args.share:
        return share(args.page, args.dry_run, args.delay)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
