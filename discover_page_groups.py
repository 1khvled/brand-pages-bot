"""
Find the groups the Ethan Cole PAGE has joined - using the saved session
to READ, then the page token for everything else.

The owner added the page to a number of groups by hand. There is no API
for listing a page's groups ("nonexisting field (groups)"), so this opens
the page's own Groups tab in the browser with the saved session and reads
the names off the page.

RULES, enforced here:
  - this READS the PAGE's groups tab. It does not post, comment, react,
    message, or read the personal account's feed, and it never touches
    the personal account's own content.
  - nothing is posted from this session. The only publisher in this
    project is the page, using a page-scoped token.
  - the saved personal session is DELETED at the end, so nothing can use
    it later by accident.

It writes the group names it finds into bot/page_groups.json, ready for
group_share_page.py.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
PROFILE = HERE / ".fbprofile"
TARGETS = HERE / "page_groups.json"
SHOTS = HERE.parent / "out"

PAGES = {
    "Ethan Cole": "1264009986803820",
    "souk today The Market Today": "1421089964411983",
}


async def main() -> int:
    from playwright.async_api import async_playwright

    if not PROFILE.exists():
        print("no saved session - nothing to read")
        return 1

    found: dict[str, list[str]] = {}
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(PROFILE), headless=True,
            args=["--disable-blink-features=AutomationControlled"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        for name, pid in PAGES.items():
            url = f"https://www.facebook.com/{pid}/groups"
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(6000)
            except Exception as ex:
                print(f"  {name}: could not open the groups tab ({str(ex)[:60]})")
                continue
            # the groups show up as links to /groups/<slug>
            hrefs = await page.eval_on_selector_all(
                "a[href*='/groups/']",
                "els => els.map(e => e.getAttribute('href'))")
            slugs = []
            for h in hrefs or []:
                m = re.search(r"/groups/([^/?#]+)", h or "")
                if m and m.group(1) not in ("", "home", "feed"):
                    s = m.group(1)
                    if s not in slugs:
                        slugs.append(s)
            print(f"  {name}: {len(slugs)} group link(s) on its groups tab")
            for s in slugs[:40]:
                print(f"      {s}")
            found[name] = slugs
            SHOTS.mkdir(parents=True, exist_ok=True)
            try:
                await page.screenshot(path=str(SHOTS / f"groups_{pid}.png"))
            except Exception:
                pass
        await ctx.close()

    # write what we found, keyed by the page key the bot uses
    data = {}
    if TARGETS.exists():
        try:
            data = json.loads(TARGETS.read_text(encoding="utf-8-sig"))
        except Exception:
            data = {}
    key_for = {"Ethan Cole": "ethan",
               "souk today The Market Today": "market"}
    for name, slugs in found.items():
        k = key_for.get(name)
        if not k:
            continue
        entries = [{"name": s} for s in slugs]
        if entries:
            data.setdefault(k, [])
            have = {e.get("name") for e in data[k]}
            for e in entries:
                if e["name"] not in have:
                    data[k].append(e)
    TARGETS.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    print(f"\nwrote {TARGETS.name}: "
          + ", ".join(f"{k}={len(v)}" for k, v in data.items()))

    # ---- and now the personal session goes, for good
    shutil.rmtree(PROFILE, ignore_errors=True)
    print("the saved personal session has been deleted from this machine")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
