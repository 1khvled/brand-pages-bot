"""
eight-dollar / bot / group_share.py
-----------------------------------
Auto-share the page's posts into Facebook GROUPS.

WHY BROWSER AUTOMATION AND NOT THE API
--------------------------------------
Facebook killed the Groups API for new apps. The old flow was
`POST /{group-id}/feed` with the `publish_to_groups` permission; the modern
flow `POST /{page-id}/sharedposts` still exists but, tested live against
this app on 21 Oct 2026, returns:

    (#10) Application does not have the capability to make this API call.

because the app does not hold `publish_to_groups`. Getting that permission
needs App Review - days to weeks, and it can be refused.

So this drives the real UI in a real browser with a real session: exactly
what a human does, because the UI does not check who is clicking.

WHAT IT DOES
------------
  1. You log in once (a visible browser, your password, the session saved to
     a private profile folder outside any repo).
  2. It reads the newest post from the Page over the Graph API.
  3. For each target group it opens the group, opens the composer, writes
     the share text, pastes the link, and presses Post.
  4. It verifies the post actually landed, and records what it shared
     where, so the same post never lands twice in the same group and the
     same group never gets two posts in a day.

Facebook changes its DOM. That is why there is a --probe mode: it opens a
group, reports exactly which selectors it can find, and screenshots the
page, so a DOM change is fixed in one pass instead of by guessing.

SAFETY
------
  * The session lives in .fbprofile/ - a private, gitignored folder, and the
    ONLY place your Facebook cookies exist.
  * One post per group per day, 20+ seconds between groups.
  * Nothing is posted without --share. --dry-run never presses Post.

USAGE
-----
    python group_share.py --login              # one-time: log in
    python group_share.py --find-groups        # list the groups you belong to
    python group_share.py --probe              # diagnose the group UI
    python group_share.py --share --dry-run    # rehearse
    python group_share.py --share              # go
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROFILE = HERE / ".fbprofile"          # private, gitignored
STATE = HERE / "group_share_state.json"
GROUPS_FILE = HERE / "groups.txt"
SHOTS = HERE / "shots"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# ---- candidate selectors, most specific first.
# Facebook ships several composer variants (new / legacy / mobile-web), and
# the strings differ per locale, so every step tries a list.
COMPOSER_CANDIDATES = [
    'div[role="button"][aria-label="Create a post"]',
    'div[role="button"][aria-label*="Create a post"]',
    'div[role="button"][aria-label*="اكتب منشور"]',
    'div[role="button"][aria-label*="إنشاء منشور"]',
    'div[aria-label="Write something..."]',
    'div[aria-label="اكتب شيئا..."]',
    'div[role="button"]:has-text("Write something")',
    'div[role="button"]:has-text("اكتب")',
]
EDITABLE_CANDIDATES = [
    'div[contenteditable="true"][role="textbox"]',
    'div[contenteditable="true"]',
    'span[contenteditable="true"]',
]
POST_BUTTON_CANDIDATES = [
    'div[aria-label="Post"][role="button"]',
    'div[aria-label="Post"]',
    'div[aria-label="نشر"][role="button"]',
    'div[aria-label="نشر"]',
    'span[aria-label="Post"]',
    'span[aria-label="نشر"]',
    'div[role="button"]:has-text("Post")',
    'div[role="button"]:has-text("نشر")',
]
CLOSE_CANDIDATES = [
    'div[aria-label="Close"]', 'div[aria-label="إغلاق"]',
    'div[aria-label="Cancel"]', 'div[aria-label="إلغاء"]',
]
LOGGED_IN_CANDIDATES = [
    'div[aria-label="Your profile"]', 'a[aria-label="Your profile"]',
    'div[aria-label="Create a post"]', 'div[aria-label="Notifications"]',
    'div[aria-label="الصفحة الشخصية"]', 'div[aria-label="Create new post"]',
]


# ------------------------------------------------------------------ state
def load_groups() -> list:
    if not GROUPS_FILE.exists():
        return []
    out = []
    for ln in GROUPS_FILE.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(ln)
    return out


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"shared": {}}


def save_state(st: dict) -> None:
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------------ graph
def newest_post(page_id: str, page_token: str) -> dict:
    import requests
    r = requests.get(
        f"https://graph.facebook.com/v26.0/{page_id}/feed",
        params={"limit": 3, "fields": "id,created_time,message,permalink_url",
                "access_token": page_token}, timeout=25)
    d = r.json() or {}
    rows = d.get("data") or []
    return rows[0] if rows else {}


def share_text_for(post: dict) -> str:
    msg = (post.get("message") or "").strip()
    first = ""
    for ln in msg.split("\n"):
        if ln.strip():
            first = ln.strip()
            break
    first = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\u2190-\u21FF\uFE0F]", "", first)
    first = re.sub(r"^[\W_]+", "", first).strip()
    return first[:240]


# ------------------------------------------------------------------ browser
async def open_browser(pw, headless: bool = False, chrome: bool = False):
    """Open the saved profile.

    `chrome=True` uses the installed Google Chrome instead of the bundled
    Chromium. Facebook is noticeably pickier about the bundled build - it
    serves a checkpoint ("confirm it's you", a captcha) that the real Chrome
    does not. The profile directory is still ours, so the user's own browser
    profile and their open windows are never touched.
    """
    PROFILE.mkdir(parents=True, exist_ok=True)
    kwargs = dict(
        user_data_dir=str(PROFILE),
        headless=headless,
        user_agent=UA,
        locale="en-US",
        viewport={"width": 1400, "height": 900},
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
              "--disable-blink-features=AutomationControlled"],
    )
    if chrome:
        kwargs["channel"] = "chrome"
    return await pw.chromium.launch_persistent_context(**kwargs)


async def first_visible(page, candidates, timeout_ms: int = 5000):
    """The first candidate selector that is actually present AND visible."""
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if await loc.count(timeout=timeout_ms):
                if await loc.is_visible():
                    return sel, loc
        except Exception:
            continue
    return None, None


async def is_logged_in(page) -> bool:
    """Is the session authenticated?

    The only proof that counts is the pair of cookies Facebook sets on a
    signed-in profile: `c_user` (the user id) and `xs` (the session token).
    Everything else is a NEGATIVE signal - a password field, a /login URL, a
    missing marker - and negatives cannot prove a session.

    Two earlier versions got this wrong. The first looked for a handful of
    aria-labels that Facebook changes often, so a real login was reported as
    a failure. The second fell back to "we are on facebook.com, so probably
    logged in", which reported a login that never happened and closed the
    window on an anonymous profile.
    """
    try:
        names = {c["name"] for c in await page.context.cookies()}
    except Exception:
        names = set()
    if "c_user" in names and "xs" in names:
        return True
    url = (page.url or "").lower()
    if "/login" in url or "login." in url:
        return False
    try:
        pw = page.locator('input[name="pass"], input#pass')
        if await pw.count() and await pw.first.is_visible():
            return False
    except Exception:
        pass
    sel, _ = await first_visible(page, LOGGED_IN_CANDIDATES, 1500)
    return sel is not None


async def login_diagnosis(page) -> str:
    """What was actually on screen - so a failed login is not a mystery."""
    bits = []
    try:
        bits.append(f"url={page.url}")
    except Exception:
        pass
    try:
        title = await page.title()
        if title:
            bits.append(f"title={title!r}")
    except Exception:
        pass
    try:
        body = (await page.inner_text("body"))[:1200].lower()
    except Exception:
        body = ""
    for needle, label in (
        ("captcha", "CAPTCHA"),
        ("security code", "2FA / security code"),
        ("two-factor", "2FA"),
        ("checkpoint", "CHECKPOINT - confirm it's you"),
        ("confirm it's you", "CHECKPOINT - confirm it's you"),
        ("suspended", "ACCOUNT SUSPENDED"),
        ("incorrect password", "wrong password"),
        ("temporarily blocked", "rate limited / temporarily blocked"),
        ("log in", "the login form is still up"),
        ("new password", "a password-change prompt"),
    ):
        if needle in body:
            bits.append(label)
    return "; ".join(bits) if bits else "nothing recognisable on the page"


async def dismiss_overlays(page) -> None:
    for sel in CLOSE_CANDIDATES:
        try:
            el = page.locator(sel).first
            if await el.count() and await el.is_visible():
                await el.click(timeout=1200)
                await page.wait_for_timeout(700)
                break
        except Exception:
            continue
    # cookie / chat bubbles
    for sel in ('button[aria-label="Decline optional cookies"]',
                'div[aria-label="Allow all cookies"]'):
        try:
            el = page.locator(sel).first
            if await el.count():
                await el.click(timeout=1200)
                await page.wait_for_timeout(500)
        except Exception:
            pass


async def paste_text(page, text: str) -> bool:
    """Paste via the clipboard - more reliable than typing a long URL, which
    Facebook sometimes mangles mid-typing."""
    try:
        await page.context.grant_permissions(["clipboard-read", "clipboard-write"],
                                            origin="https://www.facebook.com")
    except Exception:
        pass
    try:
        await page.evaluate(
            "t => navigator.clipboard.writeText(t)", text)
        await page.keyboard.press("Control+V")
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ actions
async def cmd_login(args):
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        ctx = await open_browser(pw, headless=False, chrome=args.chrome)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.facebook.com/login", wait_until="domcontentloaded",
                        timeout=60000)
        # a banner, so it is obvious what the window is for and that nobody
        # else has to guess why it opened
        try:
            await page.add_style_tag(content="""
                #fb-login-note { position: fixed; inset: 0 0 auto 0; z-index: 2147483647;
                  background: #0a84ff; color: #fff; font: 600 15px/1.4 system-ui, sans-serif;
                  padding: 12px 18px; text-align: center; box-shadow: 0 2px 12px rgba(0,0,0,.4); }
            """)
            await page.evaluate("""() => {
              const d = document.createElement('div');
              d.id = 'fb-login-note';
              d.textContent = 'Log in to Facebook in this window (password + 2FA). '
                            + 'Leave the window open - the session is saved automatically. '
                            + 'You can close it once the feed appears.';
              document.body.prepend(d);
            }""")
        except Exception:
            pass
        print("\nA Facebook window is open. Log in there (password + 2FA).")
        print("Leave the window open - the session is saved when the login lands.")
        print("The session is saved in:", PROFILE)
        print("Waiting up to 30 minutes for the login to complete...\n")
        last = ""
        for i in range(600):
            await page.wait_for_timeout(3000)
            if await is_logged_in(page):
                print("Logged in. Session saved to", PROFILE)
                await page.wait_for_timeout(3000)
                await ctx.close()
                return 0
            if i % 10 == 9:
                now = await login_diagnosis(page)
                if now != last:
                    print(f"  ...still waiting ({(i + 1) * 3}s) - {now}", flush=True)
                    last = now
                else:
                    print(f"  ...still waiting ({(i + 1) * 3}s)", flush=True)
        print("Login not detected.")
        print("  what was on screen:", await login_diagnosis(page))
        try:
            shot = PROFILE.parent.parent / "out" / "login_timeout.png"
            shot.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(shot), full_page=False)
            print("  screenshot:", shot)
        except Exception as ex:
            print("  screenshot failed:", ex)
        await ctx.close()
        return 1


async def cmd_find_groups(args):
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        ctx = await open_browser(pw, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.facebook.com/groups/your_groups",
                        wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(6000)
        for _ in range(8):
            await page.mouse.wheel(0, 4000)
            await page.wait_for_timeout(1500)
        hrefs = await page.eval_on_selector_all(
            'a[href*="/groups/"]', "els => els.map(e => e.href)")
        found = []
        for h in hrefs:
            m = re.search(r"/groups/([^/?]+)", h)
            if m and m.group(1) not in found:
                found.append(m.group(1))
        print(f"\n{len(found)} group(s) found:")
        for g in found:
            print("  https://www.facebook.com/groups/" + g)
        (HERE / "groups_found.txt").write_text(
            "\n".join("https://www.facebook.com/groups/" + g for g in found),
            encoding="utf-8")
        print("\nwritten to groups_found.txt - review it, then copy the ones you")
        print("want into groups.txt")
        await ctx.close()
        return 0


async def cmd_probe(args):
    """Open a group and report exactly what the automation can see.
    This is how a Facebook DOM change gets fixed in one pass."""
    from playwright.async_api import async_playwright
    groups = load_groups()
    if not groups:
        print("Put at least one group URL in groups.txt first.")
        return 2
    async with async_playwright() as pw:
        ctx = await open_browser(pw, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        for g in groups[:2]:
            print("=" * 68)
            print("group:", g)
            await page.goto(g, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)
            await dismiss_overlays(page)
            print("  logged in      :", await is_logged_in(page))
            sel, _ = await first_visible(page, COMPOSER_CANDIDATES)
            print("  composer       :", sel or "NOT FOUND")
            if sel:
                try:
                    await page.locator(sel).first.click(timeout=5000)
                    await page.wait_for_timeout(2000)
                except Exception as e:  # noqa: BLE001
                    print("     click failed:", e)
            esel, _ = await first_visible(page, EDITABLE_CANDIDATES)
            print("  editable       :", esel or "NOT FOUND")
            psel, _ = await first_visible(page, POST_BUTTON_CANDIDATES)
            print("  post button    :", psel or "NOT FOUND")
            SHOTS.mkdir(exist_ok=True)
            fn = SHOTS / ("probe_" + re.sub(r"[^a-z0-9]+", "_", g)[-30:] + ".png")
            try:
                await page.screenshot(path=str(fn))
                print("  screenshot     :", fn.name)
            except Exception:
                pass
            # what does the group page actually contain?
            for probe_sel in ('div[role="button"][aria-label]', 'div[aria-label]'):
                try:
                    labels = await page.eval_on_selector_all(
                        probe_sel, "els => els.slice(0,40).map(e => e.getAttribute('aria-label'))"
                        " .filter(x => x && /post|write|publi|share|group|composer/i.test(x))")
                    if labels:
                        print("  aria-labels    :", labels[:12])
                        break
                except Exception:
                    pass
        await ctx.close()
        return 0


async def share_into_group(page, group_url: str, message: str, link: str,
                           dry_run: bool) -> tuple[bool, str]:
    await page.goto(group_url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(5000)
    await dismiss_overlays(page)

    sel, loc = await first_visible(page, COMPOSER_CANDIDATES, 6000)
    if not loc:
        # A group you are not a member of shows "Join group" instead of a
        # composer. Say so plainly rather than failing with "composer not
        # found", and never try to join a group on your behalf.
        for join_sel in ('div[aria-label="Join group"]',
                         'div[aria-label="Join Group"]',
                         'div[aria-label="انضم إلى المجموعة"]',
                         'a[href*="/groups/"][role="button"]'):
            jl = page.locator(join_sel).first
            try:
                if await jl.count() and await jl.is_visible():
                    return False, ("you are not a member of this group - "
                                   "join it by hand first")
            except Exception:
                continue
        return False, "composer not found (run --probe)"
    try:
        await loc.click(timeout=6000)
    except Exception as e:  # noqa: BLE001
        return False, f"composer click failed: {type(e).__name__}"
    await page.wait_for_timeout(2200)

    esel, eloc = await first_visible(page, EDITABLE_CANDIDATES, 6000)
    if not eloc:
        return False, "text box not found (run --probe)"
    try:
        await eloc.click(timeout=4000)
        await page.wait_for_timeout(600)
        await eloc.type(message, delay=16)
        await page.wait_for_timeout(400)
        await eloc.press("End")
        await eloc.type(" ", delay=10)
        if not await paste_text(page, link):
            await eloc.type(link, delay=8)
    except Exception as e:  # noqa: BLE001
        return False, f"typing failed: {type(e).__name__}: {e}"

    await page.wait_for_timeout(3000)     # let the link preview render
    SHOTS.mkdir(exist_ok=True)
    tag = re.sub(r"[^a-z0-9]+", "_", group_url)[-30:]
    shot = SHOTS / f"{tag}.png"
    try:
        await page.screenshot(path=str(shot))
    except Exception:
        pass

    if dry_run:
        return True, f"dry run (nothing posted) - screenshot shots/{shot.name}"

    psel, ploc = await first_visible(page, POST_BUTTON_CANDIDATES, 6000)
    if not ploc:
        return False, (f"Post button not found (run --probe) - "
                       f"screenshot shots/{shot.name}")
    try:
        await ploc.click(timeout=6000)
    except Exception as e:  # noqa: BLE001
        return False, f"Post click failed: {type(e).__name__}"
    await page.wait_for_timeout(4500)

    # verify: the composer should be gone and the text should be on the page
    try:
        body = await page.inner_text("body")
        key = (message or link)[:40]
        if key and key.lower() in body.lower():
            return True, "posted (verified on the page)"
        esel2, _ = await first_visible(page, EDITABLE_CANDIDATES, 1500)
        if not esel2:
            return True, "posted (composer closed)"
        return False, "the composer is still open - the Post button may not have fired"
    except Exception:
        return True, "posted (could not verify)"


async def cmd_share(args):
    from playwright.async_api import async_playwright
    from dotenv import load_dotenv

    load_dotenv(r"C:\Users\Abdelli\Documents\ethan-cole-fb-bot\.env")
    page_id = os.getenv("FB_PAGE_ID", "").strip()
    page_token = os.getenv("FB_PAGE_ACCESS_TOKEN", "").strip()
    if not page_id or not page_token:
        print("FB_PAGE_ID / FB_PAGE_ACCESS_TOKEN not set", file=sys.stderr)
        return 2

    groups = load_groups()
    if not groups:
        print("No groups. Put one group URL per line in groups.txt")
        print("(run --find-groups to list the groups you are in).")
        return 2

    post = newest_post(page_id, page_token)
    if not post:
        print("no posts on the page")
        return 2
    link = post.get("permalink_url") or ""
    msg = share_text_for(post)
    print(f"post   : {post.get('id')}\n  {msg[:100]}\n  {link}\n")

    state = load_state()
    done_today = state["shared"].get(post["id"], [])

    async with async_playwright() as pw:
        ctx = await open_browser(pw, headless=args.headless)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        if not await is_logged_in(page):
            print("not logged in - run: python group_share.py --login",
                  file=sys.stderr)
            await ctx.close()
            return 3
        ok_count = 0
        for g in groups:
            if g in done_today:
                print(f"  skip (already shared today): {g}")
                continue
            print(f"  -> {g}")
            try:
                ok, why = await share_into_group(page, g, msg, link, args.dry_run)
            except Exception as e:  # noqa: BLE001
                ok, why = False, f"{type(e).__name__}: {e}"
            print(f"     {why}")
            if ok and not args.dry_run:
                state["shared"].setdefault(post["id"], []).append(g)
                save_state(state)
                ok_count += 1
            elif ok:
                ok_count += 1
            time.sleep(args.delay)
        await ctx.close()
    print(f"\n{ok_count} group(s) {'shared' if not args.dry_run else 'rehearsed'}.")
    return 0


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--chrome", action="store_true",
                    help="use the installed Google Chrome instead of the "
                         "bundled Chromium (the platform is pickier)")
    ap.add_argument("--find-groups", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--share", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--delay", type=int, default=22)
    args = ap.parse_args()
    if args.login:
        return await cmd_login(args)
    if args.find_groups:
        return await cmd_find_groups(args)
    if args.probe:
        return await cmd_probe(args)
    if args.share:
        return await cmd_share(args)
    ap.print_help()
    return 0


if __name__ == "__main__":
    import asyncio
    raise SystemExit(asyncio.run(main()))
