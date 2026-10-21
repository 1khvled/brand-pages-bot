"""
eight-dollar / bot / group_share_page.py
---------------------------------------
Share the pages' posts into Facebook GROUPS **as the page**.

⛔ NEVER THE PERSONAL ACCOUNT.
-------------------------------
The personal Facebook account ("Khaled Abdelli") is off limits: it must
never post, browse, comment, react, message, share, or be stored as a
session on disk. This module never opens a browser and never touches a
personal session. It publishes through a page-scoped token, so every post
appears as the page - Ethan Cole or سوق اليوم - and the personal
account never appears as an author.

WHAT IS NEEDED BEFORE IT WORKS
------------------------------
1. **The page must be a member of the group.** The owner adds it once, by
   hand: page settings -> Groups -> add. A page cannot post into a group it
   does not belong to. There is no API for listing a page's groups - the
   Graph API answers "(#100) Tried accessing nonexisting field (groups)" -
   so the target list is maintained here, in `page_groups.json`.
2. **The app must hold `publish_to_groups`.** App id 1067784715881441 does
   NOT have it. Without it the API answers, live:

       (#10) Application does not have the capability to make this API call.

   That is App Review. See eight-dollar/GROUP-SHARING-STEP-BY-STEP.md.

TARGET LIST
-----------
`page_groups.json`:

    {
      "ethan":  [ {"name": "Some Trading Group", "id": "123456789"},
                  {"name": "Another Group"} ],
      "market": [ {"name": "Arabic Markets Group"} ]
    }

Only `name` is required - the id is resolved automatically from the group's
vanity URL (`facebook.com/groups/<name>`), which is much easier to type
than a raw id.

USAGE
-----
    python group_share_page.py --list-groups
    python group_share_page.py --probe --page ethan
    python group_share_page.py --resolve
    python group_share_page.py --share --page ethan --dry-run
    python group_share_page.py --share --page ethan
"""
from __future__ import annotations

import argparse
import json
import os
import re
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

GROUPS_FILE = HERE / "page_groups.json"
STATE_FILE = HERE / "page_share_state.json"


def load_env() -> None:
    """Read the page tokens from the bot repos' .env files, if present."""
    for candidate in (ROOT.parent / "ethan-cole-fb-bot" / ".env",
                      ROOT.parent / "soq-alyoum-fb-bot" / ".env",
                      ROOT / ".env"):
        if not candidate.exists():
            continue
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
            f"No page token for {p['name']}. Set {p['token_env']} in the bot "
            f"repo's .env (or the environment).")
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


def _slug(name: str) -> str:
    """Turn 'facebook.com/groups/Some-Group' or 'Some Group' into 'Some-Group'."""
    s = str(name or "").strip()
    m = re.search(r"facebook\.com/groups/([^/?#]+)", s)
    if m:
        return m.group(1)
    s = re.sub(r"^https?://", "", s)
    return re.sub(r"[^A-Za-z0-9.\-_]+", "-", s).strip("-")


def resolve_group(token: str, entry: dict) -> dict:
    """Resolve a group entry to its numeric id and real name."""
    gid = str(entry.get("id", "")).strip()
    name = str(entry.get("name", "")).strip()
    if gid:
        return {**entry, "id": gid, "name": name or gid, "_ok": True}
    slug = _slug(name)
    if not slug:
        return {**entry, "_ok": False, "_err": "no id and no usable name"}
    res = api("GET", f"/{slug}", token, fields="id,name,privacy")
    if "error" in res:
        e = res["error"]
        return {**entry, "_ok": False,
                "_err": f"({e.get('code')}) {e.get('message', '')[:90]}"}
    return {**entry, "id": str(res.get("id", "")), "name": res.get("name", name),
            "privacy": res.get("privacy", ""), "_ok": True}


def groups_for(page_key: str, token: str) -> list:
    """The target groups, resolved."""
    if not GROUPS_FILE.exists():
        return []
    data = json.loads(GROUPS_FILE.read_text(encoding="utf-8-sig"))
    out = []
    for e in data.get(page_key, []):
        out.append(resolve_group(token, e))
    return out


def list_groups(page_key: str) -> int:
    p = PAGES[page_key]
    token = page_token(page_key)
    print(f"\n=== {p['name']} ({p['id']}) ===")
    gs = groups_for(page_key, token)
    if not gs:
        print("  no groups listed for this page yet.")
        print(f"  add them to {GROUPS_FILE.name} under \"{page_key}\", like:")
        print('    "ethan": [ {"name": "Some Trading Group"} ]')
        return 0
    for g in gs:
        if g.get("_ok"):
            print(f"  ok    {g.get('name'):<44} id={g.get('id')} "
                  f"{g.get('privacy','')}")
        else:
            print(f"  FAIL  {g.get('name',''):<44} {g.get('_err')}")
    return 0


def resolve_all() -> int:
    """Resolve every page's group list and print what resolved."""
    for key in PAGES:
        list_groups(key)
    return 0


def latest_post(page_key: str) -> dict | None:
    p = PAGES[page_key]
    token = page_token(page_key)
    data = api("GET", f"/{p['id']}/feed", token,
               limit=2, fields="id,created_time,message,permalink_url")
    posts = data.get("data") or []
    if not posts:
        print("  no posts on the page yet")
        return None
    post = posts[0]
    link = post.get("permalink_url")
    if not link:
        post_id = post.get("id", "")
        if post_id:
            link = f"https://www.facebook.com/{post_id.replace('_', '/posts/')}"
    post["_link"] = link
    return post


def probe(page_key: str) -> int:
    """Test whether sharedposts works at all, without posting anything."""
    p = PAGES[page_key]
    token = page_token(page_key)
    print(f"\n=== probing {p['name']} ===")
    data = api("GET", f"/{p['id']}", token, fields="id,name,fan_count")
    if "error" in data:
        print("  the page token does not work:", data["error"].get("message"))
        return 1
    print("  the page token works:", data.get("name"),
          "| fans:", data.get("fan_count"))

    # published=false still exercises the permission check and posts nothing
    res = api("POST", f"/{p['id']}/sharedposts", token,
              message="", link="https://example.com", published="false")
    if "error" in res:
        e = res["error"]
        msg, code = e.get("message", ""), e.get("code")
        print("  sharedposts is BLOCKED:", f"({code}) {msg}")
        if code == 10:
            print("  -> the app is missing the publish_to_groups permission.")
            print("     App id 1067784715881441 -> App Review -> Permissions")
            print("     and Features -> add publish_to_groups.")
            print("     Full steps: eight-dollar/GROUP-SHARING-STEP-BY-STEP.md")
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
    # a shared post shows the preview, so the text stays short
    text = (message.split("\n\n")[0][:480] if message else "")
    text = (text + f"\n\n{link}").strip()

    gs = groups_for(page_key, token)
    if not gs:
        print("  no groups listed for this page")
        return 1
    bad = [g for g in gs if not g.get("_ok")]
    if bad:
        print("  these could not be resolved:")
        for g in bad:
            print(f"    {g.get('name')}: {g.get('_err')}")

    state = {}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    done = state.get(page_key, {})

    print(f"\n=== sharing into {len(gs)} group(s) as {p['name']} ===")
    ok = fail = 0
    for g in gs:
        if not g.get("_ok"):
            continue
        gid = str(g.get("id", ""))
        name = g.get("name", gid)
        if done.get(gid) == post.get("id"):
            print(f"  {name:<40} already shared this post - skipped")
            continue
        if dry_run:
            print(f"  {name:<40} WOULD POST  {text[:66]}...")
            continue
        res = api("POST", f"/{p['id']}/sharedposts", token,
                  message=text, link=link, published="true")
        if "error" in res:
            e = res["error"]
            print(f"  {name:<40} FAILED: ({e.get('code')}) "
                  f"{e.get('message', '')[:90]}")
            fail += 1
            if e.get("code") == 10:
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
    ap = argparse.ArgumentParser(
        description="Share the pages' posts into groups, as the page.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--page", choices=sorted(PAGES), default="ethan")
    ap.add_argument("--list-groups", action="store_true")
    ap.add_argument("--resolve", action="store_true",
                    help="resolve every page's group list and show what works")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--share", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--delay", type=int, default=22)
    args = ap.parse_args()

    if args.resolve:
        return resolve_all()
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
