"""
eight-dollar / bot / cards.py
-----------------------------
Branded social cards for a client Facebook page.

Why PIL and not HTML: the GitHub Actions runner has no browser, and Pillow is
already a dependency of the posting bot. Arabic needs explicit shaping
(arabic-reshaper + python-bidi) because Pillow draws glyphs, not words.

Card spec (1200x630, the Facebook feed ratio):
    black ground, blue accent, logo, product name (FR + AR), three specs,
    the price in DA, and the delivery line.
"""
from __future__ import annotations

import io
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    HAVE_ARABIC = True
except Exception:          # pragma: no cover
    HAVE_ARABIC = False


def ar(text: str) -> str:
    """Shape + reorder Arabic so Pillow draws it right-to-left correctly."""
    if not HAVE_ARABIC or not text:
        return text
    try:
        return get_display(arabic_reshaper.reshape(text))
    except Exception:
        return text


LATIN_BOLD = r"C:\Windows\Fonts\arialbd.ttf"
LATIN = r"C:\Windows\Fonts\arial.ttf"
ARABIC_BOLD = r"C:\Windows\Fonts\tahomabd.ttf"
ARABIC = r"C:\Windows\Fonts\tahoma.ttf"


def font(size: int, bold: bool = True, arabic: bool = False):
    paths = []
    if arabic:
        paths = [ARABIC_BOLD if bold else ARABIC]
    else:
        paths = [LATIN_BOLD if bold else LATIN]
    paths += [LATIN_BOLD if bold else LATIN, r"C:\Windows\Fonts\segoeuib.ttf"]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def text_size(draw, s, f):
    box = draw.textbbox((0, 0), s, font=f)
    return box[2] - box[0], box[3] - box[1]


def fit_text(draw, s, max_w, start, bold=True, arabic=False, min_size=18):
    """Shrink until it fits one line."""
    size = start
    while size > min_size:
        f = font(size, bold, arabic)
        w, _ = text_size(draw, s, f)
        if w <= max_w:
            return f
        size -= 2
    return font(min_size, bold, arabic)


def wrap(draw, s, f, max_w):
    words, lines, cur = s.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if text_size(draw, trial, f)[0] <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def draw_right(draw, xy, s, f, fill):
    """Draw right-aligned (x is the RIGHT edge)."""
    w, _ = text_size(draw, s, f)
    draw.text((xy[0] - w, xy[1]), s, font=f, fill=fill)


def draw_center(draw, cx, y, s, f, fill):
    w, _ = text_size(draw, s, f)
    draw.text((cx - w // 2, y), s, font=f, fill=fill)


def fit_lines(draw, s, max_w, start, bold=True, arabic=False, max_lines=3):
    size = start
    while size > 16:
        f = font(size, bold, arabic)
        lines = wrap(draw, s, f, max_w)[:max_lines]
        if len(wrap(draw, s, f, max_w)) <= max_lines:
            return f, lines
        size -= 2
    f = font(16, bold, arabic)
    return f, wrap(draw, s, f, max_w)[:max_lines]


# ----------------------------------------------------------------------
def render_product_card(brand: dict, product: dict, out_path: Path,
                        price_dzd: int, tag: str = "") -> Path:
    """1200x630 feed card for one product."""
    W, H = 1200, 630
    bg = brand.get("bg", "#08080A")
    accent = brand.get("accent", "#1E88FF")
    white = brand.get("white", "#F5F7FA")
    muted = brand.get("muted", "#8A94A6")

    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)

    # soft accent glow, top-right
    glow = Image.new("RGB", (W, H), bg)
    gd = ImageDraw.Draw(glow)
    gd.ellipse((W - 420, -300, W + 260, 380), fill=accent)
    glow = glow.filter(ImageFilter.GaussianBlur(160))
    img = Image.blend(img, glow, 0.16)
    d = ImageDraw.Draw(img)

    # accent rule at the very top
    d.rectangle((0, 0, W, 7), fill=accent)

    # ---- logo (mark + wordmark), top-left
    logo_path = brand.get("logo")
    if logo_path and os.path.exists(logo_path):
        try:
            lg = Image.open(logo_path).convert("RGB")
            # crop the black margin so the mark fills its box
            g = lg.convert("L")
            bb = g.point(lambda v: 255 if v > 28 else 0).getbbox()
            if bb:
                pad = 6
                bb = (max(0, bb[0] - pad), max(0, bb[1] - pad),
                      min(lg.width, bb[2] + pad), min(lg.height, bb[3] + pad))
                lg = lg.crop(bb)
            ratio = 210 / lg.width
            lg = lg.resize((210, max(1, int(lg.height * ratio))), Image.LANCZOS)
            img.paste(lg, (54, 40))
            d = ImageDraw.Draw(img)
        except Exception:
            pass

    # ---- product name (Latin)
    name = product.get("name", "")
    f_name = fit_text(d, name, 980, 46, bold=True)
    d.text((54, 190), name, font=f_name, fill=white)

    # ---- product name (Arabic), right aligned
    name_ar = product.get("name_ar", "")
    if name_ar:
        shaped = ar(name_ar)
        f_ar = fit_text(d, shaped, 1000, 32, bold=True, arabic=True)
        w, _ = text_size(d, shaped, f_ar)
        # measure twice: the shaped string can report a narrower box than the
        # glyphs actually occupy, so leave a safety margin on the right edge
        x = max(24, W - 72 - int(w * 1.04))
        d.text((x, 248), shaped, font=f_ar, fill=accent)

    # ---- specs
    y = 300
    for spec in (product.get("specs") or [])[:3]:
        f = font(25, bold=False)
        d.ellipse((56, y + 11, 66, y + 21), fill=accent)
        d.text((82, y), spec, font=f, fill=muted)
        y += 40

    # ---- price block, bottom-left
    f_price = font(66, bold=True)
    price_txt = f"{price_dzd:,}".replace(",", " ") + " DA"
    d.text((54, 452), price_txt, font=f_price, fill=accent)

    f_note = font(22, bold=False)
    note = product.get("price_note", brand.get("price_note", "Neuf, garantie, prix ferme"))
    d.text((56, 530), note, font=f_note, fill=muted)

    # ---- bottom bar: delivery + CTA, both languages
    d.rectangle((0, H - 62, W, H), fill="#0F1116")
    d.rectangle((0, H - 62, W, H - 59), fill=accent)
    f_bar = font(21, bold=True)
    delivery = f"{brand.get('delivery_fr', '')}  -  {ar(brand.get('delivery_ar', ''))}"
    d.text((54, H - 47), delivery, font=f_bar, fill=white)
    f_cta = font(21, bold=True)
    cta = f"{brand.get('cta_fr', '')}  -  {ar(brand.get('cta_ar', ''))}"
    draw_right(d, (W - 54, H - 46), cta, f_cta, accent)

    if tag:
        f_tag2 = font(18, bold=True)
        d.text((56, 40), tag, font=f_tag2, fill=accent)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "JPEG", quality=95, optimize=True, progressive=True)
    return out_path


def render_story(brand: dict, caption: str, photo: Path | None, out_path: Path,
                 headline: str = "") -> Path:
    """1080x1920 story creative."""
    W, H = 1080, 1920
    accent = brand.get("accent", "#1E88FF")
    img = Image.new("RGB", (W, H), brand.get("bg", "#08080A"))
    d = ImageDraw.Draw(img)

    if photo and Path(photo).exists():
        try:
            p = Image.open(photo).convert("RGB")
            scale = max(W / p.width, 760 / p.height)
            p = p.resize((int(p.width * scale) + 1, int(p.height * scale) + 1),
                         Image.LANCZOS)
            bg = p.filter(ImageFilter.GaussianBlur(30)).point(lambda v: int(v * 0.4))
            img.paste(bg.crop(((bg.width - W) // 2, 0, (bg.width - W) // 2 + W, 760)),
                      (0, 0))
            fg = p.copy()
            fg.thumbnail((W - 80, 720), Image.LANCZOS)
            img.paste(fg, ((W - fg.width) // 2, 40 + (720 - fg.height) // 2))
        except Exception:
            pass

    d = ImageDraw.Draw(img)
    y = 840
    if headline:
        f = fit_text(d, headline, 960, 58, bold=True)
        draw_center(d, W // 2, y, headline, f, "#FFFFFF")
        y += 78

    body = [ln for ln in caption.split("\n") if ln.strip()][:7]
    for ln in body:
        is_ar = any("\u0600" <= ch <= "\u06ff" for ch in ln)
        s = ar(ln) if is_ar else ln
        f = font(30, bold=False, arabic=is_ar)
        w, _ = text_size(d, s, f)
        if w > 960:
            f, lines = fit_lines(d, s, 960, 30, bold=False, arabic=is_ar, max_lines=2)
            for l2 in lines:
                draw_center(d, W // 2, y, l2, f, "#C9D2E0")
                y += 44
            continue
        draw_center(d, W // 2, y, s, f, "#C9D2E0")
        y += 46

    d.rectangle((0, H - 120, W, H), fill=accent)
    f_cta = font(34, bold=True)
    draw_center(d, W // 2, H - 84, brand.get("cta_fr", "Commande : message privé"),
                f_cta, "#FFFFFF")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "JPEG", quality=92, optimize=True)
    return out_path
