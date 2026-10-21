"""
eight-dollar / bot / brand_bot.py
---------------------------------
The generalised client-page bot.

post_bot.py (Ethan Cole) is a proven pipeline for ONE brand: finance/AI news,
English, rage-mix voice, RSS feeds. This module is the same pipeline with the
brand pushed into a JSON file, so the same engine can run a hardware shop, a
dental clinic or a restaurant - and can be handed to a paying client.

What it does:
  1. picks the next product (rotating, learning from real engagement)
  2. writes a bilingual AR+FR caption with the LLM (Gemini, rotating key pool,
     OpenRouter fallback, fact-only template as the last resort)
  3. renders a branded 1200x630 card with Pillow (Arabic properly shaped)
  4. publishes it to the client Page, and the same post as a Story
  5. keeps a dedup + cadence state file so it never floods the page
  6. reads back real reactions/comments and scores each product

Usage:
    python brand_bot.py --brand brand.json --preview
    python brand_bot.py --brand brand.json --post --story
    python brand_bot.py --brand brand.json --scheduled     # GitHub Actions mode
    python brand_bot.py --brand brand.json --collect       # engagement sweep
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import cards          # noqa: E402
import fb_api         # noqa: E402


def _load_env() -> None:
    """Load credentials from wherever they happen to live.

    Order: this folder's .env -> the ethan-cole repo's .env (local runs reuse
    the keys that are already configured) -> nothing (CI injects real env).
    """
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    candidates = [
        HERE / ".env",
        Path(r"C:\Users\Abdelli\Documents\ethan-cole-fb-bot\.env"),
        Path(r"C:\Users\Abdelli\Documents\soq-alyoum-fb-bot\.env"),
    ]
    for c in candidates:
        if c.exists():
            load_dotenv(c, override=False)


_load_env()


# ----------------------------------------------------------------------
def load_brand(path: Path) -> dict:
    """Load a brand config.

    A path, or the literal string "env" to read BRAND_JSON from the
    environment. That is how a CLIENT config stays private: the repo holds
    the code (public, so the Actions minutes are free and the code doubles as
    a portfolio piece) while the client's products and prices live in a
    repository secret.
    """
    if str(path) == "env":
        raw = os.getenv("BRAND_JSON", "")
        if not raw:
            raise SystemExit("BRAND_JSON env var is empty")
        b = json.loads(raw)
        b["_dir"] = str(HERE)
    else:
        b = json.loads(path.read_text(encoding="utf-8"))
        b["_dir"] = str(path.parent)
    b.setdefault("_state", str(HERE / f"state_{b['page_id']}.json"))
    b.setdefault("_out", str(HERE / "out"))
    # asset paths are relative to the brand file, so the same config runs on
    # this machine and on a CI runner
    for k in ("logo", "cover"):
        v = b.get(k)
        if v and not os.path.isabs(v):
            b[k] = str((Path(b["_dir"]) / v).resolve())
    return b


def load_state(brand: dict) -> dict:
    p = Path(brand["_state"])
    state = None
    if p.exists():
        try:
            state = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            state = None
    if state is None:
        state = {"history": [], "engagement": {}, "day": {}}
    state.setdefault("history", [])
    state.setdefault("engagement", {})
    state.setdefault("day", {})
    _backfill_counters(state)
    return state


def _backfill_counters(state: dict) -> None:
    """Derive the daily counters from history.

    Without this, a state file written by an older version (or imported from
    another bot) has no counters, the cadence check thinks nothing was posted
    today, and the page gets flooded on the first run.
    """
    day = state.setdefault("day", {})
    for h in state.get("history", []):
        at = h.get("at")
        if not at:
            continue
        try:
            d = datetime.fromisoformat(at).astimezone(timezone.utc)
        except Exception:
            continue
        key = (d + timedelta(hours=1)).date().isoformat()
        rec = day.setdefault(key, {"posts": 0, "stories": 0})
        rec["posts"] = max(rec.get("posts", 0), 1)
        if h.get("story_id"):
            rec["stories"] = max(rec.get("stories", 0), 1)
        if not rec.get("last_post_at") or at > rec["last_post_at"]:
            rec["last_post_at"] = at


def save_state(brand: dict, state: dict) -> None:
    Path(brand["_state"]).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ----------------------------------------------------------------------
# caption
# ----------------------------------------------------------------------
SYSTEM = """You write Facebook posts for a small Algerian shop.
You write TWO versions of the same post: an Arabic one first, then a French one.

Rules, in order of importance:
1. NEVER invent a fact. Use only the product name, the specs and the price
   given to you. No discounts, no stock claims, no guarantees, no superlatives.
2. Arabic first. Modern standard Arabic, warm and short, the way a young
   Algerian shop owner writes. 2 lines max. No hashtags.
3. French second. Same facts, same length, natural Algerian French.
4. No "like", no "share", no "follow", no emoji spam. One emoji at most, at
   the front of the Arabic block.
5. Always state the price in DA and the delivery in both blocks.
6. End with ONE genuine question that a real buyer would answer, in French.
7. No markdown, no bullet symbols, no quotes around the product name.

Output format, exactly:
<ARABIC BLOCK>
<blank line>
<FRENCH BLOCK>
<blank line>
<one French question>
"""

FORBIDDEN = ["like", "share", "commentez", "follow us", "subscribe",
             "livraison gratuite", "garantie a vie", "meilleur prix",
             "numero 1", "offre limitee", "soldes", "promo"]


def build_user_prompt(brand: dict, p: dict) -> str:
    specs = "\n".join(f"- {s}" for s in p["specs"])
    ar_specs = "\n".join(f"- {s}" for s in p.get("ar_specs", p["specs"]))
    return f"""Shop: {brand['brand']}
Zone: {brand.get('zone', '')}
Delivery: {brand['delivery_fr']} / {brand['delivery_ar']}
CTA: {brand['cta_fr']} / {brand['cta_ar']}
Price note: {brand['price_note']}

PRODUCT (use these facts and nothing else):
Name: {p['name']}
Arabic name: {p['name_ar']}
Price: {p['price']} DA
Specs (French):
{specs}
Specs (Arabic):
{ar_specs}
Angle to lead with (do not repeat it verbatim):
{p.get('angle_fr', '')}
 / {p.get('angle_ar', '')}

Voice hints:
Arabic: {brand['voice']['ar']}
French: {brand['voice']['fr']}

Write the post now."""


def clean_caption(text: str, brand: dict) -> str:
    t = (text or "").strip()
    t = re.sub(r"^```.*?```$", "", t, flags=re.S)
    t = re.sub(r"[#*_`~]{1,}", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    for bad in brand.get("forbidden", FORBIDDEN):
        if bad.lower() in t.lower():
            t = re.sub(re.escape(bad), "", t, flags=re.I)
    lines = [ln.strip() for ln in t.split("\n")]
    return "\n".join([ln for ln in lines if ln])[:1200]


def fallback_caption(brand: dict, p: dict) -> str:
    """No invention: facts only. Used when every writer failed."""
    ar = "\n".join(p.get("ar_specs", p["specs"])[:3])
    fr = "\n".join(p["specs"][:3])
    price = f"{p['price']:,}".replace(",", " ") + " DA"
    q = brand.get("engagement_question", {}).get("fr", "")
    return (f"{p['name_ar']}\n{ar}\n\n"
            f"{p['name']}\n{fr}\n\n"
            f"{brand['cta_fr']} - {price}\n\n{q}")


def generate_caption(brand: dict, p: dict,
                     model: str = "gemini-2.5-flash") -> str:
    prompt = build_user_prompt(brand, p)
    for fn, mdl in (("gen_gemini", model),
                    ("gen_openrouter", "google/gemma-3-27b-it:free")):
        f = getattr(fb_api, fn, None)
        if not f:
            continue
        try:
            out = f(mdl, SYSTEM, prompt)
            if out and len(out.strip()) > 40:
                return clean_caption(out, brand)
        except Exception as e:  # noqa: BLE001
            print(f"  [llm:{fn}] {type(e).__name__}: {e}", file=sys.stderr)
    print("  [llm] every writer failed - using the fact-only fallback",
          file=sys.stderr)
    return fallback_caption(brand, p)


# ----------------------------------------------------------------------
# product rotation + learning
# ----------------------------------------------------------------------
def pick_product(brand: dict, state: dict) -> dict:
    prods = brand["products"]
    hist = state.get("history", [])
    last = {}
    for i, h in enumerate(hist):
        last[h.get("id")] = i
    eng = state.get("engagement", {})

    def key(p):
        # least recently posted first; among equally recent, best engagement
        recency = last.get(p["id"], -1)
        score = eng.get(p["id"], {}).get("score", 0)
        return (recency, -score)

    ordered = sorted(prods, key=key)
    if hist and ordered and ordered[0]["id"] == hist[-1].get("id"):
        ordered = ordered[1:] + ordered[:1]
    return ordered[0]


# ----------------------------------------------------------------------
# cadence
# ----------------------------------------------------------------------
def local_now() -> datetime:
    """Algeria is UTC+1 all year (no DST)."""
    return datetime.now(timezone.utc) + timedelta(hours=1)


def cadence_state(brand: dict, state: dict) -> dict:
    today = local_now().date().isoformat()
    day = state.setdefault("day", {})
    return {
        "today": today,
        "posts_today": day.get(today, {}).get("posts", 0),
        "stories_today": day.get(today, {}).get("stories", 0),
        "last_post_at": day.get(today, {}).get("last_post_at"),
    }


def may_post(brand: dict, state: dict) -> tuple[bool, str]:
    cad = brand.get("cadence", {})
    c = cadence_state(brand, state)
    now = local_now()
    if c["posts_today"] >= cad.get("posts_per_day", 2):
        return False, (f"already posted {c['posts_today']}x today "
                       f"(cap {cad.get('posts_per_day', 2)})")
    gap = cad.get("min_gap_minutes", 180)
    if c["last_post_at"]:
        last = datetime.fromisoformat(c["last_post_at"])
        mins = (now - last).total_seconds() / 60
        if mins < gap:
            return False, f"only {mins:.0f} min since the last post (gap {gap})"
    qh = cad.get("quiet_hours_local", [0, 6])
    if qh and qh[0] <= now.hour < qh[1]:
        return False, f"quiet hours ({qh[0]}:00-{qh[1]}:00)"
    ph = cad.get("post_hours_local", [9, 23])
    if ph and not (ph[0] <= now.hour < ph[1]):
        return False, f"outside posting hours {ph[0]}:00-{ph[1]}:00"
    return True, "ok"


# ----------------------------------------------------------------------
# engagement collection
# ----------------------------------------------------------------------
def collect_engagement(brand: dict, state: dict) -> int:
    """Read real reactions/comments back and score each product."""
    os.environ["FB_PAGE_ID"] = brand["page_id"]
    os.environ["FB_PAGE_ACCESS_TOKEN"] = fb_api.resolve_page_token(brand["page_id"])
    import requests
    eng = state.setdefault("engagement", {})
    n = 0
    for h in state.get("history", [])[-40:]:
        pid = h.get("post_id")
        if not pid:
            continue
        try:
            r = requests.get(
                f"https://graph.facebook.com/v26.0/{pid}",
                params={"fields": "comments.summary(total_count),reactions.summary(total_count)",
                        "access_token": os.environ["FB_PAGE_ACCESS_TOKEN"]},
                timeout=20)
            d = r.json() or {}
            if "error" in d:
                print(f"  engagement {pid}: {json.dumps(d)[:120]}", file=sys.stderr)
                continue
            c = (((d.get("comments") or {}).get("summary") or {}).get("total_count")) or 0
            rx = (((d.get("reactions") or {}).get("summary") or {}).get("total_count")) or 0
            e = eng.setdefault(h["id"], {"reactions": 0, "comments": 0, "score": 0})
            e["reactions"] = max(e.get("reactions", 0), rx)
            e["comments"] = max(e.get("comments", 0), c)
            e["score"] = e["reactions"] * 1.0 + e["comments"] * 3.0
            h["reactions"], h["comments"] = rx, c
            n += 1
        except Exception as e:  # noqa: BLE001
            print(f"  engagement {pid}: {type(e).__name__}", file=sys.stderr)
    save_state(brand, state)
    return n


# ----------------------------------------------------------------------
# publishing
# ----------------------------------------------------------------------
def publish(brand: dict, caption: str, card_path: Path, want_story: bool,
            dry_run: bool) -> dict:
    os.environ["FB_PAGE_ID"] = brand["page_id"]
    os.environ["FB_PAGE_ACCESS_TOKEN"] = fb_api.resolve_page_token(brand["page_id"])
    data = card_path.read_bytes()
    out = {"caption": caption, "card": str(card_path)}
    if dry_run:
        out["dry_run"] = True
        return out
    out["post_id"] = fb_api.publish_photo_to_facebook(data, "jpeg", caption)
    fb_api.log(f"published post {out['post_id']}")
    if want_story:
        try:
            story_bytes = None
            try:
                sp = cards.render_story(
                    brand, caption, None,
                    card_path.parent / (card_path.stem + "-story.jpg"))
                story_bytes = sp.read_bytes()
            except Exception as e:  # noqa: BLE001
                print(f"  story render failed: {e}", file=sys.stderr)
            out["story_id"] = fb_api.publish_story_from_photo(
                data, "jpeg", caption, story_bytes)
            fb_api.log(f"published story {out['story_id']}")
        except Exception as e:  # noqa: BLE001
            out["story_error"] = f"{type(e).__name__}: {e}"
            print(f"  story failed (non-fatal): {e}", file=sys.stderr)
    return out


# ----------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", default=str(HERE / "brand.json"))
    ap.add_argument("--post", action="store_true")
    ap.add_argument("--story", action="store_true")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--scheduled", action="store_true",
                    help="respect the cadence; skip if not time")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--collect", action="store_true",
                    help="read engagement back and score products")
    args = ap.parse_args()

    brand = load_brand(Path(args.brand))
    state = load_state(brand)

    if args.collect:
        n = collect_engagement(brand, state)
        print(f"collected engagement for {n} posts")
        for pid, e in sorted(state.get("engagement", {}).items(),
                             key=lambda kv: -kv[1].get("score", 0)):
            print(f"  {pid:<16} score {e.get('score', 0):>5}  "
                  f"(reactions {e.get('reactions')}, comments {e.get('comments')})")
        return 0

    prod = pick_product(brand, state)
    print(f"brand   : {brand['brand']}  (page {brand['page_id']})")
    print(f"product : {prod['name']}  -  {prod['price']} DA")

    if args.scheduled and not args.force:
        ok, why = may_post(brand, state)
        if not ok:
            print(f"SKIP: {why}")
            return 0

    caption = generate_caption(brand, prod)
    print("\n----- caption -----")
    print(caption)
    print("--------------------\n")

    outdir = Path(brand["_out"]) / brand["brand"].replace(" ", "_").lower()
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    card_path = outdir / f"{prod['id']}-{stamp}.jpg"
    cards.render_product_card(brand, prod, card_path, prod["price"])
    print(f"card    : {card_path}")

    if args.preview:
        return 0
    if not (args.post or args.dry_run or args.scheduled):
        return 0

    res = publish(brand, caption, card_path, args.story, args.dry_run)

    now_iso = datetime.now(timezone.utc).isoformat()
    state.setdefault("history", []).append({
        "id": prod["id"], "name": prod["name"], "at": now_iso,
        "post_id": res.get("post_id"), "story_id": res.get("story_id"),
    })
    state["history"] = state["history"][:][-80:]
    day = state.setdefault("day", {})
    today = local_now().date().isoformat()
    rec = day.setdefault(today, {"posts": 0, "stories": 0})
    rec["posts"] += 1
    if res.get("story_id"):
        rec["stories"] += 1
    rec["last_post_at"] = now_iso
    # keep 30 days of counters
    cutoff = (local_now().date() - timedelta(days=30)).isoformat()
    state["day"] = {k: v for k, v in day.items() if k >= cutoff}
    save_state(brand, state)
    print(f"\nsaved state -> {brand['_state']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
