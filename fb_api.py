"""
eight-dollar / bot / fb_api.py
------------------------------
The Facebook + LLM layer, extracted from the proven ethan-cole pipeline so
the brand bot runs anywhere (this machine, a GitHub Actions runner, a VPS)
with no dependency on the news bot.

Extracted, not rewritten: the Gemini call (rotating key pool, 429 failover),
the OpenRouter fallback, the photo publish, and the two-step story publish
(upload unpublished -> /photo_stories) all behave exactly as they did in
post_bot.py.

One addition over the original: `mint_page_token()`. The original assumed a
long-lived Page token already existed. That assumption broke in October 2026:
a Page token whose profile lost admin rights returns error 190 and every
publish fails, and no amount of re-minting fixes it. The fix is to mint a FRESH
Page token from a user token that actually administers the page, which is what
this does.
"""
from __future__ import annotations

import io
import json
import os
from datetime import datetime, timezone

import requests

FB_API_VERSION = "v26.0"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ----------------------------------------------------------------------
# LLM
# ----------------------------------------------------------------------
def _gemini_keys() -> list:
    keys = []
    multi = os.getenv("GEMINI_API_KEYS", "")
    if multi:
        keys += [k.strip() for k in multi.split(",") if k.strip()]
    single = os.getenv("GEMINI_API_KEY", "").strip()
    if single:
        keys.append(single)
    return list(dict.fromkeys(keys))


def gen_gemini(model: str, system: str, user: str) -> str:
    """Rotate the key pool by time slot, fail over on quota errors."""
    keys = _gemini_keys()
    if not keys:
        raise RuntimeError("No Gemini key set")
    now = datetime.now(timezone.utc)
    slot = now.timetuple().tm_yday * 72 + now.hour * 3 + now.minute // 20
    start = slot % len(keys)
    ordered = keys[start:] + keys[:start]
    last_err = "no keys tried"
    for key in ordered:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
                f":generateContent?key={key}",
                headers={"Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": system + "\n\n" + user}]}],
                      "generationConfig": {"maxOutputTokens": 1000,
                                           "temperature": 0.5,
                                           "thinkingConfig": {"thinkingBudget": 0}}},
                timeout=90)
            d = r.json()
            if d.get("error"):
                raise RuntimeError(f"API error: {str(d['error'])[:120]}")
            return d["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as ex:  # noqa: BLE001
            last_err = f"key ...{key[-6:]}: {ex}"[:160]
            continue
    raise RuntimeError(f"Gemini {model} failed on all {len(keys)} keys: {last_err}")


def gen_openrouter(model: str, system: str, user: str) -> str:
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("no OPENROUTER_API_KEY")
    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}",
                 "HTTP-Referer": "https://github.com/brand-pages-bot",
                 "X-Title": "brand-pages-bot",
                 "Content-Type": "application/json"},
        json={"model": model, "max_tokens": 500, "temperature": 0.5,
              "reasoning": {"exclude": True},
              "messages": [{"role": "system", "content": system},
                           {"role": "user", "content": user}]},
        timeout=90)
    d = r.json()
    if r.status_code != 200:
        raise RuntimeError(f"OpenRouter {model} HTTP {r.status_code}: "
                           f"{json.dumps(d)[:200]}")
    try:
        return d["choices"][0]["message"]["content"].strip()
    except Exception as ex:  # noqa: BLE001
        raise RuntimeError(f"OpenRouter {model} parse error: {ex}")


# ----------------------------------------------------------------------
# tokens
# ----------------------------------------------------------------------
def token_is_valid(page_id: str, token: str) -> bool:
    if not token:
        return False
    try:
        r = requests.get(f"https://graph.facebook.com/{FB_API_VERSION}/{page_id}",
                         params={"fields": "id,name", "access_token": token},
                         timeout=20)
        return "name" in (r.json() or {})
    except Exception:
        return False


def mint_page_token(page_id: str) -> str:
    """Fresh Page token from a user token that actually administers the page.

    This is the fix for error 190 ("This Page access token belongs to a Page
    that is not accessible"). A stale Page token cannot be repaired by
    refreshing itself - it has to be re-minted from a valid user token.
    """
    ut = os.getenv("FB_USER_TOKEN", "").strip()
    if not ut:
        raise RuntimeError("no FB_USER_TOKEN - a human login is required")
    r = requests.get("https://graph.facebook.com/v26.0/me/accounts",
                     params={"access_token": ut}, timeout=25)
    d = r.json() or {}
    if "error" in d:
        raise RuntimeError(f"/me/accounts failed: {json.dumps(d)[:200]}")
    managed = []
    for pg in d.get("data", []):
        managed.append(str(pg.get("id")))
        if str(pg.get("id")) == str(page_id) and pg.get("access_token"):
            return pg["access_token"]
    raise RuntimeError(
        f"FB_USER_TOKEN does not administer page {page_id}. It manages "
        f"{len(managed)} page(s): {', '.join(managed) or 'none'}. "
        f"Re-seed FB_USER_TOKEN from the Facebook profile that owns the page.")


def resolve_page_token(page_id: str) -> str:
    """Use the configured token if it works, else mint a fresh one."""
    tok = os.getenv("FB_PAGE_ACCESS_TOKEN", "").strip()
    if tok and token_is_valid(page_id, tok):
        return tok
    log("configured page token is not usable - minting a fresh one")
    return mint_page_token(page_id)


# ----------------------------------------------------------------------
# publishing
# ----------------------------------------------------------------------
def _normalize_upload(data: bytes, ext: str = "jpeg", w: int = 1200,
                      h: int = 630) -> tuple[bytes, str]:
    """Force any outgoing photo to exactly w x h so the page never mixes
    shapes (a portrait next to a 1.91:1 card renders as if it was squeezed)."""
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return data, ext
    if im.size != (w, h):
        scale = max(w / im.size[0], h / im.size[1])
        im = im.resize((int(im.size[0] * scale) + 1,
                        int(im.size[1] * scale) + 1), Image.LANCZOS)
        x = (im.width - w) // 2
        y = (im.height - h) // 2
        im = im.crop((x, y, x + w, y + h))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=95, optimize=True, progressive=True)
    return buf.getvalue(), "jpeg"


def publish_photo_to_facebook(image_bytes: bytes, ext: str, caption: str) -> str:
    page_id = os.getenv("FB_PAGE_ID", "").strip()
    token = os.getenv("FB_PAGE_ACCESS_TOKEN", "").strip()
    if not page_id or not token:
        raise RuntimeError("FB_PAGE_ID / FB_PAGE_ACCESS_TOKEN not set")
    image_bytes, ext = _normalize_upload(image_bytes, ext)
    url = f"https://graph.facebook.com/{FB_API_VERSION}/{page_id}/photos"
    files = {"source": (f"photo.{ext}", image_bytes, f"image/{ext}")}
    data = {"caption": caption, "access_token": token}
    r = requests.post(url, files=files, data=data, timeout=60)
    try:
        resp = r.json()
    except Exception:
        raise RuntimeError(f"Facebook non-JSON response {r.status_code}: "
                           f"{r.text[:300]}")
    if r.status_code != 200 or "error" in resp:
        raise RuntimeError(f"Facebook error: {json.dumps(resp)[:500]}")
    return resp.get("post_id") or resp.get("id", "")


def publish_story_from_photo(image_bytes: bytes, ext: str, caption: str = "",
                              story_bytes: bytes | None = None) -> str:
    """Publish the POST as a 24h Page Story: upload unpublished, then publish
    via /photo_stories. Never fails the run - callers wrap it."""
    page_id = os.getenv("FB_PAGE_ID", "").strip()
    token = os.getenv("FB_PAGE_ACCESS_TOKEN", "").strip()
    if not page_id or not token:
        raise RuntimeError("FB_PAGE_ID / FB_PAGE_ACCESS_TOKEN not set")
    creative = story_bytes or image_bytes
    r = requests.post(
        f"https://graph.facebook.com/{FB_API_VERSION}/{page_id}/photos",
        files={"source": ("story.jpeg", creative, "image/jpeg")},
        data={"published": "false", "access_token": token}, timeout=60)
    up = r.json()
    pid = up.get("id")
    if r.status_code != 200 or not pid:
        raise RuntimeError(f"story upload failed: {json.dumps(up)[:200]}")
    r2 = requests.post(
        f"https://graph.facebook.com/{FB_API_VERSION}/{page_id}/photo_stories",
        json={"photo_id": pid, "access_token": token}, timeout=20)
    st = r2.json()
    if r2.status_code != 200 or not st.get("success"):
        raise RuntimeError(f"story publish failed: {json.dumps(st)[:200]}")
    return st.get("post_id", "")


def publish_to_facebook(message: str) -> str:
    page_id = os.getenv("FB_PAGE_ID", "").strip()
    token = os.getenv("FB_PAGE_ACCESS_TOKEN", "").strip()
    if not page_id or not token:
        raise RuntimeError("FB_PAGE_ID / FB_PAGE_ACCESS_TOKEN not set")
    url = f"https://graph.facebook.com/{FB_API_VERSION}/{page_id}/feed"
    r = requests.post(url, json={"message": message, "access_token": token},
                      timeout=20)
    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"Facebook non-JSON response {r.status_code}: "
                           f"{r.text[:300]}")
    if r.status_code != 200 or "error" in data:
        raise RuntimeError(f"Facebook error: {json.dumps(data)[:500]}")
    return data.get("id", "")
