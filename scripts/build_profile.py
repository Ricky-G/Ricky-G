#!/usr/bin/env python3
"""
build_profile.py  ·  renders every image block of the Ricky-G GitHub profile README.

    python scripts/build_profile.py --offline   # use data/*.json only (no network)
    python scripts/build_profile.py --github-cli  # refresh using an existing read-only gh login

Writes both themes of every block:
    assets/*.svg             static blocks (hero, links, labels, stack)
    assets/generated/*.svg   data-driven blocks (posts, tiles with live counts, pulse, sign-off)
and rewrites the <!-- WRITING:START --> ... <!-- WRITING:END --> block in README.md.

Needs Python 3.14 and the pinned Pillow/timezone dependencies in requirements.txt.
All colours live in THEMES, so a rebrand is a token change.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import random
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    NZ = ZoneInfo("Pacific/Auckland")
except ZoneInfoNotFoundError as exc:
    raise RuntimeError("Install requirements.txt: Pacific/Auckland timezone data is required.") from exc

try:
    from PIL import ImageFont
except ImportError as exc:
    raise RuntimeError("Install requirements.txt: Pillow is required to validate and render images.") from exc

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
GEN = ASSETS / "generated"
DATA = ROOT / "data"

GITHUB_USER = "Ricky-G"
FEED_URL = "https://clouddev.blog/latest-posts.json"
NEW_DAYS = 14

# --------------------------------------------------------------------------- tokens
THEMES: dict[str, dict[str, str]] = {
    "dark": {
        "card0": "#121a37", "card1": "#0b1128", "cardline": "#252f55",
        "chip": "#161e3d", "chip_o": "0.93", "chipline": "#2c3764",
        "ink": "#eef1ff", "ink2": "#bdc5e4", "ink3": "#8a93b7", "ink4": "#5e6790",
        "green": "#34d399", "teal": "#2dd4bf", "violet": "#a78bfa", "magenta": "#e879f9",
        "rose": "#fb7185", "gold": "#fbbf24", "blue": "#60a5fa",
        "sky0": "#050918", "sky1": "#0a1234", "sky2": "#171246",
        "aurA": "#34d399", "aurA_o": "0.46", "aurB": "#8b5cf6", "aurB_o": "0.46",
        "aurC": "#e879f9", "aurC_o": "0.30", "ray_o": "0.55",
        "star": "#ffffff", "star_o": "1",
        "land": "#03050d", "land_o": "0.92", "far_o": "0.55", "lamp": "#ffd27a", "lamp_o": "1",
        "dot": "#9aa6d6", "dot_o": "0.16",
        "aura": "#8b5cf6", "aura_o": "0.45",
        "ring_in": "#ffffff", "ring_in_o": "0.14",
        "term": "#080d22", "termline": "#1f2850", "termink": "#d3daf7", "termdim": "#6f79a3",
        "shadow": "#000000", "shadow_o": "0.45",
        "track": "#1b2448", "glow_o": "0.30",
    },
    "light": {
        "card0": "#ffffff", "card1": "#f7f8fe", "cardline": "#e2e6f3",
        "chip": "#ffffff", "chip_o": "0.92", "chipline": "#dfe3f2",
        "ink": "#0b1020", "ink2": "#394262", "ink3": "#666f8e", "ink4": "#9aa1ba",
        "green": "#059669", "teal": "#0d9488", "violet": "#7c3aed", "magenta": "#c026d3",
        "rose": "#e11d48", "gold": "#d97706", "blue": "#2563eb",
        "sky0": "#fbfcff", "sky1": "#f1f4ff", "sky2": "#f7f0ff",
        "aurA": "#6ee7b7", "aurA_o": "0.55", "aurB": "#c4b5fd", "aurB_o": "0.60",
        "aurC": "#f5b4fb", "aurC_o": "0.48", "ray_o": "0.28",
        "star": "#7c3aed", "star_o": "0",
        "land": "#27306b", "land_o": "0.11", "far_o": "0.07", "lamp": "#d97706", "lamp_o": "0",
        "dot": "#3a4470", "dot_o": "0.10",
        "aura": "#c4b5fd", "aura_o": "0.60",
        "ring_in": "#ffffff", "ring_in_o": "0.75",
        "term": "#0e1430", "termline": "#262f5c", "termink": "#d9dff8", "termdim": "#7d87b1",
        "shadow": "#1e2a5a", "shadow_o": "0.13",
        "track": "#eceffa", "glow_o": "0.22",
    },
}
# Terminal accents stay bright in both themes (the terminal is always dark).
TERM = {"tgreen": "#34d399", "trose": "#fb7185", "tviolet": "#a78bfa", "tgold": "#fbbf24"}
for _t in THEMES.values():
    _t.update(TERM)

# Covers are artwork, so they do not change with the theme.
COVER = {"bg0": "#0a0f2c", "bg1": "#1e1456", "a": "#34d399", "b": "#a78bfa", "c": "#e879f9", "ink": "#f4f6ff"}

FONT_UI = ("'Segoe UI Variable Display','Segoe UI',system-ui,-apple-system,BlinkMacSystemFont,"
           "'Helvetica Neue',Arial,sans-serif")
FONT_MONO = "'Cascadia Code','JetBrains Mono','SF Mono',ui-monospace,Menlo,Consolas,'Liberation Mono',monospace"


def css_tokens() -> dict[str, str]:
    """Token map that points at CSS variables (used by the interactive concept page)."""
    return {k: f"var(--t-{k})" for k in THEMES["dark"]}


# --------------------------------------------------------------------------- text
_FONT_FILES = {
    "regular": ["fonts/measure-regular.ttf"],
    "bold": ["fonts/measure-bold.ttf"],
    "mono": ["fonts/measure-mono.ttf"],
}
_SLACK = {"regular": 1.12, "bold": 1.12, "mono": 1.02}
_fonts: dict = {}


def _font(kind: str, size: float):
    key = (kind, int(round(size * 4)))
    if key in _fonts:
        return _fonts[key]
    f = None
    if ImageFont is not None:
        for p in _FONT_FILES[kind]:
            pp = Path(p) if os.path.isabs(p) else ROOT / p
            if pp.exists():
                f = ImageFont.truetype(str(pp), size=key[1])
                break
    if f is None and kind != "mono":
        # Pillow's bundled font keeps line breaks stable between Windows and Actions.
        f = ImageFont.load_default(size=key[1])
    _fonts[key] = f
    return f


def tw(text: str, size: float, kind: str = "regular", ls: float = 0.0) -> float:
    """Approximate rendered width in SVG units (conservative)."""
    f = _font(kind, size)
    if f is None:
        w = len(text) * size * (0.6 if kind == "mono" else 0.57)
    else:
        w = f.getlength(text) / 4.0
    return w * _SLACK[kind] + ls * max(len(text) - 1, 0)


def wrap(text: str, size: float, kind: str, max_w: float, max_lines: int) -> list[str]:
    if size <= 0 or max_w <= 0 or max_lines < 1:
        raise ValueError("Text wrapping needs positive dimensions.")
    words = []
    for word in text.split():
        part = ""
        for char in word:
            if part and tw(part + char, size, kind) > max_w:
                words.append(part)
                part = ""
            part += char
        if part:
            words.append(part)
    lines, cur = [], ""
    for w in words:
        t = f"{cur} {w}".strip()
        if tw(t, size, kind) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and tw(last + "…", size, kind) > max_w:
            last = last[:-1].rstrip()
        lines[-1] = last.rstrip(",;:.") + "…"
    return lines


def esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def tx(x, y, s, size, fill, weight=400, *, mono=False, anchor="start", ls=None, cls="", pre=False, extra="", role=""):
    st = f"font-size:{size}px;fill:{fill};font-weight:{weight}"
    if ls is not None:
        st += f";letter-spacing:{ls}px"
    c = " ".join(v for v in ("m" if mono else "", cls) if v)
    a = f' text-anchor="{anchor}"' if anchor != "start" else ""
    sp = ' xml:space="preserve"' if pre else ""
    cc = f' class="{c}"' if c else ""
    rr = f' data-font-role="{esc(role)}"' if role else ""
    return f'<text x="{x:.1f}" y="{y:.1f}"{a}{cc}{sp}{rr} style="{st}"{extra}>{esc(s)}</text>'


# --------------------------------------------------------------------------- shared svg bits
def svg_open(P: str, w: int, h: int, title: str, css: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'id="motion" class="{P}" role="img" aria-label="{esc(title)}"><title>{esc(title)}</title><style>{css}</style>')


# The parent picture opts into #motion; an unqualified image stays still.
BASE_CSS = """
.%P text{font-family:%UI}
.%P .m{font-family:%MONO}
@keyframes %P-tw{0%,100%{opacity:.18}50%{opacity:1}}
@keyframes %P-float{0%,100%{transform:translateY(0)}50%{transform:translateY(-7px)}}
@keyframes %P-ping{0%{transform:scale(1);opacity:.8}100%{transform:scale(3);opacity:0}}
@keyframes %P-spin{to{transform:rotate(360deg)}}
@keyframes %P-spinr{to{transform:rotate(-360deg)}}
@keyframes %P-blink{0%,55%{opacity:1}56%,100%{opacity:.1}}
@keyframes %P-in{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@keyframes %P-grow{from{transform:scaleX(0)}to{transform:scaleX(1)}}
@keyframes %P-travel{0%{transform:translateX(0);opacity:0}8%{opacity:1}85%{opacity:1}100%{transform:translateX(var(--d,1000px));opacity:0}}
.%P .t1{animation:%P-tw 3.4s ease-in-out infinite}
.%P .t2{animation:%P-tw 4.8s ease-in-out infinite 1.1s}
.%P .t3{animation:%P-tw 6.2s ease-in-out infinite 2.3s}
.%P .ping{animation:%P-ping 2.6s ease-out infinite}
.%P .blink{animation:%P-blink 1.1s steps(1) infinite}
.%P .fl1{animation:%P-float 6s ease-in-out infinite}
.%P .fl2{animation:%P-float 7s ease-in-out infinite 1.4s}
.%P .fl3{animation:%P-float 6.5s ease-in-out infinite .7s}
.%P .in{animation:%P-in .7s cubic-bezier(.2,.8,.2,1) both}
.%P .grow{transform-box:fill-box;transform-origin:0 50%;animation:%P-grow 1.4s cubic-bezier(.2,.8,.2,1) .5s both}
.%P:not(:target) *{animation:none!important}
@media (prefers-reduced-motion:reduce){.%P:not(:target) *{animation:none!important}}
"""


def css(P: str, extra: str = "") -> str:
    return (BASE_CSS + extra).replace("%P", P).replace("%UI", FONT_UI).replace("%MONO", FONT_MONO)


def stops(T):
    return (f'<stop offset="0" style="stop-color:{T["green"]}"/><stop offset=".33" style="stop-color:{T["teal"]}"/>'
            f'<stop offset=".7" style="stop-color:{T["violet"]}"/><stop offset="1" style="stop-color:{T["magenta"]}"/>')


def defs_common(T, P):
    s = stops(T)
    return (
        f'<linearGradient id="{P}-brand" x1="0" y1="0" x2="1" y2="0">{s}</linearGradient>'
        f'<linearGradient id="{P}-shim" x1="0" y1="0" x2="1" y2="0" spreadMethod="reflect">{s}</linearGradient>'
        f'<linearGradient id="{P}-card" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:{T["card0"]}"/>'
        f'<stop offset="1" style="stop-color:{T["card1"]}"/></linearGradient>'
        f'<filter id="{P}-shadow" x="-20%" y="-40%" width="140%" height="200%">'
        f'<feGaussianBlur in="SourceAlpha" stdDeviation="7"/><feOffset dy="6" result="o"/>'
        f'<feFlood style="flood-color:{T["shadow"]};flood-opacity:{T["shadow_o"]}"/>'
        f'<feComposite in2="o" operator="in"/><feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge></filter>'
        f'<filter id="{P}-blur" x="-60%" y="-60%" width="220%" height="220%"><feGaussianBlur stdDeviation="40"/></filter>'
        f'<filter id="{P}-soft" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="7"/></filter>'
    )


def brand_u(T, P, gid, x1, y1, x2, y2):
    return (f'<linearGradient id="{P}-{gid}" gradientUnits="userSpaceOnUse" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}">'
            f'{stops(T)}</linearGradient>')


def card(T, P, x, y, w, h, r=24, stroke=None, sw=1.4):
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" '
            f'style="fill:url(#{P}-card);stroke:{stroke or T["cardline"]};stroke-width:{sw}"/>')


def glow(T, P, cx, cy, r, key="aurB", op="glow_o"):
    return (f'<circle cx="{cx}" cy="{cy}" r="{r}" filter="url(#{P}-blur)" '
            f'style="fill:{T[key]};opacity:{T[op]}"/>')


def kicker(T, x, y, text, dot_key="green", pulse=False, P="", size=21):
    s = f'<circle cx="{x + 5}" cy="{y - 4.5}" r="4.4" style="fill:{T[dot_key]}"/>'
    if pulse:
        s += (f'<circle class="ping" cx="{x + 5}" cy="{y - 4.5}" r="4.4" style="fill:none;stroke:{T[dot_key]};'
              f'stroke-width:1.5;transform-origin:{x + 5}px {y - 4.5}px;transform-box:view-box"/>')
    return s + tx(x + 20, y + 1, text, size, T["ink3"], 700, mono=True, ls=1.2, role="kicker")


def arrow(x, y, color, size=11, sw=2.1):
    return (f'<path d="M{x} {y + size}L{x + size} {y}M{x + 3.5} {y}H{x + size}V{y + size - 3.5}" '
            f'style="fill:none;stroke:{color};stroke-width:{sw};stroke-linecap:round;stroke-linejoin:round"/>')


def chip_box(T, x, y, label, *, dot=None, size=13.5, h=30, icon="", pad=13, mono=True, fill=None):
    w = pad + (16 if (dot or icon) else 0) + tw(label, size, "mono" if mono else "regular") + pad
    s = (f'<rect x="{x:.1f}" y="{y}" width="{w:.1f}" height="{h}" rx="{h / 2}" '
         f'style="fill:{fill or T["chip"]};stroke:{T["chipline"]};stroke-width:1.1"/>')
    cx = x + pad + 5
    if dot:
        s += f'<circle cx="{cx:.1f}" cy="{y + h / 2}" r="4.6" style="fill:{dot}"/>'
    s += icon.replace("%X", f"{cx:.1f}").replace("%Y", f"{y + h / 2:.1f}")
    s += tx(x + pad + (16 if (dot or icon) else 0), y + h / 2 + size * 0.36, label, size, T["ink2"],
            500, mono=mono, role="chip-body")
    return s, w


STAR_ICON = ('<path d="M%X %Y m0 -6.2 l1.9 3.9 4.3 .6 -3.1 3 .7 4.3 -3.8 -2 -3.8 2 .7 -4.3 -3.1 -3 4.3 -.6z" '
             'style="fill:%C"/>')
FORK_ICON = ('<g style="fill:none;stroke:%C;stroke-width:1.6;stroke-linecap:round">'
             '<circle cx="%X" cy="%Y" r="0" transform="translate(-3.5 -4.6)"/>'
             '<path d="M%X %Y m-3.6 -4.2 v1.6 a2.4 2.4 0 0 0 2.4 2.4 h2.4 a2.4 2.4 0 0 0 2.4 -2.4 v-1.6 '
             'M%X %Y v4.8"/></g>')


# --------------------------------------------------------------------------- hero
BUILDINGS = [(718, 24, 26), (744, 16, 40), (762, 28, 33), (792, 20, 52), (814, 32, 38), (848, 18, 61),
             (868, 26, 46), (896, 22, 70), (920, 34, 43), (956, 20, 57), (978, 28, 49), (1008, 16, 36),
             (1026, 30, 64), (1058, 18, 44), (1102, 26, 55), (1130, 22, 40), (1154, 32, 31)]


def skyline(T, P):
    """Auckland at night: Harbour Bridge, Rangitoto, the CBD and the Sky Tower. Local box 1200 x 140."""
    g = [f'<path d="M352 140C430 131 482 106 536 98Q561 94 586 98C640 106 694 131 770 140Z" '
         f'style="fill:{T["land"]};opacity:{T["far_o"]}"/>']
    b = [f'<path d="M0 117H338" style="fill:none;stroke:{T["land"]};stroke-width:3.4"/>']
    for px, pw in ((28, 4), (70, 4), (112, 4), (160, 7), (318, 7)):
        b.append(f'<rect x="{px - pw / 2:.1f}" y="117" width="{pw}" height="23" style="fill:{T["land"]}"/>')

    def yq(x):
        t = (x - 160) / 158.0
        return (1 - t) ** 2 * 117 + 2 * (1 - t) * t * 49 + t * t * 117

    b.append(f'<path d="M160 117Q239 49 318 117" style="fill:none;stroke:{T["land"]};stroke-width:3"/>')
    n, d = 12, []
    xs = [160 + 158 * i / n for i in range(n + 1)]
    for i, x in enumerate(xs):
        d.append(f"M{x:.1f} {yq(x):.1f}V117")
        if i < n:
            x2 = xs[i + 1]
            d.append(f"M{x:.1f} {yq(x):.1f}L{x2:.1f} 117" if i % 2 == 0 else f"M{x:.1f} 117L{x2:.1f} {yq(x2):.1f}")
    b.append(f'<path d="{"".join(d)}" style="fill:none;stroke:{T["land"]};stroke-width:1.5"/>')
    g.append(f'<g style="opacity:{T["land_o"]}">{"".join(b)}</g>')

    city = [f'<rect x="{x}" y="{140 - h}" width="{w}" height="{h}"/>' for x, w, h in BUILDINGS]
    city.append('<rect x="902" y="62" width="2" height="9"/><rect x="1036" y="68" width="2" height="9"/>')
    tower = ("M1079 140L1084.5 122L1091.5 122L1097 140Z"
             "M1084.5 124L1085.6 42L1090.4 42L1091.5 124Z"
             "M1074 41L1102 41L1105.5 34.5L1100.5 26L1075.5 26L1070.5 34.5Z"
             "M1085.8 26V8H1090.2V26Z"
             "M1087.3 8V-14H1088.7V8Z")
    g.append(f'<g style="fill:{T["land"]};opacity:{T["land_o"]}">{"".join(city)}<path d="{tower}"/></g>')

    rng = random.Random(11)
    lamps = []
    for x, w, h in BUILDINGS:
        for _ in range(max(1, int(w * h / 240))):
            lx, ly = rng.uniform(x + 3, x + w - 5), rng.uniform(140 - h + 5, 134)
            lamps.append(f'<rect class="{rng.choice(["t1", "t2", "t3"])}" x="{lx:.1f}" y="{ly:.1f}" '
                         f'width="2.2" height="2.2" style="fill:{T["lamp"]}"/>')
    for xx in (1077, 1083, 1089, 1095):
        lamps.append(f'<rect class="t2" x="{xx}" y="32.5" width="2" height="2" style="fill:{T["lamp"]}"/>')
    lamps.append(f'<circle class="blink" cx="1088" cy="-14" r="2.4" style="fill:{T["rose"]}"/>')
    g.append(f'<g style="opacity:{T["lamp_o"]}">{"".join(lamps)}</g>')
    return "".join(g)


def sky(T, P, W, H, blobs, rays, star_box, n_stars, seed):
    s = [f'<rect x="7" y="7" width="{W - 14}" height="{H - 14}" style="fill:url(#{P}-sky)"/>',
         f'<g filter="url(#{P}-soft)" style="opacity:{T["ray_o"]}">']
    for i, (x, y, h) in enumerate(rays):
        s.append(f'<rect class="ray" x="{x}" y="{y}" width="10" height="{h}" rx="5" '
                 f'style="fill:url(#{P}-rayg);animation-delay:{-i * 1.7:.1f}s"/>')
    s.append(f'</g><g filter="url(#{P}-blur)">')
    for cx, cy, rx, ry, rot, key, cls in blobs:
        s.append(f'<g class="{cls}"><ellipse cx="{cx}" cy="{cy}" rx="{rx}" ry="{ry}" transform="rotate({rot} {cx} {cy})" '
                 f'style="fill:{T[key]};opacity:{T[key + "_o"]}"/></g>')
    s.append('</g>')
    s.append(f'<rect x="7" y="7" width="{W - 14}" height="{H - 14}" mask="url(#{P}-fade)" '
             f'style="fill:url(#{P}-dots);opacity:{T["dot_o"]}"/>')
    rng = random.Random(seed)
    x0, y0, x1, y1 = star_box
    s.append(f'<g style="opacity:{T["star_o"]}">')
    for _ in range(n_stars):
        x, y = rng.uniform(x0, x1), rng.uniform(y0, y1)
        r = rng.choice([.6, .8, 1.0, 1.2, 1.5, 1.9])
        s.append(f'<circle class="{rng.choice(["t1", "t2", "t3"])}" cx="{x:.1f}" cy="{y:.1f}" r="{r}" '
                 f'style="fill:{T["star"]}"/>')
    s.append('</g>')
    return "".join(s)


def hero_defs(T, P, W, H, cx, cy, r):
    return (
        f'<clipPath id="{P}-clip"><rect x="7" y="7" width="{W - 14}" height="{H - 14}" rx="26"/></clipPath>'
        f'<linearGradient id="{P}-sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:{T["sky0"]}"/>'
        f'<stop offset=".62" style="stop-color:{T["sky1"]}"/><stop offset="1" style="stop-color:{T["sky2"]}"/></linearGradient>'
        f'<linearGradient id="{P}-rayg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:{T["aurA"]};stop-opacity:0"/>'
        f'<stop offset=".35" style="stop-color:{T["aurA"]};stop-opacity:.9"/>'
        f'<stop offset="1" style="stop-color:{T["aurB"]};stop-opacity:0"/></linearGradient>'
        f'<radialGradient id="{P}-aura"><stop offset="0" style="stop-color:{T["aura"]};stop-opacity:{T["aura_o"]}"/>'
        f'<stop offset="1" style="stop-color:{T["aura"]};stop-opacity:0"/></radialGradient>'
        f'<pattern id="{P}-dots" width="24" height="24" patternUnits="userSpaceOnUse">'
        f'<circle cx="2" cy="2" r="1.15" style="fill:{T["dot"]}"/></pattern>'
        f'<radialGradient id="{P}-fadeg" cx=".28" cy=".2" r=".85"><stop offset="0" stop-color="#fff"/>'
        f'<stop offset="1" stop-color="#000"/></radialGradient>'
        f'<mask id="{P}-fade" maskUnits="userSpaceOnUse" x="0" y="0" width="{W}" height="{H}">'
        f'<rect x="0" y="0" width="{W}" height="{H}" fill="url(#{P}-fadeg)"/></mask>'
        + brand_u(T, P, "ringg", cx - r, cy - r, cx + r, cy + r)
    )


HERO_CSS = """
@keyframes %P-drift{0%{transform:translate(-34px,0)}100%{transform:translate(44px,10px)}}
@keyframes %P-drift2{0%{transform:translate(30px,6px)}100%{transform:translate(-40px,-6px)}}
@keyframes %P-ray{0%,100%{opacity:.3}50%{opacity:1}}
@keyframes %P-shake{0%,80%,100%{transform:translateX(0)}83%{transform:translateX(-5px)}86%{transform:translateX(5px)}89%{transform:translateX(-3px)}92%{transform:translateX(2px)}}
@keyframes %P-flash{0%,78%,100%{opacity:0}85%{opacity:1}}
@keyframes %P-sweep{from{transform:scaleX(0)}to{transform:scaleX(1)}}
.%P .a1{animation:%P-drift 21s ease-in-out infinite alternate}
.%P .a2{animation:%P-drift2 27s ease-in-out infinite alternate}
.%P .ray{animation:%P-ray 7s ease-in-out infinite}
.%P .ring{animation:%P-spin 24s linear infinite}
.%P .orbit{animation:%P-spinr 46s linear infinite}
.%P .shake{animation:%P-shake 6s ease-in-out infinite 1.5s}
.%P .flash{animation:%P-flash 6s ease-in-out infinite 1.5s}
.%P .sweep{transform-box:fill-box;transform-origin:0 50%;animation:%P-sweep 1.6s cubic-bezier(.2,.8,.2,1) .7s both}
@keyframes %P-circuit{to{stroke-dashoffset:-36}}
.%P .circuit{animation:%P-circuit 6s linear infinite}
@keyframes %P-allow-route{0%,100%{transform:translate(0,0)}25%{transform:translate(14px,-12px)}50%{transform:translate(28px,0)}75%{transform:translate(14px,12px)}}
@keyframes %P-deny-route{0%,100%{transform:translate(0,0)}25%{transform:translate(-12px,-10px)}50%{transform:translate(-24px,0)}75%{transform:translate(-12px,10px)}}
@keyframes %P-audit-route{0%,100%{transform:translate(0,0)}25%{transform:translate(12px,-12px)}50%{transform:translate(24px,0)}75%{transform:translate(12px,12px)}}
@keyframes %P-deny-glow{0%,72%,100%{opacity:0}82%,88%{opacity:.55}95%{opacity:0}}
.%P .chip-allow{animation:%P-allow-route 12s ease-in-out infinite}
.%P .chip-deny{animation:%P-deny-route 14s ease-in-out infinite 1.4s}
.%P .chip-audit{animation:%P-audit-route 16s ease-in-out infinite .7s}
.%P .deny-glow{animation:%P-deny-glow 6s ease-in-out infinite 1.5s}
"""


def governance_core(T, P, cx, cy, r):
    """Original policy shield and circuit nodes; no portrait or raster dependency."""
    r1, r2 = r + 14, r + 46
    pk = []
    for ang, key in ((-38, "green"), (98, "violet"), (212, "magenta")):
        a = math.radians(ang)
        px, py = cx + r2 * math.cos(a), cy + r2 * math.sin(a)
        pk.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="10" style="fill:{T[key]};opacity:.2"/>'
                  f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4.4" style="fill:{T[key]}"/>')
    org = f"transform-origin:{cx}px {cy}px;transform-box:view-box"
    base = (
        f'<circle cx="{cx}" cy="{cy}" r="{r + 70}" style="fill:url(#{P}-aura)"/>'
        f'<g class="orbit" style="{org}"><circle cx="{cx}" cy="{cy}" r="{r2}" style="fill:none;stroke:{T["ink3"]};'
        f'stroke-opacity:.5;stroke-width:1.3;stroke-dasharray:1.5 8;stroke-linecap:round"/>{"".join(pk)}</g>'
        f'<circle class="ring" cx="{cx}" cy="{cy}" r="{r1}" style="fill:none;stroke:url(#{P}-ringg);'
        f'stroke-width:3.6;{org}"/>'
        f'<g transform="translate({cx} {cy})" data-illustration="trusted-cloud-agent-governance">'
        f'<path d="M-142-50H-108L-62-6M138-104H110L58-48M140 88H104L60 42" '
        f'style="fill:none;stroke:url(#{P}-brand);stroke-width:3;opacity:.75"/>'
        f'<path class="circuit" d="M-142-50H-108L-62-6M138-104H110L58-48M140 88H104L60 42" '
        f'style="fill:none;stroke:{T["ink"]};stroke-width:2;stroke-dasharray:3 15;opacity:.5"/>'
        f'<path d="M0-106L91-69V-5C91 62 44 111 0 130C-44 111-91 62-91-5V-69Z" '
        f'transform="translate(0 9)" style="fill:{T["violet"]};opacity:.16"/>'
        f'<path d="M0-106L91-69V-5C91 62 44 111 0 130C-44 111-91 62-91-5V-69Z" '
        f'style="fill:url(#{P}-card);stroke:url(#{P}-brand);stroke-width:4"/>'
        f'<path d="M0-86L73-56V-3C73 48 35 87 0 105C-35 87-73 48-73-3V-56Z" '
        f'style="fill:none;stroke:{T["ink3"]};stroke-width:1;stroke-opacity:.4"/>'
        f'<rect x="-36" y="-40" width="72" height="72" rx="15" '
        f'style="fill:{T["chip"]};stroke:url(#{P}-brand);stroke-width:2"/>'
        f'<path d="M-52-24H-36M-52-4H-36M-52 16H-36M36-24H52M36-4H52M36 16H52'
        f'M-20-56V-40M0-56V-40M20-56V-40M-20 32V48M0 32V48M20 32V48" '
        f'style="fill:none;stroke:{T["teal"]};stroke-width:3;stroke-linecap:round"/>'
        f'<path d="M-18-7L-3 8L23-21" style="fill:none;stroke:{T["green"]};stroke-width:7;'
        f'stroke-linecap:round;stroke-linejoin:round"/>'
        f'<circle cx="0" cy="75" r="5" style="fill:{T["green"]}"/>'
        '</g>'
    )
    nodes = []
    icons = {
        "cloud": '<path d="M-18 8H16a10 10 0 0 0 1-20 14 14 0 0 0-27-2 11 11 0 0 0-8 22Z"/>',
        "agent": '<path d="M0-19L18-9V10L0 20-18 10V-9Z"/><path d="M0-8L8-3V5L0 9-8 5V-3Z"/>',
        "tool": '<path d="M-9-13L-22 0-9 13M9-13L22 0 9 13M4-17L-4 17"/>',
    }
    for dx, dy, key, colour in ((128, -112, "cloud", "teal"), (-132, -28, "agent", "violet"),
                                (128, 94, "tool", "magenta")):
        nodes.append(
            f'<g transform="translate({cx + dx} {cy + dy})" data-node="{key}">'
            f'<circle r="35" style="fill:{T["chip"]};stroke:{T[colour]};stroke-width:2"/>'
            f'<circle r="28" style="fill:none;stroke:{T[colour]};stroke-opacity:.18;stroke-width:5"/>'
            f'<g style="fill:none;stroke:{T[colour]};stroke-width:2.8;stroke-linecap:round;'
            f'stroke-linejoin:round">{icons[key]}</g></g>'
        )
    return base + "".join(nodes)


def decision_chip(T, P, x, y, kind, verb, obj, align="start", fs=20):
    col = {"allow": T["green"], "deny": T["rose"], "audit": T["violet"]}[kind]
    h = 48
    w = 14 + 24 + 11 + tw(f"{verb} · {obj}", fs, "mono") + 18
    if align == "end":
        x -= w
    cx, cy = x + 14 + 12, y + h / 2
    if kind == "allow":
        icon = (f'<path d="M{cx - 5:.1f} {cy + .5:.1f}l3.4 3.4 6.4-7" style="fill:none;stroke:{col};stroke-width:2.3;'
                f'stroke-linecap:round;stroke-linejoin:round"/>')
    elif kind == "deny":
        icon = (f'<path d="M{cx - 4:.1f} {cy - 4:.1f}l8 8M{cx + 4:.1f} {cy - 4:.1f}l-8 8" style="fill:none;'
                f'stroke:{col};stroke-width:2.3;stroke-linecap:round"/>')
    else:
        icon = f'<path d="M{cx:.1f} {cy - 5.5:.1f}l5 5.5-5 5.5-5-5.5z" style="fill:{col}"/>'
    body = (f'<rect x="{x:.1f}" y="{y}" width="{w:.1f}" height="{h}" rx="{h / 2}" filter="url(#{P}-shadow)" '
            f'style="fill:{T["chip"]};fill-opacity:{T["chip_o"]};stroke:{T["chipline"]};stroke-width:1.2"/>'
            f'<circle cx="{cx:.1f}" cy="{cy}" r="12" style="fill:{col};fill-opacity:.16;stroke:{col};'
            f'stroke-opacity:.55;stroke-width:1.2"/>{icon}'
            f'<text class="m" x="{x + 49:.1f}" y="{y + 24 + fs * .36:.1f}" xml:space="preserve" '
            f'data-font-role="decision" style="font-size:{fs}px;fill:{T["ink"]}">'
            f'<tspan style="fill:{col};font-weight:700">{verb}</tspan><tspan style="fill:{T["ink3"]}"> · </tspan>'
            f'{esc(obj)}</text>')
    if kind == "deny":
        halo = (f'<rect class="deny-glow" data-hero-effect="deny-glow" x="{x - 4:.1f}" y="{y - 4}" '
                f'width="{w + 8:.1f}" height="{h + 8}" '
                f'rx="{h / 2 + 4}" filter="url(#{P}-soft)" '
                f'style="fill:none;stroke:{col};stroke-width:8;opacity:0"/>')
        body = (f'<g class="shake">{halo}{body}<rect class="flash" data-hero-effect="deny-flash" '
                f'x="{x - 2:.1f}" y="{y - 2}" '
                f'width="{w + 4:.1f}" height="{h + 4}" rx="{h / 2 + 2}" '
                f'style="fill:none;stroke:{col};stroke-width:2;opacity:0"/></g>')
    return body, w


HERO_CHIPS = {
    "desktop": (("allow", 900, 80, "repo.read", "end"),
                ("deny", 930, 439, "shell.exec", "end"),
                ("audit", 1148, 534, "signed", "end")),
    "phone": (("allow", 30, 114, "repo.read", "start"),
              ("deny", 564, 498, "shell.exec", "end"),
              ("audit", 34, 600, "signed", "start")),
}


def hero_chips(T, P, phone=False):
    elements = []
    for kind, x, y, action, align in HERO_CHIPS["phone" if phone else "desktop"]:
        body, _ = decision_chip(T, P, x, y, kind, kind, action, align=align, fs=26 if phone else 20)
        elements.append(f'<g class="chip-{kind}" data-hero-chip="{kind}">{body}</g>')
    return "".join(elements)


def greeting(T, P, x, y, text, center=False, fs=20):
    h = 48 if fs <= 20 else 58
    w = 22 + 16 + tw(text, fs, "bold") * 0.97 + 14
    if center:
        x -= w / 2
    dx, dy = x + 21, y + h / 2
    return (f'<rect x="{x:.1f}" y="{y}" width="{w:.1f}" height="{h}" rx="{h / 2}" '
            f'style="fill:{T["chip"]};fill-opacity:{T["chip_o"]};stroke:{T["chipline"]};stroke-width:1.2"/>'
            f'<circle class="ping" cx="{dx:.1f}" cy="{dy}" r="4.8" style="fill:none;stroke:{T["green"]};'
            f'stroke-width:1.6;transform-origin:{dx:.1f}px {dy}px;transform-box:view-box"/>'
            f'<circle cx="{dx:.1f}" cy="{dy}" r="4.6" style="fill:{T["green"]}"/>'
            + tx(dx + 16, dy + fs * .36, text, fs, T["ink2"], 600, role="greeting"))


HERO_TITLE = ("ricky g. Cloud and AI Solution Architect, Auckland, New Zealand. Engineer at heart. "
              "The hard part isn't creating AI. It's governing it. A layered policy shield links cloud, "
              "AI-agent and tool nodes through identity, policy and audit above the Auckland skyline.")


def hero_desktop(T, P):
    W, H, cx, cy, r = 1200, 644, 935, 276, 124
    o = [svg_open(P, W, H, HERO_TITLE, css(P, HERO_CSS)),
         "<defs>", defs_common(T, P), hero_defs(T, P, W, H, cx, cy, r), "</defs>",
         f'<g clip-path="url(#{P}-clip)">',
         sky(T, P, W, H,
             blobs=[(250, 20, 340, 82, -7, "aurA", "a1"), (650, -6, 380, 96, 5, "aurB", "a2"),
                    (1090, 50, 260, 92, -12, "aurC", "a1"), (930, 150, 170, 60, 0, "aurA", "a2")],
             rays=[(120, -10, 230), (170, -10, 190), (232, -10, 250), (300, -10, 205), (372, -10, 238),
                   (450, -10, 180)],
             star_box=(20, 16, 1180, 380), n_stars=58, seed=7),
         f'<g transform="translate(0 497)">{skyline(T, P)}</g>', "</g>",
         greeting(T, P, 50, 50, "Kia ora · shipping from Auckland, NZ"),
         f'<text x="44" y="232" style="font-size:164px;font-weight:800;letter-spacing:-6px;fill:{T["ink"]}">ricky '
         f'<tspan style="fill:url(#{P}-shim)">g.</tspan></text>',
         tx(48, 313, "Cloud & AI Solution Architect", 34, T["ink"], 650, role="hero-role"),
         tx(48, 356, "Engineer at heart · Auckland, New Zealand", 25, T["ink3"], 500, role="hero-meta"),
         tx(48, 430, "The hard part isn't creating AI.", 38, T["ink2"], 500, role="hero-slogan"),
         tx(48, 491, "It's governing it.", 52, f"url(#{P}-brand)", 800, ls=-0.5, role="hero-slogan"),
         f'<rect class="sweep" x="50" y="513" width="190" height="5" rx="2.5" style="fill:url(#{P}-brand)"/>',
         governance_core(T, P, cx, cy, r)]
    o += [hero_chips(T, P),
          f'<rect x="7.7" y="7.7" width="{W - 15.4}" height="{H - 15.4}" rx="25.3" '
          f'style="fill:none;stroke:{T["cardline"]};stroke-width:1.4"/></svg>']
    return "".join(o)


def hero_phone(T, P):
    W, H, cx, cy, r = 600, 1304, 300, 336, 122
    o = [svg_open(P, W, H, HERO_TITLE, css(P, HERO_CSS)),
         "<defs>", defs_common(T, P), hero_defs(T, P, W, H, cx, cy, r), "</defs>",
         f'<g clip-path="url(#{P}-clip)">',
         sky(T, P, W, H,
             blobs=[(140, 10, 240, 80, -8, "aurA", "a1"), (380, -10, 260, 90, 6, "aurB", "a2"),
                    (560, 90, 170, 80, -12, "aurC", "a1")],
             rays=[(70, -10, 220), (120, -10, 180), (180, -10, 240), (250, -10, 200)],
             star_box=(20, 16, 580, 560), n_stars=40, seed=9),
         f'<g transform="translate(0 1226) scale(.5)">{skyline(T, P)}</g>', "</g>",
         greeting(T, P, 300, 36, "Kia ora · Auckland, NZ", center=True, fs=32),
         governance_core(T, P, cx, cy, r)]
    o += [hero_chips(T, P, phone=True),
          f'<text x="300" y="848" text-anchor="middle" style="font-size:156px;font-weight:800;letter-spacing:-5px;'
          f'fill:{T["ink"]}">ricky <tspan style="fill:url(#{P}-shim)">g.</tspan></text>',
          tx(300, 924, "Cloud & AI Solution", 44, T["ink"], 650, anchor="middle", role="hero-role"),
          tx(300, 978, "Architect", 44, T["ink"], 650, anchor="middle", role="hero-role"),
          tx(300, 1030, "Auckland, NZ · Engineer at heart", 34, T["ink3"], 500, anchor="middle", role="hero-meta")]
    y = 1092
    for line in wrap("The hard part isn't creating AI.", 40, "regular", 532, 2):
        o.append(tx(300, y, line, 40, T["ink2"], 500, anchor="middle", role="hero-slogan"))
        y += 48
    o += [tx(300, 1208, "It's governing it.", 56, f"url(#{P}-brand)", 800, anchor="middle",
             ls=-0.5, role="hero-slogan"),
          f'<rect class="sweep" x="220" y="1230" width="160" height="5" rx="2.5" style="fill:url(#{P}-brand)"/>',
          f'<rect x="7.7" y="7.7" width="{W - 15.4}" height="{H - 15.4}" rx="25.3" '
          f'style="fill:none;stroke:{T["cardline"]};stroke-width:1.4"/></svg>']
    return "".join(o)


INTRO_BODY = ("I design cloud architectures, build AI systems you can trust, and create developer platforms "
              "that accelerate delivery. I write about the journey on my blog.")
ABOUT_BODY = ("I'm an engineer from Auckland, passionate about cloud, AI, and startups. I build scalable "
              "systems, govern AI agents, and obsess over developer experience, turning gnarly architecture "
              "into things that actually ship.")
INTRO_TITLE = "Engineer at heart. " + INTRO_BODY + " About me. " + ABOUT_BODY


def introduction(T, P, phone=False):
    width, margin, body_size, heading_size = (400, 28, 35, 44) if phone else (1200, 44, 30, 42)
    elements, y = [], 78 if not phone else 84
    for heading, body in (("Engineer at heart.", INTRO_BODY), ("About me", ABOUT_BODY)):
        for line in wrap(heading, heading_size, "bold", width - 2 * margin, 4):
            elements.append(tx(margin, y, line, heading_size, T["ink"], 750, role="intro-heading"))
            y += heading_size * 1.25
        y += 8
        for line in wrap(body, body_size, "regular", width - 2 * margin, 30):
            elements.append(tx(margin, y, line, body_size, T["ink2"], role="intro-body"))
            y += body_size * 1.45
        y += 34
    height = math.ceil(y)
    return (svg_open(P, width, height, INTRO_TITLE, css(P)) + "<defs>" + defs_common(T, P) + "</defs>"
            + card(T, P, 7, 7, width - 14, height - 14) + "".join(elements) + "</svg>")


# --------------------------------------------------------------------------- link pills
PILLS = {
    "blog": ("READ THE BLOG", "clouddev.blog", "https://clouddev.blog"),
    "site": ("HOME BASE", "ricky-g.com", "https://ricky-g.com"),
    "linkedin": ("LET'S CONNECT", "LinkedIn", "https://www.linkedin.com/in/rickygummadi"),
}


def pill(T, P, kind):
    W, H = 300, 104
    kick, label, _ = PILLS[kind]
    hot = kind == "blog"
    o = [svg_open(P, W, H, f"{label}: {kick.capitalize()}", css(P)), "<defs>", defs_common(T, P),
         brand_u(T, P, "edge", 7, 0, 293, 0), "</defs>",
         f'<rect x="7" y="7" width="286" height="90" rx="45" style="fill:url(#{P}-card);'
         f'stroke:{f"url(#{P}-edge)" if hot else T["cardline"]};stroke-width:{1.8 if hot else 1.3}"/>']
    if kind == "blog":
        o.append(f'<circle cx="48" cy="48" r="27" style="fill:url(#{P}-brand)"/>'
                 f'<circle cx="38.5" cy="57.5" r="3.6" style="fill:#fff"/>'
                 f'<path d="M35 44a13.5 13.5 0 0 1 13.5 13.5M35 34.5a23 23 0 0 1 23 23" style="fill:none;stroke:#fff;'
                 f'stroke-width:3.6;stroke-linecap:round"/>')
    elif kind == "site":
        o.append(f'<circle cx="48" cy="48" r="27" style="fill:{T["ink"]}"/>'
                 f'<text x="48" y="58.5" text-anchor="middle" style="font-size:29px;font-weight:800;'
                 f'letter-spacing:-1px;fill:{T["card0"]}">g<tspan style="fill:url(#{P}-brand)">.</tspan></text>')
    else:
        o.append('<circle cx="48" cy="48" r="27" style="fill:#0a66c2"/>'
                 '<text x="48" y="57.5" text-anchor="middle" style="font-size:25px;font-weight:800;fill:#fff">in</text>')
    o += [tx(82, 40, kick, 17, T["ink3"], 700, mono=True, ls=.7, role="pill-meta"),
          tx(82, 74, label, 26, T["ink"], 750, role="pill-title"),
          arrow(272, 46, T["ink3"], 8, 2.1), "</svg>"]
    return "".join(o)


# --------------------------------------------------------------------------- section labels
LABELS = {
    "writing": ("01", "Writing", "published on clouddev.blog · latest three"),
    "building": ("02", "Building", "open-source projects · what I'm building"),
    "stack": ("03", "Stack", "what I build with, drawn like an architecture"),
    "pulse": ("04", "Pulse", "a year of building · GitHub activity"),
    "arcade": ("05", "Arcade", "a little play · a real contribution calendar"),
}


def label(T, P, key):
    num, title, meta = LABELS[key]
    W, H = 1200, 96
    css_x = """
@keyframes %P-run{0%{transform:translateX(0);opacity:0}6%{opacity:1}88%{opacity:1}100%{transform:translateX(1150px);opacity:0}}
.%P .run{animation:%P-run 7s cubic-bezier(.45,0,.2,1) infinite}
"""
    o = [svg_open(P, W, H, f"{title}: {meta}", css(P, css_x)), "<defs>", defs_common(T, P),
         f'<linearGradient id="{P}-line" x1="0" y1="0" x2="1" y2="0">'
         f'<stop offset="0" style="stop-color:{T["green"]};stop-opacity:.9"/>'
         f'<stop offset=".4" style="stop-color:{T["violet"]};stop-opacity:.55"/>'
         f'<stop offset="1" style="stop-color:{T["magenta"]};stop-opacity:0"/></linearGradient></defs>',
         tx(14, 62, num, 21, f"url(#{P}-brand)", 700, mono=True, role="section-number"),
         tx(62, 64, title, 36, T["ink"], 750, ls=-0.3, role="section-title"),
         tx(1186, 62, meta, 18.5, T["ink3"], 400, mono=True, anchor="end", role="section-meta"),
         f'<rect x="14" y="83" width="1172" height="1.6" style="fill:url(#{P}-line)"/>',
         f'<g class="run"><circle cx="18" cy="83.8" r="7" style="fill:{T["green"]};opacity:.22"/>'
         f'<circle cx="18" cy="83.8" r="3" style="fill:{T["green"]}"/></g>',
         "</svg>"]
    return "".join(o)


# --------------------------------------------------------------------------- covers
def cover_art(P, key, topic, line, x, y, w, h, rx=14, image_b64=None):
    """Stand-in cover in the clouddev.blog style (7:3, gradient wordmark, short hero line).
    When the feed provides an image, it is embedded instead."""
    cid = f"{P}-cv{key}"
    o = [f'<clipPath id="{cid}"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}"/></clipPath>',
         f'<g clip-path="url(#{cid})">']
    if image_b64:
        o.append(f'<image href="data:image/jpeg;base64,{image_b64}" x="{x}" y="{y}" width="{w}" height="{h}" '
                 f'preserveAspectRatio="xMidYMid slice"/>')
    else:
        C = COVER
        o.append(
            f'<linearGradient id="{cid}g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{C["bg0"]}"/>'
            f'<stop offset="1" stop-color="{C["bg1"]}"/></linearGradient>'
            f'<linearGradient id="{cid}w" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{C["a"]}"/>'
            f'<stop offset=".55" stop-color="{C["b"]}"/><stop offset="1" stop-color="{C["c"]}"/></linearGradient>'
            f'<pattern id="{cid}p" width="{w / 14:.2f}" height="{w / 14:.2f}" patternUnits="userSpaceOnUse">'
            f'<path d="M{w / 14:.2f} 0V{w / 14:.2f}H0" style="fill:none;stroke:#fff;stroke-opacity:.05;stroke-width:1"/></pattern>'
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="url(#{cid}g)"/>'
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="url(#{cid}p)"/>'
            f'<circle cx="{x + w * .86:.1f}" cy="{y + h * .12:.1f}" r="{h * .55:.1f}" filter="url(#{P}-blur)" '
            f'style="fill:{C["b"]};opacity:.55"/>'
            f'<circle cx="{x + w * .62:.1f}" cy="{y + h * 1.05:.1f}" r="{h * .45:.1f}" filter="url(#{P}-blur)" '
            f'style="fill:{C["a"]};opacity:.35"/>')
        size = min(h * 0.40, (w * 0.66) / max(len(topic) * 0.6, 1))
        o.append(tx(x + w * 0.065, y + h * 0.15, "clouddev.blog", max(h * 0.062, 9), "#ffffff", 600, mono=True,
                    extra=' opacity=".6"'))
        o.append(f'<text x="{x + w * 0.06:.1f}" y="{y + h * 0.62:.1f}" style="font-size:{size:.1f}px;font-weight:800;'
                 f'letter-spacing:{-size * 0.035:.1f}px;fill:url(#{cid}w)">{esc(topic)}</text>')
        o.append(tx(x + w * 0.065, y + h * 0.80, line, max(h * 0.105, 11), C["ink"], 600))
        # little node graph in the corner, a recurring cover motif
        nx, ny = x + w * 0.84, y + h * 0.56
        sc = h / 270
        pts = [(nx, ny - 46 * sc), (nx + 44 * sc, ny + 6 * sc), (nx - 8 * sc, ny + 50 * sc)]
        o.append(f'<path d="M{pts[0][0]:.1f} {pts[0][1]:.1f}L{pts[1][0]:.1f} {pts[1][1]:.1f}L{pts[2][0]:.1f} '
                 f'{pts[2][1]:.1f}Z" style="fill:none;stroke:#fff;stroke-opacity:.22;stroke-width:{1.4 * sc + .4:.1f}"/>')
        for (px, py), col in zip(pts, (C["a"], C["b"], C["c"])):
            o.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{7 * sc + 1.5:.1f}" style="fill:{col}"/>')
    o.append("</g>")
    return "".join(o)


# --------------------------------------------------------------------------- posts
def post_meta(post):
    dt = datetime.fromisoformat(post["date_published"].replace("Z", "+00:00")).astimezone(NZ)
    parts = [p for p in post["url"].split("clouddev.blog/", 1)[-1].strip("/").split("/")[:-1]]
    cat = " / ".join(p.replace("-", " ") for p in parts) or "clouddev.blog"
    return dt, f"{dt.day} {dt.strftime('%b %Y')}", cat


def new_badge(T, P, x, y):
    return (f'<rect x="{x}" y="{y}" width="82" height="36" rx="18" style="fill:url(#{P}-brand)"/>'
            f'<circle class="ping" cx="{x + 15}" cy="{y + 18}" r="4.6" style="fill:none;stroke:#fff;stroke-width:1.4;'
            f'transform-origin:{x + 15}px {y + 18}px;transform-box:view-box"/>'
            f'<circle cx="{x + 15}" cy="{y + 18}" r="4.4" style="fill:#fff"/>'
            + tx(x + 29, y + 24.5, "NEW", 18, "#ffffff", 800, ls=.8, role="post-meta"))


def post_featured(T, P, post, cover, now):
    dt, date, cat = post_meta(post)
    is_new = (now - dt) < timedelta(days=NEW_DAYS)
    x0, wmax = 616, 548
    meta = wrap(f"{date} · {cat}", 18.5, "mono", wmax - (96 if is_new else 0), 2)
    title = wrap(post["title"], 38, "bold", wmax, 4)
    excerpt = wrap(post.get("content_text", ""), 25, "regular", wmax, 4)
    title_y = 118 + (len(meta) - 1) * 27
    excerpt_y = title_y + len(title) * 47 + 8
    cta_y = excerpt_y + len(excerpt) * 35 + 24
    W, H = 1200, max(388, math.ceil(cta_y + 30))
    o = [svg_open(P, W, H, f"Latest post: {post['title']}", css(P)), "<defs>", defs_common(T, P), "</defs>",
         card(T, P, 7, 7, 1186, H - 14),
         cover_art(P, "f", cover.get("topic", ""), cover.get("line", ""), 24, (H - 237) / 2,
                   552, 237, 16, cover.get("b64"))]
    kx = x0
    if is_new:
        o.append(new_badge(T, P, x0, 38))
        kx = x0 + 96
    for index, line in enumerate(meta):
        o.append(tx(kx, 63 + index * 27, line, 18.5, T["ink3"], mono=True, role="post-meta"))
    for index, line in enumerate(title):
        o.append(tx(x0, title_y + index * 47, line, 38, T["ink"], 750, ls=-.3, role="post-title"))
    for index, line in enumerate(excerpt):
        o.append(tx(x0, excerpt_y + index * 35, line, 25, T["ink2"], role="post-body"))
    o.append(tx(x0, cta_y, "Read on clouddev.blog →", 24, f"url(#{P}-brand)", 750, role="post-link"))
    o.append("</svg>")
    return "".join(o)


def post_mini(T, P, post, cover, now):
    W, H = 400, 550
    dt, date, cat = post_meta(post)
    is_new = (now - dt) < timedelta(days=NEW_DAYS)
    short_cat = cat.split(" / ")[-1]
    o = [svg_open(P, W, H, f"Post: {post['title']}", css(P)), "<defs>", defs_common(T, P), "</defs>",
         card(T, P, 7, 7, 386, H - 14),
         cover_art(P, "m", cover.get("topic", ""), cover.get("line", ""), 21, 21, 358, 153, 14, cover.get("b64"))]
    if is_new:
        o.append(new_badge(T, P, 33, 33))
    for index, line in enumerate(wrap(f"{date} · {short_cat}", 21, "mono", 344, 2)):
        o.append(tx(28, 216 + index * 29, line, 21, T["ink3"], mono=True, role="post-meta"))
    for index, line in enumerate(wrap(post["title"], 27, "bold", 344, 6)):
        o.append(tx(28, 291 + index * 35, line, 27, T["ink"], 750, ls=-.2, role="post-title"))
    o.append(tx(28, 514, "Read the article →", 22, f"url(#{P}-brand)", 750, role="post-link"))
    o.append("</svg>")
    return "".join(o)


def archive_card(T, P, stats):
    W, H = 400, 550
    title = f"Browse {stats['posts']} published posts on clouddev.blog" if stats["posts"] is not None else "Browse the archive; article count unavailable."
    count = str(stats["posts"]) if stats["posts"] is not None else "Explore"
    suffix = f"posts since {stats['first_year']}" if stats["posts"] is not None else "the archive"
    size = 110 if stats["posts"] is not None else 58
    o = [svg_open(P, W, H, title, css(P)), "<defs>",
         defs_common(T, P), f'<clipPath id="{P}-c"><rect x="7" y="7" width="386" height="{H - 14}" rx="24"/></clipPath>',
         "</defs>", card(T, P, 7, 7, 386, H - 14),
         f'<g clip-path="url(#{P}-c)">{glow(T, P, 360, 40, 90, "aurC")}</g>',
         kicker(T, 28, 56, "THE ARCHIVE", "violet"),
         tx(26, 174, count, size, f"url(#{P}-brand)", 800, ls=-3, role="archive-count"),
         tx(28, 214, suffix, 26, T["ink2"], 600, role="archive-body")]
    x, y = 28, 252
    for t in ("Azure", "AI agents", "GitHub Copilot", "AKS", "APIM", "MCP", "DevOps"):
        w = 14 + tw(t, 21, "mono") + 14
        if x + w > 372:
            x, y = 28, y + 50
        s, w = chip_box(T, x, y, t, size=21, h=40, pad=14)
        o.append(s)
        x += w + 8
    o.append(tx(28, 514, "Browse clouddev.blog →", 22, f"url(#{P}-brand)", 750, role="archive-link"))
    o.append("</svg>")
    return "".join(o)


# --------------------------------------------------------------------------- building tiles
TILE_CSS = """
@keyframes %P-row{0%,%A{opacity:0;transform:translateX(-6px)}%B,93%{opacity:1;transform:none}100%{opacity:0}}
"""


def ring10(T, P, cx, cy, r, sw=6):
    o = []
    for i in range(10):
        a0, a1 = math.radians(-90 + i * 36 + 3), math.radians(-90 + (i + 1) * 36 - 3)
        x0, y0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
        x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
        o.append(f'<path class="in" d="M{x0:.1f} {y0:.1f}A{r} {r} 0 0 1 {x1:.1f} {y1:.1f}" '
                 f'style="fill:none;stroke:url(#{P}-ringu);stroke-width:{sw};stroke-linecap:round;'
                 f'animation-delay:{0.4 + i * 0.12:.2f}s"/>')
    return "".join(o)


def tile_agt(T, P, st):
    W, H = 800, 550
    rows = [("allow", "web.search"), ("allow", "repo.read"), ("deny", "shell.exec"),
            ("allow", "pr.create"), ("audit", "4 calls, signed")]
    kf = []
    for i in range(len(rows)):
        a = 3 + i * 11
        kf.append(f"@keyframes %P-row{i}{{0%,{a}%{{opacity:0;transform:translateX(-6px)}}{a + 4}%,93%{{opacity:1;"
                  f"transform:none}}100%{{opacity:0}}}}.%P .r{i}{{animation:%P-row{i} 9s ease-out infinite}}")
    o = [svg_open(P, W, H, "Now building: Agent Governance Toolkit, policy enforcement and zero-trust identity "
                           "for AI agents. The project reports OWASP Agentic Top 10 coverage.", css(P, "".join(kf))),
         "<defs>", defs_common(T, P), brand_u(T, P, "ringu", 446, 463, 488, 505),
         f'<clipPath id="{P}-c"><rect x="7" y="7" width="786" height="{H - 14}" rx="24"/></clipPath></defs>',
         card(T, P, 7, 7, 786, H - 14),
         f'<g clip-path="url(#{P}-c)">{glow(T, P, 770, 10, 150, "aurB")}{glow(T, P, 420, 360, 120, "aurA")}</g>',
         kicker(T, 34, 56, "NOW BUILDING", "green", pulse=True, P=P)]
    for index, line in enumerate(("microsoft/", "agent-governance-toolkit")):
        o.append(tx(34, 100 + index * 29, line, 21, T["ink3"], mono=True, role="project-meta"))
    o += [tx(32, 188, "Agent Governance", 44, T["ink"], 800, ls=-.6, role="project-title"),
          tx(32, 243, "Toolkit", 44, T["ink"], 800, ls=-.6, role="project-title")]
    y = 290
    for ln in wrap("Policy enforcement, zero-trust identity, execution sandboxing and reliability engineering "
                   "for autonomous AI agents.", 22, "regular", 372, 5):
        o.append(tx(34, y, ln, 22, T["ink2"], role="project-body"))
        y += 31
    x = 34
    for lab, icon in ((metric(st["agt_stars"]) if st["agt_stars"] is not None else "— stars",
                       STAR_ICON.replace("%C", T["gold"])),
                      (f"{st['agt_forks']} forks" if st["agt_forks"] is not None else "— forks", ""),
                      ("Python", "")):
        s, w = chip_box(T, x, 494, lab, size=21, h=40, icon=icon,
                        dot=("#3776ab" if lab == "Python" else (T["ink4"] if "forks" in lab else None)))
        o.append(s)
        x += w + 8
    # terminal
    o.append(tx(438, 125, "Policy illustration", 21, T["ink3"], mono=True, role="project-meta"))
    o.append(f'<rect x="438" y="152" width="332" height="290" rx="14" style="fill:{T["term"]};'
             f'stroke:{T["termline"]};stroke-width:1.2"/>')
    for i, c in enumerate((T["trose"], T["tgold"], T["tgreen"])):
        o.append(f'<circle cx="{456 + i * 18}" cy="175" r="5" style="fill:{c};opacity:.85"/>')
    o.append(tx(526, 182, "agt", 21, T["termdim"], mono=True, role="terminal-meta"))
    o.append(f'<rect x="438" y="196" width="332" height="1" style="fill:{T["termline"]}"/>')
    for i, (verdict, action) in enumerate(rows):
        yy = 233 + i * 38
        col = {"allow": T["tgreen"], "deny": T["trose"], "audit": T["tviolet"]}[verdict]
        g = []
        if verdict == "deny":
            g.append(f'<rect x="447" y="{yy - 23}" width="314" height="32" rx="6" style="fill:{T["trose"]};opacity:.14"/>')
        mark = {"allow": "✓", "deny": "✗", "audit": "◆"}[verdict]
        g.append(tx(454, yy, mark, 21, col, 700, mono=True))
        g.append(tx(477, yy, verdict, 21, col, 700, mono=True, role="terminal-body"))
        g.append(tx(551, yy, action, 21, T["termink"], mono=True, role="terminal-body"))
        o.append(f'<g class="r{i}">{"".join(g)}</g>')
    o.append(f'<rect class="blink" x="455" y="412" width="10" height="18" style="fill:{T["termink"]}"/>')
    o.append(ring10(T, P, 463, 483, 20, 6))
    o.append(tx(497, 490, "OWASP Agentic Top 10", 22, T["ink"], 700, role="project-body"))
    o.append(tx(438, 529, "10/10 · project-reported", 21, T["ink3"], mono=True, role="project-meta"))
    o.append("</svg>")
    return "".join(o)


def tile_head(T, P, kick, dot, title, right=None, pulse=False):
    o = [kicker(T, 28, 56, kick, dot, pulse=pulse, P=P)]
    for index, line in enumerate(wrap(title, 34, "bold", 344, 2)):
        o.append(tx(26, 136 + index * 42, line, 34, T["ink"], 800, ls=-.5, role="project-title"))
    if right:
        o.append(right)
    return "".join(o)


def tile_hub(T, P, st):
    W, H = 400, 550
    o = [svg_open(P, W, H, "Azure Scenario Hub: tested Azure and Microsoft AI scenarios, with a real APIM benchmark.",
                  css(P)), "<defs>", defs_common(T, P),
         f'<clipPath id="{P}-c"><rect x="7" y="7" width="386" height="{H - 14}" rx="24"/></clipPath></defs>',
         card(T, P, 7, 7, 386, H - 14), f'<g clip-path="url(#{P}-c)">{glow(T, P, 380, 20, 90, "aurA")}</g>',
         tile_head(T, P, "THE LAB", "violet", "Azure Scenario Hub")]
    star = STAR_ICON.replace("%C", T["gold"]).replace("%X", "322").replace("%Y", "82")
    o.append(star + tx(340, 89, metric(st["hub_stars"]) if st["hub_stars"] is not None else "—",
                      21, T["ink3"], 500, mono=True, role="project-meta"))
    y = 215
    for ln in wrap("Tested Azure and Microsoft AI scenarios: deployable architectures, runnable demos "
                   "and benchmarks.", 22, "regular", 340, 4):
        o.append(tx(28, y, ln, 22, T["ink2"], role="project-body"))
        y += 31
    o.append(tx(28, 346, "APIM FAN-OUT", 21, T["ink3"], 700, mono=True, role="project-meta"))
    o.append(tx(28, 376, "1.67M REQUESTS", 21, T["ink3"], mono=True, role="project-meta"))
    for i, (lab, val) in enumerate((("Shared backend", 882), ("Backend per API", 874))):
        yy = 416 + i * 44
        o.append(tx(28, yy, lab, 21, T["ink2"], 500, role="project-body"))
        o.append(tx(372, yy, f"{val} req/s", 21, T["ink"], 600, mono=True, anchor="end", role="project-body"))
        o.append(f'<rect x="28" y="{yy + 7}" width="344" height="9" rx="4.5" style="fill:{T["track"]}"/>')
        o.append(f'<rect class="grow" x="28" y="{yy + 7}" width="{344 * val / 900:.1f}" height="9" rx="4.5" '
                 f'style="fill:url(#{P}-brand);animation-delay:{0.5 + i * 0.25}s"/>')
    o.append(tx(28, 508, "p95 146 vs 153 ms", 21, T["ink3"], mono=True, role="project-meta"))
    o.append(tx(28, 536, "0 errors · a tie", 21, T["ink3"], mono=True, role="project-meta"))
    o.append("</svg>")
    return "".join(o)


def tile_docker(T, P, st):
    W, H = 400, 500
    o = [svg_open(P, W, H, "docker-ai-models: prebuilt Dockerfiles for popular AI models.", css(P)), "<defs>",
         defs_common(T, P), "</defs>", card(T, P, 7, 7, 386, H - 14),
         tile_head(T, P, "CONTAINERS", "teal", "docker-ai-models",
                   tx(372, 90, "Python", 21, T["ink3"], 500, mono=True, anchor="end", role="project-meta")
                   + f'<circle cx="{372 - tw("Python", 21, "mono") - 10:.1f}" cy="83" r="4.4" style="fill:#3776ab"/>')]
    y = 214
    for ln in wrap("Prebuilt Dockerfiles for popular AI models. Container recipes, ready to explore.", 22,
                   "regular", 340, 3):
        o.append(tx(28, y, ln, 22, T["ink2"], role="project-body"))
        y += 31
    o.append(f'<rect x="26" y="326" width="348" height="142" rx="12" style="fill:{T["term"]};stroke:{T["termline"]};'
             f'stroke-width:1.1"/>')
    code = [("FROM", " python:3.11-slim"), ("RUN", "  pip install \\"),
            ("", "    mmgp gradio"), ("CMD", "  python app.py")]
    for i, (k, v) in enumerate(code):
        yy = 356 + i * 30
        o.append(tx(40, yy, str(i + 1), 21, T["termdim"], mono=True))
        o.append(f'<text class="m in" x="66" y="{yy}" xml:space="preserve" data-font-role="terminal-body" '
                 f'style="font-size:21px;fill:{T["termink"]};'
                 f'animation-delay:{0.4 + i * 0.35:.2f}s"><tspan style="fill:{T["tviolet"]};font-weight:700">{k}'
                 f'</tspan>{esc(v)}</text>')
    o.append("</svg>")
    return "".join(o)


def tile_ledger(T, P, st):
    W, H = 400, 500
    css_x = """
@keyframes %P-move{0%,30%{transform:translate(0,0)}45%,80%{transform:translate(112px,-2px)}95%,100%{transform:translate(0,0)}}
.%P .mv{animation:%P-move 7s cubic-bezier(.6,0,.2,1) infinite}
"""
    o = [svg_open(P, W, H, "ledgerboard: local-first Markdown Kanban for VS Code.", css(P, css_x)), "<defs>",
         defs_common(T, P), "</defs>", card(T, P, 7, 7, 386, H - 14),
         tile_head(T, P, "VS CODE EXTENSION", "blue", "ledgerboard",
                   tx(372, 90, "JavaScript", 21, T["ink3"], 500, mono=True, anchor="end", role="project-meta")
                   + f'<circle cx="{372 - tw("JavaScript", 21, "mono") - 10:.1f}" cy="83" r="4.4" style="fill:#f1e05a"/>')]
    y = 214
    for ln in wrap("Local-first Markdown Kanban for VS Code, with conflict-safe autosave.", 22, "regular", 340, 3):
        o.append(tx(28, y, ln, 22, T["ink2"], role="project-body"))
        y += 31
    cols = ("Todo", "Doing", "Done")
    for i, c in enumerate(cols):
        x = 26 + i * 116
        o.append(f'<rect x="{x}" y="326" width="108" height="142" rx="10" style="fill:{T["chip"]};stroke:{T["chipline"]};'
                 f'stroke-width:1"/>')
        o.append(tx(x + 10, 355, c.upper(), 21, T["ink3"], 700, mono=True, role="project-meta"))
        n = (2, 1, 2)[i]
        for k in range(n):
            o.append(f'<rect x="{x + 9}" y="{372 + k * 39}" width="90" height="30" rx="6" style="fill:{T["card0"]};'
                     f'stroke:{T["chipline"]};stroke-width:1"/>'
                     f'<rect x="{x + 17}" y="{384 + k * 39}" width="{(52, 38, 60)[(i + k) % 3]}" height="5" rx="2.5" '
                     f'style="fill:{T["ink4"]};opacity:.55"/>')
    o.append(f'<g class="mv"><rect x="35" y="429" width="90" height="30" rx="6" filter="url(#{P}-shadow)" '
             f'style="fill:{T["card0"]};stroke:{T["violet"]};stroke-width:1.4"/>'
             f'<rect x="43" y="441" width="46" height="5" rx="2.5" style="fill:url(#{P}-brand)"/></g>')
    o.append("</svg>")
    return "".join(o)


def tile_arcade(T, P, st):
    W, H = 400, 500
    cells_x, cells_y, s, gap = 19, 5, 13, 4.6
    x0, y0 = 29, 338
    css_x = """
@keyframes %P-slither{0%{transform:translateX(0)}100%{transform:translateX(%Dpx)}}
.%P .sn{animation:%P-slither 5s steps(%N) infinite}
""".replace("%D", f"{(s + gap) * 9:.1f}").replace("%N", "9")
    o = [svg_open(P, W, H, "mini arcade: play snake and minesweeper on ricky-g.com.", css(P, css_x)), "<defs>",
         defs_common(T, P), "</defs>", card(T, P, 7, 7, 386, H - 14),
         tile_head(T, P, "INSERT COIN", "gold", "mini arcade",
                   tx(372, 90, "ricky-g.com", 21, T["ink3"], 500, mono=True, anchor="end", role="project-meta"))]
    y = 214
    for ln in wrap("Snake and minesweeper, live on ricky-g.com. Beat my high score.", 22, "regular", 340, 3):
        o.append(tx(28, y, ln, 22, T["ink2"], role="project-body"))
        y += 31
    rng = random.Random(5)
    for j in range(cells_y):
        for i in range(cells_x):
            op = rng.choice([.35, .35, .35, .55, .8])
            o.append(f'<rect x="{x0 + i * (s + gap):.1f}" y="{y0 + j * (s + gap):.1f}" width="{s}" height="{s}" rx="3" '
                     f'style="fill:{T["chip"]};stroke:{T["chipline"]};stroke-width:.8;opacity:{op}"/>')
    sy = y0 + 2 * (s + gap)
    o.append(f'<rect x="{x0 + 14 * (s + gap):.1f}" y="{sy:.1f}" width="{s}" height="{s}" rx="6.5" style="fill:{T["rose"]}"/>')
    body = []
    for k in range(5):
        body.append(f'<rect x="{x0 + (4 - k) * (s + gap):.1f}" y="{sy:.1f}" width="{s}" height="{s}" rx="3.5" '
                    f'style="fill:url(#{P}-brand);opacity:{1 - k * 0.14:.2f}"/>')
    o.append(f'<g class="sn">{"".join(body)}</g>')
    o.append(tx(372, 474, "▶ PRESS START", 21, T["ink3"], 700, mono=True, anchor="end", cls="blink",
                role="project-meta"))
    o.append("</svg>")
    return "".join(o)


# --------------------------------------------------------------------------- stack
STACK = {
    "layers": [
        ("AGENTS & AI", [("GitHub Copilot", "#8957e5"), ("Model Context Protocol", "#94a3b8"),
                         ("Microsoft Agent Framework", "#0078d4"), ("Semantic Kernel", "#7b61ff"),
                         ("Microsoft Foundry", "#8661c5"), ("Azure OpenAI", "#10a37f")]),
        ("CLOUD PLATFORM", [("Azure", "#0078d4"), ("AKS", "#326ce5"), ("API Management", "#00a2ed"),
                            ("Container Apps", "#6b5cd6"), ("Functions", "#f2c811"), ("Bicep", "#0b7bd6"),
                            ("Terraform", "#7b42bc"), ("Docker", "#2496ed")]),
        ("LANGUAGES", [("C#", "#9b4f96"), (".NET", "#512bd4"), ("Python", "#3776ab"), ("TypeScript", "#3178c6"),
                       ("PowerShell", "#5391fe")]),
    ],
    "cross": ("GOVERNANCE", "cuts across every layer",
              [("Agent Governance Toolkit", None), ("Entra ID", "#0078d4"), ("Private Link", "#00a2ed"),
               ("Azure Monitor", "#0f6cbd")]),
}


def stack(T, P):
    W = 1200
    band_x, band_w, lab_w = 22, 770, 212
    cx0, cw = 814, 364
    css_x = """
@keyframes %P-dash{to{stroke-dashoffset:-24}}
.%P .flow{animation:%P-dash 1.2s linear infinite}
"""
    bands, y = [], 26
    for name, chips in STACK["layers"]:
        rows, x, row = [], band_x + lab_w, []
        for lab, col in chips:
            w = 13 + 16 + tw(lab, 22, "regular") + 13
            if x + w > band_x + band_w - 16 and row:
                rows.append(row)
                row, x = [], band_x + lab_w
            row.append((lab, col, x))
            x += w + 9
        rows.append(row)
        h = max(40 + len(rows) * 56, 120)
        bands.append((name, rows, y, h))
        y += h + 18
    H = y + 12
    o = [svg_open(P, W, H, "Stack: agents and AI, cloud platform and languages, with governance across every layer.",
                  css(P, css_x)), "<defs>", defs_common(T, P), brand_u(T, P, "edge", cx0, 26, cx0 + cw, H - 26),
         "</defs>", card(T, P, 7, 7, 1186, H - 14)]
    for i, (name, rows, by, bh) in enumerate(bands):
        o.append(f'<rect x="{band_x}" y="{by}" width="{band_w}" height="{bh}" rx="16" style="fill:{T["chip"]};'
                 f'fill-opacity:.55;stroke:{T["chipline"]};stroke-width:1"/>')
        o.append(tx(band_x + 18, by + 47, name, 18, T["ink3"], 700, mono=True, ls=1, role="stack-meta"))
        o.append(tx(band_x + 18, by + 77, ("layer 3", "layer 2", "layer 1")[i], 18, T["ink3"],
                    mono=True, role="stack-meta"))
        for r, row in enumerate(rows):
            for lab, col, x in row:
                s, _ = chip_box(T, x, by + 22 + r * 56, lab, dot=col, size=22, h=44, pad=13, mono=False,
                                fill=T["card0"])
                o.append(s)
        my = by + bh / 2
        o.append(f'<path class="flow" d="M{band_x + band_w} {my:.1f}H{cx0}" style="fill:none;stroke:{T["violet"]};'
                 f'stroke-width:1.6;stroke-dasharray:4 8;opacity:.8"/>')
        o.append(f'<circle cx="{cx0}" cy="{my:.1f}" r="4" style="fill:{T["violet"]}"/>')
    name, sub, chips = STACK["cross"]
    o.append(f'<rect x="{cx0}" y="26" width="{cw}" height="{H - 52}" rx="16" style="fill:{T["chip"]};fill-opacity:.55;'
             f'stroke:url(#{P}-edge);stroke-width:1.8"/>')
    sx, sy = cx0 + 24, 54
    o.append(f'<path d="M{sx + 9} {sy - 13}l9 3.6v6.4c0 5.8-4 10-9 11.8-5-1.8-9-6-9-11.8v-6.4z" '
             f'style="fill:url(#{P}-brand)"/><path d="M{sx + 5} {sy + 1}l3 3 5.5-6" style="fill:none;stroke:#fff;'
             f'stroke-width:2;stroke-linecap:round;stroke-linejoin:round"/>')
    o.append(tx(sx + 28, sy + 4, name, 20, T["ink"], 700, mono=True, ls=1.2, role="stack-heading"))
    o.append(tx(sx, sy + 40, sub, 22, T["ink3"], role="stack-body"))
    yy = sy + 64
    for lab, col in chips:
        if col is None:
            lines = wrap(lab, 22, "bold", cw - 78, 2)
            height = 22 + len(lines) * 29
            s = f'<rect x="{sx}" y="{yy}" width="{cw - 48}" height="{height}" rx="18" style="fill:url(#{P}-brand)"/>'
            s += "".join(tx(sx + 15, yy + 29 + index * 29, line, 22, "#ffffff", 700, role="stack-body")
                         for index, line in enumerate(lines))
        else:
            height = 44
            s, _ = chip_box(T, sx, yy, lab, dot=col, size=22, h=height, pad=14, mono=False, fill=T["card0"])
        o.append(s)
        yy += height + 16
    fy = H - 26 - 44
    o.append(f'<rect x="{sx}" y="{fy - 24}" width="{cw - 48}" height="1" style="fill:{T["chipline"]}"/>')
    o.append(tx(sx, fy - 8, "every tool call", 18.5, T["ink3"], mono=True, role="stack-meta"))
    o.append(f'<text class="m" x="{sx}" y="{fy + 24}" data-font-role="stack-meta" style="font-size:18.5px;fill:{T["ink2"]}">'
             f'identity <tspan style="fill:{T["violet"]}">›</tspan> policy <tspan style="fill:{T["violet"]}">›</tspan> audit</text>')
    o.append("</svg>")
    return "".join(o)


# --------------------------------------------------------------------------- pulse
def pulse(T, P, st):
    if st["mix"] is None or st["contributions"] is None:
        return activity_notice(T, P, st)
    W, H = 1200, 360
    mix = st["mix"]
    total = sum(mix.values()) or 1
    o = [svg_open(P, W, H, f"GitHub activity: {st['contributions']:,} contributions in the last year.", css(P)), "<defs>",
         defs_common(T, P), f'<clipPath id="{P}-c"><rect x="7" y="7" width="1186" height="{H - 14}" rx="24"/></clipPath>',
         "</defs>", card(T, P, 7, 7, 1186, H - 14),
         f'<g clip-path="url(#{P}-c)">{glow(T, P, 120, 300, 130, "aurA")}</g>',
         kicker(T, 40, 62, "LAST 12 MONTHS", "green", pulse=True, P=P),
         tx(36, 188, f"{st['contributions']:,}", 110, f"url(#{P}-brand)", 800, ls=-3, role="pulse-count"),
         tx(40, 241, "contributions on GitHub", 25, T["ink2"], 500, role="pulse-body"),
         tx(40, 326, "A year of building", 18.5, T["ink3"], mono=True, role="pulse-meta"),
         f'<rect x="404" y="40" width="1" height="280" style="fill:{T["cardline"]}"/>',
         f'<rect x="838" y="40" width="1" height="280" style="fill:{T["cardline"]}"/>',
         kicker(T, 438, 62, "WHERE IT GOES", "violet")]
    x = 438
    segs = (("Commits", "commits", T["green"]), ("Pull requests", "prs", T["teal"]),
            ("Issues", "issues", T["violet"]), ("Code review", "reviews", T["magenta"]))
    bw = 360
    o.append(f'<clipPath id="{P}-bar"><rect x="438" y="86" width="{bw}" height="22" rx="11"/></clipPath>'
             f'<g clip-path="url(#{P}-bar)" class="grow">')
    for _, k, col in segs:
        w = bw * mix[k] / total
        o.append(f'<rect x="{x:.1f}" y="86" width="{w + .5:.1f}" height="22" style="fill:{col}"/>')
        x += w
    o.append("</g>")
    for i, (lab, k, col) in enumerate(segs):
        cx, cy = 438, 152 + i * 40
        pct = round(100 * mix[k] / total)
        o.append(f'<circle cx="{cx + 5}" cy="{cy - 5}" r="5" style="fill:{col}"/>')
        o.append(tx(cx + 18, cy, lab, 22, T["ink2"], 500, role="pulse-body"))
        o.append(tx(814, cy, f"{pct}%", 22, T["ink"], 700, mono=True, anchor="end", role="pulse-body"))
    o.append(tx(438, 326, "public repository activity", 18.5, T["ink3"], mono=True, role="pulse-meta"))
    stats_r = ((metric(st["repos"]), "public repositories"), (metric(st["contributed_to"]), "public repos contributed to"),
               (metric(st["posts"]), "posts on clouddev.blog"))
    for i, (num, lab) in enumerate(stats_r):
        yy = 96 + i * 94
        o.append(tx(864, yy, num, 46, T["ink"], 800, ls=-1, role="pulse-count"))
        for index, line in enumerate(wrap(lab, 22, "regular", 304, 2)):
            o.append(tx(866, yy + 32 + index * 28, line, 22, T["ink3"], role="pulse-body"))
    o.append("</svg>")
    return "".join(o)


# --------------------------------------------------------------------------- sign-off
OFF_CLOCK_BODY = ("I'm still building AI and AI agents, exploring emerging tech, and chasing far too many "
                  "side projects. I love meeting founders and building crazy, impossible ideas together, "
                  "so if that's you, let's talk.")
OFF_CLOCK_TEXT = "Off the clock? " + OFF_CLOCK_BODY


def signoff(T, P, st):
    W, H = 1200, 410
    o = [svg_open(P, W, H, OFF_CLOCK_TEXT, css(P)),
         "<defs>", defs_common(T, P), f'<clipPath id="{P}-c"><rect x="7" y="7" width="1186" height="{H - 14}" rx="24"/>'
         f'</clipPath></defs>', card(T, P, 7, 7, 1186, H - 14),
         f'<g clip-path="url(#{P}-c)">{glow(T, P, 1150, 230, 170, "aurB")}{glow(T, P, 900, 0, 120, "aurA")}</g>',
         kicker(T, 44, 60, "OFF THE CLOCK", "magenta"),
         tx(42, 121, "Still building. Let's make something.", 43, T["ink"], 800, ls=-.6, role="signoff-title")]
    y = 179
    for ln in wrap(OFF_CLOCK_BODY, 26, "regular", 1110, 6):
        o.append(tx(44, y, ln, 26, T["ink2"], role="signoff-body"))
        y += 38
    o += [f'<text x="1156" y="353" text-anchor="end" style="font-size:70px;font-weight:800;letter-spacing:-2.4px;'
          f'fill:{T["ink"]}">ricky <tspan style="fill:url(#{P}-shim)">g.</tspan></text>',
          tx(44, 330, "Auckland, New Zealand", 20, T["ink3"], mono=True, role="signoff-meta"),
          "</svg>"]
    return "".join(o)


def phone_card(T, P, height, title, extra_css=""):
    return [svg_open(P, 600, height, title, css(P, extra_css)),
            "<defs>", defs_common(T, P), "</defs>",
            card(T, P, 7, 7, 586, height - 14)]


def post_phone(T, P, post, cover, now):
    _, date, category = post_meta(post)
    meta = wrap(f"{date} · {category}", 32, "mono", 532, 3)
    title = wrap(post["title"], 44, "bold", 532, 6)
    excerpt = wrap(post.get("content_text", ""), 40, "regular", 532, 6)
    title_y = 342 + len(meta) * 44
    excerpt_y = title_y + len(title) * 58 + 16
    cta_y = excerpt_y + len(excerpt) * 53 + 26
    o = phone_card(T, P, math.ceil(cta_y + 42), f"Latest post: {post['title']}")
    o.append(cover_art(P, "phone", cover.get("topic", ""), cover.get("line", ""),
                       24, 24, 552, 237, 16, cover.get("b64")))
    for index, line in enumerate(meta):
        o.append(tx(32, 313 + index * 44, line, 32, T["ink3"], mono=True, role="post-meta"))
    for index, line in enumerate(title):
        o.append(tx(32, title_y + index * 58, line, 44, T["ink"], 750, ls=-.3, role="post-title"))
    for index, line in enumerate(excerpt):
        o.append(tx(32, excerpt_y + index * 53, line, 40, T["ink2"], role="post-body"))
    o += [tx(32, cta_y, "Read on clouddev.blog →", 32, f"url(#{P}-brand)", 750, role="post-link"), "</svg>"]
    return "".join(o)


def label_phone(T, P, key):
    number, title, meta = LABELS[key]
    o = [svg_open(P, 600, 196, f"{title}: {meta}", css(P)),
         "<defs>", defs_common(T, P), "</defs>",
         tx(14, 60, number, 34, f"url(#{P}-brand)", 700, mono=True, role="section-number"),
         tx(86, 63, title, 48, T["ink"], 750, role="section-title")]
    for index, line in enumerate(wrap(meta, 32, "mono", 570, 2)):
        o.append(tx(14, 116 + index * 43, line, 32, T["ink3"], mono=True, role="section-meta"))
    o += [f'<rect x="14" y="184" width="572" height="2" style="fill:url(#{P}-brand)"/>', "</svg>"]
    return "".join(o)


def tile_agt_phone(T, P, st):
    elements = [kicker(T, 34, 64, "NOW BUILDING", pulse=True, P=P, size=32)]
    for index, line in enumerate(("microsoft/", "agent-governance-toolkit")):
        elements.append(tx(34, 125 + index * 44, line, 32, T["ink3"], mono=True, role="project-meta"))
    title = wrap("Agent Governance Toolkit", 56, "bold", 532, 3)
    for index, line in enumerate(title):
        elements.append(tx(32, 237 + index * 70, line, 56, T["ink"], 800, ls=-.6, role="project-title"))
    y = 237 + len(title) * 70 + 18
    for line in wrap("Policy enforcement, zero-trust identity, execution sandboxing and reliability "
                     "engineering for autonomous AI agents.", 40, "regular", 530, 12):
        elements.append(tx(34, y, line, 40, T["ink2"], role="project-body"))
        y += 53
    y += 8
    x = 34
    for label_text in (f"★ {st['agt_stars']}" if st["agt_stars"] is not None else "— stars",
                       f"{st['agt_forks']} forks" if st["agt_forks"] is not None else "— forks", "Python"):
        item, width = chip_box(T, x, y, label_text, size=32, h=54)
        if x + width > 568 and x != 34:
            x, y = 34, y + 72
            item, width = chip_box(T, x, y, label_text, size=32, h=54)
        elements.append(item)
        x += width + 9
    terminal_y = y + 88
    elements.append(f'<rect x="32" y="{terminal_y}" width="536" height="352" rx="16" style="fill:{T["term"]};'
             f'stroke:{T["termline"]};stroke-width:1.2"/>')
    elements.append(tx(52, terminal_y + 52, "agt · policy illustration", 32, T["termdim"],
                       mono=True, role="terminal-meta"))
    rows = [("allow", "web.search"), ("allow", "repo.read"), ("deny", "shell.exec"),
            ("allow", "pr.create"), ("audit", "4 calls, signed")]
    for index, (decision, action) in enumerate(rows):
        y = terminal_y + 113 + index * 48
        colour = {"allow": T["tgreen"], "deny": T["trose"], "audit": T["tviolet"]}[decision]
        elements.append(tx(52, y, decision, 32, colour, 700, mono=True, role="terminal-body"))
        elements.append(tx(182, y, action, 32, T["termink"], mono=True, role="terminal-body"))
    y = terminal_y + 414
    for line in wrap("OWASP Agentic Top 10 · project reports 10/10", 32, "regular", 532, 3):
        elements.append(tx(34, y, line, 32, T["ink3"], role="project-meta"))
        y += 44
    o = phone_card(T, P, math.ceil(y + 20), "Agent Governance Toolkit: policy enforcement, zero-trust identity and audit.")
    return "".join(o + elements + ["</svg>"])


def stack_phone(T, P):
    sections = list(STACK["layers"]) + [(STACK["cross"][0], STACK["cross"][2])]
    elements, y = [], 62
    for name, chips in sections:
        elements.append(tx(32, y, name, 46, T["ink"], 750, role="stack-heading"))
        y += 36
        x = 32
        for name, colour in chips:
            width = 28 + tw(name, 40, "regular") + (16 if colour else 0)
            if x + width > 568 and x > 32:
                x, y = 32, y + 80
            if width > 536:
                lines = wrap(name, 40, "regular", 480, 3)
                height = 24 + len(lines) * 52
                elements.append(f'<rect x="32" y="{y}" width="536" height="{height}" rx="20" '
                                f'style="fill:{T["chip"]};stroke:{T["chipline"]};stroke-width:1.1"/>')
                if colour:
                    elements.append(f'<circle cx="52" cy="{y + 30}" r="6" style="fill:{colour}"/>')
                for index, line in enumerate(lines):
                    elements.append(tx(70, y + 48 + index * 52, line, 40, T["ink2"], role="stack-body"))
                y += height + 16
                x = 32
            else:
                item, width = chip_box(T, x, y, name, dot=colour, size=40, h=62, pad=14, mono=False)
                elements.append(item)
                x += width + 10
        y += 116
    for line in wrap("every tool call: identity › policy › audit", 32, "regular", 536, 3):
        elements.append(tx(32, y, line, 32, T["ink2"], role="stack-meta"))
        y += 46
    height = math.ceil(y + 22)
    o = phone_card(T, P, height, "Stack: agents, cloud, languages and governance across every layer.")
    o += elements
    o.append("</svg>")
    return "".join(o)


def pulse_phone(T, P, st):
    if st["mix"] is None or st["contributions"] is None:
        return activity_notice(T, P, st, phone=True)
    elements = [kicker(T, 34, 62, "LAST 12 MONTHS", pulse=True, P=P, size=32),
                tx(32, 198, f"{st['contributions']:,}", 116, f"url(#{P}-brand)", 800, ls=-3, role="pulse-count")]
    y = 261
    for line in wrap("contributions in the last year", 40, "regular", 532, 3):
        elements.append(tx(34, y, line, 40, T["ink2"], role="pulse-body"))
        y += 53
    y += 32
    total = sum(st["mix"].values())
    segments = (("Commits", "commits", "green"), ("Pull requests", "prs", "teal"),
                ("Issues", "issues", "violet"), ("Code reviews", "reviews", "magenta"))
    for index, (name, key, colour) in enumerate(segments):
        elements.append(f'<circle cx="43" cy="{y - 8}" r="7" style="fill:{T[colour]}"/>')
        elements.append(tx(62, y, name, 40, T["ink2"], role="pulse-body"))
        percentage = round(100 * st["mix"][key] / total) if total else 0
        elements.append(tx(562, y, f"{st['mix'][key]:,} · {percentage}%", 36, T["ink"], 700,
                           anchor="end", role="pulse-body"))
        y += 62
    y += 16
    for line in wrap("Public repository activity", 32, "regular", 532, 3):
        elements.append(tx(34, y, line, 32, T["ink3"], role="pulse-meta"))
        y += 45
    values = ((st["repos"], "public repositories"), (st["contributed_to"], "public repos contributed to"),
              (st["posts"], "published blog posts"))
    for number, caption in values:
        y += 76
        for line in wrap(metric(number), 62, "bold", 532, 2):
            elements.append(tx(32, y, line, 62, T["ink"], 800, role="pulse-count"))
            y += 69
        for line in wrap(caption, 40, "regular", 532, 3):
            elements.append(tx(34, y, line, 40, T["ink2"], role="pulse-body"))
            y += 53
    o = phone_card(T, P, math.ceil(y + 20), f"GitHub activity: {st['contributions']:,} contributions in the last year.")
    return "".join(o + elements + ["</svg>"])


def activity_notice(T, P, st, phone=False):
    width, size = (600, 40) if phone else (1200, 26)
    elements = [tx(32, 88, "GitHub activity", 48, T["ink"], 750, role="pulse-title")]
    lines = [f"Contributions in the last year: {metric(st['contributions'], True)}",
             f"Public repositories: {metric(st['repos'])} · blog posts: {metric(st['posts'])}",
             "Activity breakdown is currently unavailable."]
    y = 159
    for text in lines:
        for line in wrap(text, size, "regular", width - 64, 4):
            elements.append(tx(32, y, line, size, T["ink2"], role="pulse-body"))
            y += size * 1.4
        y += 10
    height = math.ceil(y + 24)
    o = [svg_open(P, width, height, "GitHub activity. Some counts are currently unavailable.",
                  css(P)), "<defs>", defs_common(T, P), "</defs>", card(T, P, 7, 7, width - 14, height - 14)]
    return "".join(o + elements + ["</svg>"])


def signoff_phone(T, P, st):
    elements = [kicker(T, 34, 66, "OFF THE CLOCK", "magenta", size=32)]
    y = 143
    for line in wrap("Still building. Let's make something.", 52, "bold", 532, 3):
        elements.append(tx(32, y, line, 52, T["ink"], 800, ls=-.4, role="signoff-title"))
        y += 65
    y += 16
    for line in wrap(OFF_CLOCK_BODY, 40, "regular", 532, 20):
        elements.append(tx(34, y, line, 40, T["ink2"], role="signoff-body"))
        y += 53
    y += 40
    elements.append(tx(34, y, "ricky g.", 60, f"url(#{P}-brand)", 800, ls=-1.5))
    y += 61
    for line in wrap("Auckland, New Zealand", 32, "regular", 532, 3):
        elements.append(tx(34, y, line, 32, T["ink3"], role="signoff-meta"))
        y += 45
    o = phone_card(T, P, math.ceil(y + 20), OFF_CLOCK_TEXT)
    return "".join(o + elements + ["</svg>"])


def snake(T, P, days):
    if not days:
        return (svg_open(P, 1200, 190, "GitHub contribution calendar is currently unavailable.", css(P))
                + "<defs>" + defs_common(T, P) + "</defs>" + card(T, P, 7, 7, 1186, 176)
                + tx(32, 70, "Contribution calendar unavailable", 35, T["ink"], 750)
                + tx(32, 120, "The contribution calendar is currently unavailable. Please check back later.", 23, T["ink2"])
                + "</svg>")
    first = datetime.fromisoformat(days[0]["date"]).date()
    origin = first - timedelta(days=(first.weekday() + 1) % 7)
    columns = (datetime.fromisoformat(days[-1]["date"]).date() - origin).days // 7 + 1
    width, height, cell = 1200, 260, 19
    gap = min(22, 1100 / columns)
    colours = [T["track"], "#0f5a45", "#14a38a", "#8b6cf0", "#e879f9"] if T["ink"] == "#eef1ff" else [
        T["track"], "#a7f3d0", "#5eead4", "#a78bfa", "#d946ef"]
    extra = """
@keyframes %P-snake{from{transform:translateX(0)}to{transform:translateX(920px)}}
.%P .snake{animation:%P-snake 28s linear infinite alternate}
"""
    o = [svg_open(P, width, height, "A playful snake over the actual public GitHub contribution calendar.",
                  css(P, extra)), "<defs>", defs_common(T, P), "</defs>", card(T, P, 7, 7, 1186, 246),
         tx(32, 42, "REAL CONTRIBUTIONS · PLAYFUL PIXELS", 20, T["ink3"], 700, mono=True)]
    for day in days:
        delta = (datetime.fromisoformat(day["date"]).date() - origin).days
        x, y = 32 + delta // 7 * gap, 64 + delta % 7 * 23
        o.append(f'<rect x="{x:.1f}" y="{y}" width="{cell}" height="19" rx="4" '
                 f'style="fill:{colours[day["level"]]}"><title>{esc(day["date"])}: '
                 f'{day["count"]} contributions</title></rect>')
    body = "".join(f'<rect x="{32 + index * 21}" y="133" width="19" height="19" rx="5" '
                   f'style="fill:{T["gold"]};opacity:{1 - index * .12}"/>' for index in range(5))
    o += [f'<g class="snake">{body}</g>', "</svg>"]
    return "".join(o)


# --------------------------------------------------------------------------- readme
def metric(value, commas=False):
    if value is None:
        return "unavailable"
    return f"{value:,}" if commas and isinstance(value, int) else str(value)


def picture(base, alt, width, href=None, phone=False):
    sources = motion_sources(base, phone)
    if phone:
        sources += (f'<source media="(prefers-color-scheme: dark) and (max-width: 600px)" '
                    f'srcset="{base}-phone-dark.svg"><source media="(max-width: 600px)" '
                    f'srcset="{base}-phone-light.svg">')
    image = (f'<picture>{sources}<source media="(prefers-color-scheme: dark)" srcset="{base}-dark.svg">'
             f'<img src="{base}-light.svg" width="{esc(width)}" alt="{esc(alt)}"></picture>')
    return f'<a href="{esc(href)}">{image}</a>' if href else image


def motion_sources(base, phone=False):
    sources = []
    if phone:
        for theme in ("dark", "light"):
            sources.append(f'<source media="(prefers-color-scheme: {theme}) and (max-width: 600px) '
                           f'and (prefers-reduced-motion: no-preference)" srcset="{base}-phone-{theme}.svg#motion">')
    for theme in ("dark", "light"):
        size = " and (min-width: 601px)" if phone else ""
        sources.append(f'<source media="(prefers-color-scheme: {theme}){size} '
                       f'and (prefers-reduced-motion: no-preference)" srcset="{base}-{theme}.svg#motion">')
    return "".join(sources)


def writing_block(feed, stats):
    items = feed["items"]
    first = items[0]
    alt = f"{first['title']}. Published {post_meta(first)[1]}. {first['content_text']}"
    lines = ['<p align="center">',
             picture("assets/generated/post-1", alt, "99%", first["url"], phone=True) + "<br>"]
    row = "".join(picture(f"assets/generated/post-{index}",
                         f"{item['title']}. Published {post_meta(item)[1]}. {item['content_text']}",
                         "275", item["url"])
                  for index, item in enumerate(items[1:], 2))
    archive_alt = (f"Browse {stats['posts']} published posts since {stats['first_year']} on clouddev.blog."
                   if stats["posts"] is not None else "Browse all published articles on clouddev.blog. Count unavailable.")
    row += picture("assets/generated/archive", archive_alt, "275", "https://clouddev.blog/archives/")
    lines += [row, "</p>", ""]
    for item in items:
        lines.append(f'<h3><a href="{esc(item["url"])}">{esc(item["title"])}</a></h3>')
        lines.append(f'<p><strong>{post_meta(item)[1]}</strong><br>{esc(item["content_text"])}</p>')
    count = f' {stats["posts"]}' if stats["posts"] is not None else ""
    lines.append(f'<p><a href="https://clouddev.blog/archives/">Explore all{count} published articles →</a></p>')
    return "\n".join(lines)


def projects_block(stats):
    agt = "https://github.com/microsoft/agent-governance-toolkit"
    hub = "https://github.com/Ricky-G/azure-scenario-hub"
    return ('<p align="center">\n'
            + picture("assets/generated/tile-agt",
                      f"Agent Governance Toolkit: {metric(stats['agt_stars'])} stars, {metric(stats['agt_forks'])} forks. "
                      "Policy enforcement, identity, sandboxing and audit for AI agents. "
                      "The project reports ten OWASP Agentic Top 10 risks covered; the terminal is illustrative.",
                      "550", agt, phone=True)
            + picture("assets/generated/tile-hub",
                      f"Azure Scenario Hub: {metric(stats['hub_stars'])} stars. APIM benchmark: 882 versus 874 "
                      "requests per second, 146 versus 153 ms p95, zero errors across 1.67 million requests.",
                      "275", hub) + "<br>\n"
            + picture("assets/tile-docker", "docker-ai-models: prebuilt Dockerfiles for popular AI models; "
                      "the terminal is an illustration.", "275", "https://github.com/Ricky-G/docker-ai-models")
            + picture("assets/tile-ledgerboard", "ledgerboard: local-first Markdown Kanban for VS Code "
                      "with conflict-safe autosave; the moving card is an illustration.",
                      "275", "https://github.com/Ricky-G/ledgerboard")
            + picture("assets/tile-arcade", "Play snake and minesweeper on ricky-g.com.", "275", "https://ricky-g.com")
            + "</p>\n\n"
            f'<h3><a href="{agt}">Agent Governance Toolkit</a></h3>\n'
            '<p>Policy enforcement, zero-trust '
            'identity, execution sandboxing and reliability engineering for autonomous AI agents. '
            'The project reports OWASP Agentic Top 10 coverage.</p>\n'
            f'<h3><a href="{hub}">Azure Scenario Hub</a></h3>\n'
            '<p>Deployable Azure and Microsoft AI '
            'scenarios, runnable demos and benchmarks. '
            '<a href="https://clouddev.blog/Azure/APIM/'
            'apim-backend-fan-out-shared-backend-rewrite-uri-vs-one-backend-per-api-a-real-benchmark/">'
            'Read the APIM benchmark and its methodology →</a></p>\n'
            '<p><a href="https://github.com/Ricky-G/docker-ai-models">docker-ai-models</a> · '
            '<a href="https://github.com/Ricky-G/ledgerboard">ledgerboard</a> · '
            '<a href="https://ricky-g.com">The mini arcade</a></p>')


def pulse_block(stats):
    mix = stats["mix"]
    description = (f"GitHub activity: {metric(stats['contributions'], True)} contributions in the last year. "
                   f"{metric(stats['repos'])} public repositories; {metric(stats['posts'])} published blog posts.")
    activity = ("Activity breakdown is currently unavailable."
                if mix is None else
                f"Public-repository activity: {mix['commits']} commits, {mix['prs']} pull requests, "
                f"{mix['issues']} issues and {mix['reviews']} reviews across "
                f"{metric(stats['contributed_to'])} public repositories.")
    return ('<p align="center">' + picture("assets/generated/pulse", description + " " + activity,
            "99%", phone=True) + '</p>\n\n'
            f'<p><strong>{metric(stats["contributions"], True)} contributions in the last year</strong> '
            f'· {metric(stats["repos"])} public repositories · '
            f'{metric(stats["posts"])} published articles.</p>\n'
            f'<p>{esc(activity)}</p>')


def signoff_block(stats):
    return ('<p align="center">' + picture("assets/generated/signoff",
            f"{OFF_CLOCK_TEXT} Auckland, New Zealand.",
            "99%", "https://www.linkedin.com/in/rickygummadi", phone=True) + '</p>\n\n'
            f'<p align="center">{esc(OFF_CLOCK_TEXT)}</p>\n\n'
            '<p align="center">'
            '<a href="https://www.linkedin.com/in/rickygummadi">Let’s make something →</a></p>')


def replace_block(source, name, body, initialize=False):
    prefix, end = f"<!-- {name}:START", f"<!-- {name}:END -->"
    if source.count(prefix) != 1 or source.count(end) != 1:
        raise ValueError(f"{name}: exactly one start and end marker is required.")
    start = source.index(prefix)
    header_end = source.find("-->", start) + 3
    stop = source.index(end)
    if header_end < start + 3 or stop < header_end:
        raise ValueError(f"{name}: invalid generated boundary.")
    canonical = f"<!-- {name}:START -->"
    region = source[header_end:stop]
    owned = re.fullmatch(r"\n<!-- GENERATED:sha256=([a-f0-9]{64}) -->\n(.*)\n", region, re.DOTALL)
    if owned:
        if hashlib.sha256(owned[2].encode()).hexdigest() != owned[1]:
            raise ValueError(f"{name}: generated content was edited; refusing to overwrite it.")
    elif not initialize:
        raise ValueError(f"{name}: no ownership checksum. First adoption requires --initialize.")
    if source[start:header_end] != canonical and not initialize:
        raise ValueError(f"{name}: start marker changed.")
    digest = hashlib.sha256(body.encode()).hexdigest()
    replacement = f"{canonical}\n<!-- GENERATED:sha256={digest} -->\n{body}\n{end}"
    return source[:start] + replacement + source[stop + len(end):]


# --------------------------------------------------------------------------- build
def build_all(T, theme, data, covers_fn):
    stats, feed, now = data["stats"], data["feed"], data["now"]
    items = feed["items"][:3]
    out = {
        "hero": hero_desktop(T, f"hd{theme[0]}"),
        "hero-phone": hero_phone(T, f"hp{theme[0]}"),
        "intro": introduction(T, f"in{theme[0]}"),
        "intro-phone": introduction(T, f"inp{theme[0]}", phone=True),
        "label-writing": label(T, f"lw{theme[0]}", "writing"),
        "label-writing-phone": label_phone(T, f"lwp{theme[0]}", "writing"),
        "label-building": label(T, f"lb{theme[0]}", "building"),
        "label-building-phone": label_phone(T, f"lbp{theme[0]}", "building"),
        "label-stack": label(T, f"ls{theme[0]}", "stack"),
        "label-stack-phone": label_phone(T, f"lsp{theme[0]}", "stack"),
        "label-pulse": label(T, f"lp{theme[0]}", "pulse"),
        "label-pulse-phone": label_phone(T, f"lpp{theme[0]}", "pulse"),
        "label-arcade": label(T, f"la{theme[0]}", "arcade"),
        "label-arcade-phone": label_phone(T, f"lap{theme[0]}", "arcade"),
        "stack": stack(T, f"st{theme[0]}"),
        "stack-phone": stack_phone(T, f"stp{theme[0]}"),
        "generated/archive": archive_card(T, f"ar{theme[0]}", stats),
        "generated/tile-agt": tile_agt(T, f"ta{theme[0]}", stats),
        "generated/tile-agt-phone": tile_agt_phone(T, f"tap{theme[0]}", stats),
        "generated/tile-hub": tile_hub(T, f"th{theme[0]}", stats),
        "tile-docker": tile_docker(T, f"td{theme[0]}", stats),
        "tile-ledgerboard": tile_ledger(T, f"tl{theme[0]}", stats),
        "tile-arcade": tile_arcade(T, f"tc{theme[0]}", stats),
        "generated/pulse": pulse(T, f"pu{theme[0]}", stats),
        "generated/pulse-phone": pulse_phone(T, f"pup{theme[0]}", stats),
        "generated/signoff": signoff(T, f"so{theme[0]}", stats),
        "generated/signoff-phone": signoff_phone(T, f"sop{theme[0]}", stats),
    }
    for index, post in enumerate(items, 1):
        renderer = post_featured if index == 1 else post_mini
        out[f"generated/post-{index}"] = renderer(T, f"p{index}{theme[0]}", post, covers_fn(post), now)
    out["generated/post-1-phone"] = post_phone(T, f"p1p{theme[0]}", items[0], covers_fn(items[0]), now)
    for k in PILLS:
        out[f"pill-{k}"] = pill(T, f"pl{k[0]}{theme[0]}", k)
    return out


STAND_IN_COVERS = {  # used when a cover image cannot be fetched (and for the offline concept)
    "github-copilot-agents": ("Copilot", "CLI vs Local vs Cloud"),
    "mcp-goes-stateless": ("MCP", "Goes stateless"),
    "agents-md-vs": ("AGENTS.md", "Which context file, when"),
}


def stand_in(post):
    for k, (topic, line) in STAND_IN_COVERS.items():
        if k in post["url"]:
            return {"topic": topic, "line": line}
    first = post["title"].split(":")[0].split(" ")[0]
    return {"topic": first[:12], "line": "clouddev.blog"}


def main(argv=None):
    from profile_data import ProfileError, read_inputs

    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="do not call the network; use data/*.json")
    ap.add_argument("--now", help="override 'now' (ISO 8601), for reproducible renders")
    ap.add_argument("--github-cli", action="store_true", help="use existing gh login for read-only public API requests")
    ap.add_argument("--initialize", action="store_true", help="adopt the reviewed starter and initialize ownership checks")
    ap.add_argument("--preserve-snake", action="store_true", help="validate and retain freshly generated snk images")
    args = ap.parse_args(argv)
    lock = ROOT / ".profile-build.lock"
    try:
        handle = lock.open("x", encoding="ascii")
    except FileExistsError:
        print("Profile build is locked. Confirm the previous process stopped before removing its lock.", file=sys.stderr)
        return 1
    try:
        with handle:
            handle.write(str(os.getpid()))
            handle.flush()
            baseline = capture_baseline()
            state_path = DATA / "build-state.json"
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
            verify_ownership(state, baseline, args.initialize, args.preserve_snake)
            now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
            if now.tzinfo is None:
                raise ValueError("--now needs an explicit timezone.")
            if args.offline and not args.now and state.get("rendered_at"):
                now = datetime.fromisoformat(state["rendered_at"])
            now = now.astimezone(NZ)
            inputs = read_inputs(ROOT, now=now, offline=args.offline, github_cli=args.github_cli)
            for warning in inputs.warnings:
                print(f"WARNING: {warning}", file=sys.stderr)
                if os.environ.get("GITHUB_ACTIONS") == "true":
                    safe = warning.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
                    print(f"::warning::{safe}")
            if inputs.warnings and os.environ.get("GITHUB_STEP_SUMMARY"):
                with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
                    summary.write("### Retained snapshot / unavailable provider warnings\n\n")
                    for warning in inputs.warnings:
                        summary.write(f"- {warning.replace(chr(10), ' ').replace(chr(13), ' ')}\n")
            stats = dict(inputs.stats)
            stamp = datetime.fromisoformat(state.get("rendered_at", now.isoformat())).astimezone(NZ)
            set_stamp(stats, stamp)
            def covers_fn(post):
                cover = stand_in(post)
                if post["url"] in inputs.covers:
                    cover["b64"] = inputs.covers[post["url"]]
                else:
                    cover["line"] = "Cover unavailable · clouddev.blog"
                return cover

            data = {"stats": stats, "feed": inputs.feed, "now": now}
            pending = dict(inputs.pending)
            for theme, tokens in THEMES.items():
                for name, svg in build_all(tokens, theme, data, covers_fn).items():
                    pending[f"assets/{name}-{theme}.svg"] = svg.encode()
                snake_name = f"assets/generated/snake-{theme}.svg"
                if args.preserve_snake or state.get("snake_source") == "snk":
                    existing = (ROOT / snake_name).read_text(encoding="utf-8")
                    pending[snake_name] = stop_snake_motion(existing).encode()
                else:
                    pending[snake_name] = snake(tokens, f"sn{theme[0]}", inputs.calendar).encode()
            old_readme = (ROOT / "README.md").read_bytes().decode("utf-8")
            readme = old_readme
            bodies = {"WRITING": writing_block(inputs.feed, stats), "PROJECTS": projects_block(stats),
                      "PULSE": pulse_block(stats), "SIGNOFF": signoff_block(stats)}
            for name, body in bodies.items():
                readme = replace_block(readme, name, body, args.initialize)
            pending["README.md"] = readme.encode()
            content_baseline = dict(baseline)
            if args.preserve_snake:
                # The provider replaced these files before the build; compare with their last published bytes.
                content_baseline.update({name: value for name, value in state.get("files", {}).items()
                                         if name.startswith("assets/generated/snake-")})
            content_changed = any(content_baseline.get(name) != digest(content) for name, content in pending.items()
                                  if "signoff-" not in name and name != "README.md")
            content_changed |= readme != old_readme
            if content_changed:
                set_stamp(stats, now)
                for theme, tokens in THEMES.items():
                    pending[f"assets/generated/signoff-{theme}.svg"] = signoff(tokens, f"so{theme[0]}", stats).encode()
                    pending[f"assets/generated/signoff-phone-{theme}.svg"] = signoff_phone(tokens, f"sop{theme[0]}", stats).encode()
                pending["README.md"] = replace_block(readme, "SIGNOFF", signoff_block(stats)).encode()
                state = {"rendered_at": now.isoformat(),
                         "snake_source": "snk" if args.preserve_snake or state.get("snake_source") == "snk" else "local-calendar",
                         "files": {name: digest(value) for name, value in pending.items() if name.endswith(".svg")}}
                pending["data/build-state.json"] = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode()
            for name, content in pending.items():
                if name.endswith(".svg"):
                    validate_svg(name, content)
            verify_references(pending["README.md"], pending)
            updates = {name: content for name, content in pending.items() if baseline.get(name) != digest(content)}
            if updates:
                atomic_write(updates, baseline)
                print(f"Profile built: {len(updates)} changed files; public sources and image references validated.")
            else:
                print("Profile unchanged: verified inputs produced an exact no-op.")
            return 0
    except (ProfileError, ValueError, OSError, ET.ParseError) as exc:
        print(f"Profile build FAILED: {exc}. Invalid input is not promoted; previous valid output is retained.", file=sys.stderr)
        return 1
    finally:
        lock.unlink(missing_ok=True)


def digest(content):
    return hashlib.sha256(content).hexdigest()


def capture_baseline():
    paths = [ROOT / "README.md", ROOT / "requirements.txt"]
    for directory in (ASSETS, DATA, ROOT / "scripts"):
        paths.extend(path for path in directory.rglob("*") if path.is_file() and "__pycache__" not in path.parts)
    return {path.relative_to(ROOT).as_posix(): digest(path.read_bytes()) for path in paths if path.exists()}


def verify_ownership(state, baseline, initialize, preserve_snake):
    if not state and not initialize:
        raise ValueError("First adoption requires --initialize; supplied sample data is not trusted automatically.")
    for name, expected in state.get("files", {}).items():
        if preserve_snake and name.startswith("assets/generated/snake-"):
            continue
        if baseline.get(name) != expected:
            raise ValueError(f"{name}: generated asset was edited outside the build; refusing to overwrite.")


def set_stamp(stats, now):
    stats["synced_long"] = f"{now.day} {now.strftime('%b %Y, %H:%M')} {now.tzname()}"
    offset = int(now.utcoffset().total_seconds() // 3600)
    stats["utc"] = f"UTC{offset:+d}"


def stop_snake_motion(svg):
    namespace = "http://www.w3.org/2000/svg"
    ET.register_namespace("", namespace)
    root = ET.fromstring(svg)
    if root.get("id") not in (None, "motion"):
        raise ValueError("Snake root has an unknown fragment identifier.")
    root.set("id", "motion")
    for node in list(root):
        if node.get("id") == "profile-motion":
            root.remove(node)
        elif node.tag == f"{{{namespace}}}style":
            node.text = remove_reduced_media(node.text or "")
    guard = ET.SubElement(root, f"{{{namespace}}}style", {"id": "profile-motion"})
    guard.text = ("svg:not(:target) *{animation:none!important}"
                  "@media(prefers-reduced-motion:reduce){svg:not(:target) *{animation:none!important}}")
    return ET.tostring(root, encoding="unicode")


def remove_reduced_media(stylesheet):
    pattern = re.compile(r"@media\s*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)\s*\{")
    while match := pattern.search(stylesheet):
        index, depth = match.end(), 1
        while index < len(stylesheet) and depth:
            depth += (stylesheet[index] == "{") - (stylesheet[index] == "}")
            index += 1
        if depth:
            raise ValueError("Malformed reduced-motion CSS in snake image.")
        stylesheet = stylesheet[:match.start()] + stylesheet[index:]
    return stylesheet


def validate_svg(name, content):
    root = ET.fromstring(content)
    if root.tag != "{http://www.w3.org/2000/svg}svg":
        raise ValueError(f"{name}: not an SVG image.")
    budget = 200_000 if "hero" in name or "post-" in name else 100_000
    if len(content) >= budget:
        raise ValueError(f"{name}: image exceeds the {budget:,}-byte budget.")
    if b"prefers-reduced-motion" not in content:
        raise ValueError(f"{name}: reduced-motion behavior missing.")
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag in {"script", "foreignObject", "iframe", "animate", "animateTransform", "set"}:
            raise ValueError(f"{name}: unsupported active or motion element {tag}.")
        for key, value in node.attrib.items():
            if key.lower().startswith("on"):
                raise ValueError(f"{name}: event handler not allowed.")
            if key.rsplit("}", 1)[-1] == "href" and not value.startswith(("#", "data:image/jpeg;base64,")):
                raise ValueError(f"{name}: external image/resource reference.")
            if re.search(r"url\(\s*['\"]?(?:https?:|//|data:)", value, re.IGNORECASE):
                raise ValueError(f"{name}: external CSS resource.")
        if tag == "style" and re.search(r"@import|url\(\s*['\"]?(?:https?:|//|data:)", node.text or "", re.IGNORECASE):
            raise ValueError(f"{name}: external CSS resource.")


def verify_references(readme, pending):
    for reference in re.findall(r'(?:src|srcset)="([^"]+)"', readme.decode()):
        reference, _, fragment = reference.partition("#")
        if fragment not in ("", "motion"):
            raise ValueError(f"Unknown image fragment: {fragment}.")
        if not reference.startswith("assets/") or ".." in reference.split("/"):
            raise ValueError(f"README image is not repository-local: {reference}.")
        if reference not in pending and not (ROOT / reference).is_file():
            raise ValueError(f"README image is missing: {reference}.")


def atomic_write(updates, baseline):
    for name in updates:
        path = ROOT / name
        if Path(name).is_absolute() or not path.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError(f"Output path escaped the profile repository: {name}.")
    for name, expected in baseline.items():
        path = ROOT / name
        if not path.is_file() or digest(path.read_bytes()) != expected:
            raise ValueError(f"Concurrent edit detected in {name}; no output written.")
    for name in updates:
        if name not in baseline and (ROOT / name).exists():
            raise ValueError(f"Concurrent new file detected: {name}.")
    originals = {name: (ROOT / name).read_bytes() if (ROOT / name).exists() else None for name in updates}
    written = []
    with tempfile.TemporaryDirectory(prefix=".profile-build-", dir=ROOT) as temporary:
        staged = Path(temporary)
        for name, content in updates.items():
            path = staged / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        try:
            for name, content in updates.items():
                target = ROOT / name
                current = target.read_bytes() if target.exists() else None
                if current != originals[name]:
                    raise ValueError(f"Concurrent edit detected before replacing {name}.")
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged / name, target)
                written.append(name)
        except (OSError, ValueError):
            for name in reversed(written):
                target = ROOT / name
                if digest(target.read_bytes()) != digest(updates[name]):
                    print(f"External edit retained during rollback: {name}.", file=sys.stderr)
                    continue
                if originals[name] is None:
                    target.unlink()
                else:
                    restore = staged / name
                    restore.write_bytes(originals[name])
                    os.replace(restore, target)
            raise


if __name__ == "__main__":
    sys.exit(main())
