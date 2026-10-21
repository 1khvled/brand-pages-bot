"""
eight-dollar / bot / group_share.py
-----------------------------------
⛔ DISABLED — AND IT STAYS THAT WAY.

The owner's personal Facebook account ("Khaled Abdelli") is off limits. It
must never post, browse, comment, react, message, share, or be stored on
this machine as a session. Every public post goes out as a PAGE.

This module used to sign in as the personal account and drive the Facebook
UI to share the pages' posts into groups. It did that because the app
lacks the `publish_to_groups` permission, so the API answer was:

    (#10) Application does not have the capability to make this API call.

That reason does not matter. Signing in as the personal account is
forbidden, so **the browser code has been deleted**, not just disabled. The
saved session was removed from this machine on 22 October 2026: no profile
folder, no cookie file, no browser process.

THE REPLACEMENT
---------------
    bot/group_share_page.py

It never opens a browser and never touches a personal session. It posts
into groups **as the page** - Ethan Cole (1264009986803820) or سوق اليوم
(1421089964411983) - over the Graph API with a page-scoped token.

Two things are needed before it can post into a group:

1. The page must be a member of the group. The owner adds it once, by
   hand: page settings -> Groups -> add.
2. The app must hold `publish_to_groups`. That needs App Review:
   facebook.com/developers -> your app -> App Review -> permissions.

Check where it stands at any time:

    python group_share_page.py --probe --page ethan

THE RULE
--------
See eight-dollar/POLICY.md. It is an owner mandate, not a preference. If a
task appears to need the personal account, stop and ask - do not
improvise, and do not restore the code below.
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REFUSAL = """
REFUSED. This command signs in as your personal Facebook account
("Khaled Abdelli"). Your personal account is off limits - it must never
post, browse, or be stored as a session on disk. Every public post goes
out as a PAGE: Ethan Cole (1264009986803820) or سوق اليوم (1421089964411983).

The browser code that used to live here has been deleted, not merely
disabled. To share into groups, use the page-token path:

    python group_share_page.py --list-groups
    python group_share_page.py --probe --page ethan
    python group_share_page.py --share --page ethan --dry-run

That posts as the page, not as you. See eight-dollar/POLICY.md.
"""


def main() -> int:
    print(REFUSAL)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
