"""Validated public inputs and write-free cache proposals for the profile renderer."""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import warnings as warning_control
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any

BLOG = "https://clouddev.blog"
FEED_URL = BLOG + "/latest-posts.json"
ATOM_URL = BLOG + "/atom.xml"
ARCHIVE_URL = BLOG + "/archives/"
CALENDAR_URL = "https://github.com/users/Ricky-G/contributions"
USER_URL = "https://api.github.com/users/Ricky-G"
GRAPHQL_URL = "https://api.github.com/graphql"
PROJECTS = {
    "agt": "microsoft/agent-governance-toolkit",
    "hub": "Ricky-G/azure-scenario-hub",
    "docker": "Ricky-G/docker-ai-models",
    "ledger": "Ricky-G/ledgerboard",
}
TIMEOUT = 12
ATTEMPTS = 2
JSON_LIMIT = 256_000
HTML_LIMIT = 2_000_000
COVER_LIMIT = 8_000_000
JPEG_LIMIT = 100_000
CACHE_FILES = ("feed", "stats", "calendar", "covers")
UTC = timezone.utc


class ProfileError(RuntimeError):
    """An input cannot be trusted; the renderer must retain its existing output."""


@dataclass
class ProfileInputs:
    feed: dict[str, Any]
    stats: dict[str, Any]
    covers: dict[str, str] = field(default_factory=dict)
    calendar: list[dict[str, Any]] = field(default_factory=list)
    pending: dict[str, bytes] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Response:
    body: bytes
    url: str
    content_type: str
    status: int = 200


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json(raw: bytes | str | dict | list) -> Any:
    if isinstance(raw, (dict, list)):
        return raw

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProfileError("JSON input contains duplicate object keys")
            result[key] = value
        return result

    def finite(value):
        raise ProfileError("JSON input contains a non-finite value")

    try:
        return json.loads(
            raw.decode("utf-8") if isinstance(raw, bytes) else raw,
            object_pairs_hook=unique, parse_constant=finite,
        )
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ProfileError("Malformed UTF-8 JSON input") from exc


def _text(value: Any, name: str, limit: int = 1_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ProfileError(f"{name} must be nonempty plain text of at most {limit} characters")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ProfileError(f"{name} contains invalid Unicode") from exc
    if any(unicodedata.category(ch) in {"Cc", "Cf"} and ch not in "\t\n\r" for ch in value):
        raise ProfileError(f"{name} contains unsafe control characters")
    if re.search(r"<\s*[/!?a-zA-Z]", value):
        raise ProfileError(f"{name} must be plain text, not HTML")
    return " ".join(unicodedata.normalize("NFC", value).split())


def _instant(value: Any, name: str = "date_published") -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise ProfileError(f"{name} requires a complete ISO 8601 instant with an explicit offset")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if abs(dt.utcoffset() or timedelta()) > timedelta(hours=14):
            raise ValueError("offset exceeds 14 hours")
        return dt.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise ProfileError(f"{name} is not a valid publication instant") from exc


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _blog_url(value: Any, *, article: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 2_000:
        raise ProfileError("Public blog URL must be a bounded string")
    try:
        value.encode("ascii")
        parsed = urllib.parse.urlsplit(value)
    except (UnicodeError, ValueError) as exc:
        raise ProfileError("Public URL must be canonical ASCII HTTPS") from exc
    if (
        parsed.scheme != "https" or parsed.netloc != "clouddev.blog"
        or parsed.query or parsed.fragment or "\\" in value
        or any(ch.isspace() or ord(ch) < 32 for ch in value)
        or not parsed.path.startswith("/") or "//" in parsed.path
    ):
        raise ProfileError("Unsafe or off-origin public blog URL")
    path = parsed.path
    for _ in range(4):
        if re.search(r"%(?![0-9a-fA-F]{2})", path):
            raise ProfileError("Malformed URL encoding")
        if re.search(r"%(?:2f|5c|00)", path, re.IGNORECASE):
            raise ProfileError("Encoded path separators are not canonical")
        try:
            decoded = urllib.parse.unquote(path, errors="strict")
        except UnicodeError as exc:
            raise ProfileError("URL encoding is not valid UTF-8") from exc
        if any(part in {".", ".."} for part in decoded.split("/")) or "\\" in decoded:
            raise ProfileError("URL traversal is not allowed")
        if any(unicodedata.category(ch) in {"Cc", "Cf"} for ch in decoded):
            raise ProfileError("URL contains unsafe control characters")
        if decoded == path:
            break
        path = decoded
    else:
        raise ProfileError("Excessively encoded URL")
    if article and (
        parsed.path == "/" or not parsed.path.endswith("/")
        or parsed.path.split("/")[1].lower() in {
            "archives", "page", "tags", "categories", "about", "cookies", "img", "js", "css"
        }
    ):
        raise ProfileError("Feed URL is not a canonical article URL")
    return value


def validate_feed(raw: Any) -> dict[str, Any]:
    """Normalize JSON Feed 1.1, retaining only explicitly public display fields."""
    feed = _json(raw)
    if not isinstance(feed, dict) or feed.get("version") != "https://jsonfeed.org/version/1.1":
        raise ProfileError("Expected JSON Feed 1.1")
    if feed.get("home_page_url") != BLOG + "/" or feed.get("feed_url") != FEED_URL:
        raise ProfileError("Feed identity does not match the canonical public blog")
    items = feed.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 3:
        raise ProfileError("Latest-posts feed must contain one to three articles")
    normalized = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ProfileError("Feed article must be an object")
        url = _blog_url(item.get("url"), article=True)
        if item.get("id") != url or url in seen:
            raise ProfileError("Feed article IDs must equal unique canonical URLs")
        seen.add(url)
        result = {
            "id": url, "url": url,
            "title": _text(item.get("title"), "article title", 400),
            "date_published": _iso(_instant(item.get("date_published"))),
            "content_text": _text(item.get("content_text"), "content_text", 4_000),
        }
        if "image" in item:
            result["image"] = _blog_url(item["image"])
        normalized.append(result)
    normalized.sort(key=lambda it: (-_instant(it["date_published"]).timestamp(), it["url"]))
    return {
        "version": "https://jsonfeed.org/version/1.1",
        "title": _text(feed.get("title"), "feed title", 200),
        "home_page_url": BLOG + "/", "feed_url": FEED_URL, "items": normalized,
    }


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if new.scheme != "https" or new.netloc != old.netloc or req.has_header("Authorization"):
            raise ProfileError("Public fetch attempted an unsafe redirect")
        if new.username or new.password or new.fragment:
            raise ProfileError("Public fetch attempted a noncanonical redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch(url: str, *, limit: int, accept: str, body: bytes | None = None, token: str | None = None) -> Response:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc not in {"clouddev.blog", "github.com", "api.github.com"}:
        raise ProfileError("Fetch target is not an approved public origin")
    if parsed.username or parsed.password or parsed.fragment:
        raise ProfileError("Fetch target is not canonical")
    headers = {"User-Agent": "Ricky-G-profile-public-inputs/1", "Accept": accept, "Accept-Encoding": "identity"}
    if token:
        if url != GRAPHQL_URL:
            raise ProfileError("Authentication is restricted to the GitHub GraphQL endpoint")
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers)
    opener = urllib.request.build_opener(_SafeRedirect())
    for attempt in range(ATTEMPTS):
        try:
            with opener.open(request, timeout=TIMEOUT) as response:
                if response.status != 200:
                    raise ProfileError(f"Public endpoint returned HTTP {response.status}")
                length = response.headers.get("Content-Length")
                if length and (not length.isdecimal() or int(length) > limit):
                    raise ProfileError("Public response exceeds its size budget")
                if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
                    raise ProfileError("Unexpected compressed public response")
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise ProfileError("Public response exceeds its size budget")
                if response.geturl() != url:
                    raise ProfileError("Public endpoint redirected instead of serving its canonical URL")
                return Response(data, url, response.headers.get_content_type())
        except urllib.error.HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt + 1 == ATTEMPTS:
                raise ProfileError(f"Public endpoint {url} returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt + 1 == ATTEMPTS:
                raise ProfileError(f"Public fetch failed for {url} after {ATTEMPTS} bounded attempts") from exc
        if attempt + 1 < ATTEMPTS:
            time.sleep(0.25 * (attempt + 1))
    raise ProfileError("Public fetch did not produce a response")


def _response(response: Response, url: str, types: set[str]) -> bytes:
    if response.status != 200 or response.url != url or response.content_type not in types:
        raise ProfileError(f"Public readiness failed for {url}: expected canonical HTTP 200 and a valid content type")
    return response.body


class _Article(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.canonical: list[str] = []
        self.titles: list[str] = []
        self.headings: list[str] = []
        self.active: str | None = None
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "link" and "canonical" in values.get("rel", "").lower().split():
            self.canonical.append(values.get("href", ""))
        if tag in {"title", "h1"}:
            if self.active:
                raise ProfileError("Malformed nested article title")
            self.active, self.parts = tag, []

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == self.active:
            value = _text("".join(self.parts), "public article heading", 800)
            (self.titles if tag == "title" else self.headings).append(value)
            self.active, self.parts = None, []


def _article_receipt(response: Response, item: dict[str, Any]) -> dict[str, Any]:
    body = _response(response, item["url"], {"text/html"})
    try:
        parser = _Article()
        parser.feed(body.decode("utf-8"))
        parser.close()
    except (UnicodeError, ValueError) as exc:
        raise ProfileError("Malformed public article HTML") from exc
    if len(parser.canonical) != 1 or _blog_url(parser.canonical[0], article=True) != item["url"]:
        raise ProfileError("Article canonical identity does not match the selected feed URL")
    expected = item["title"]
    title_match = len(parser.titles) == 1 and (
        parser.titles[0] == expected or parser.titles[0].startswith(expected + " | ")
        or parser.titles[0].startswith(expected + " - ")
    )
    if not title_match or (parser.headings and expected not in parser.headings):
        raise ProfileError("Public article title does not match the selected feed title")
    return {"url": item["url"], "canonical": item["url"], "title": expected, "status": 200, "raw_sha256": _hash(body)}


def _pillow():
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise ProfileError("Pillow is required to validate and cache raster covers") from exc
    return Image, ImageOps


def _jpeg(data: bytes, *, cached: bool = False, content_type: str | None = None) -> bytes:
    Image, ImageOps = _pillow()
    if not data or len(data) >= (JPEG_LIMIT if cached else COVER_LIMIT + 1):
        raise ProfileError("Raster cover exceeds its size budget")
    try:
        with warning_control.catch_warnings():
            warning_control.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                fmt = probe.format
                width, height = probe.size
                if (
                    fmt not in {"JPEG", "PNG", "WEBP"} or width <= 0 or height <= 0
                    or width > 8_192 or height > 8_192 or width * height > 20_000_000
                    or getattr(probe, "n_frames", 1) != 1
                ):
                    raise ProfileError("Cover must be a bounded single-frame PNG, JPEG or WebP")
                if content_type and fmt != {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}.get(content_type):
                    raise ProfileError("Cover MIME type does not match the decoded raster")
                probe.verify()
            with Image.open(io.BytesIO(data)) as original:
                if cached:
                    if fmt != "JPEG" or original.size != (1316, 564) or original.getexif():
                        raise ProfileError("Cached JPEG must be metadata-free and exactly 1316 by 564")
                    original.load()
                    return data
                original.load()
                original = ImageOps.exif_transpose(original)
                if "A" in original.getbands():
                    rgba = original.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, (10, 15, 44, 255))
                    background.alpha_composite(rgba)
                    original = background.convert("RGB")
                else:
                    original = original.convert("RGB")
                image = ImageOps.fit(original, (1316, 564), method=Image.Resampling.LANCZOS)
                for quality in (80, 74, 68, 62, 56, 50, 44, 38, 32, 26):
                    out = io.BytesIO()
                    image.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
                    result = out.getvalue()
                    if len(result) < JPEG_LIMIT:
                        return result
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ProfileError("Cover is not a valid bounded raster image") from exc
    raise ProfileError("Cover cannot fit the JPEG budget without an unacceptable quality reduction")


def _count(value: Any, name: str) -> int:
    if type(value) is not int or not 0 <= value <= 100_000_000:
        raise ProfileError(f"{name} must be a bounded nonnegative integer")
    return value


def _format_k(value: int) -> str:
    return (f"{value / 1000:.1f}k".replace(".0k", "k")) if value >= 1000 else str(value)


def _display_count(value: str, name: str) -> int:
    if value.lower() == "no":
        return 0
    if not re.fullmatch(r"(?:0|[1-9]\d{0,8}|[1-9]\d{0,2}(?:,\d{3}){1,2})", value):
        raise ProfileError(f"{name} is not a valid bounded displayed count")
    return _count(int(value.replace(",", "")), name)


class _Calendar(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cells: list[dict[str, str]] = []
        self.tooltips: dict[str, str] = {}
        self.description: list[str] = []
        self.active: tuple[str, str] | None = None
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "td" and "data-date" in values:
            self.cells.append(values)
        if tag == "tool-tip" or (tag == "h2" and values.get("id") == "js-contribution-activity-description"):
            if self.active:
                raise ProfileError("Malformed contribution markup")
            self.active, self.parts = (tag, values.get("for", "")), []

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if self.active and self.active[0] == tag:
            value = " ".join("".join(self.parts).split())
            if tag == "h2":
                self.description.append(value)
            else:
                target = self.active[1]
                if not target or target in self.tooltips:
                    raise ProfileError("Duplicate or unassociated contribution tooltip")
                self.tooltips[target] = value
            self.active, self.parts = None, []


def _calendar(response: Response, now: datetime) -> tuple[list[dict[str, Any]], int]:
    body = _response(response, CALENDAR_URL, {"text/html"})
    try:
        parser = _Calendar()
        parser.feed(body.decode("utf-8"))
        parser.close()
    except UnicodeError as exc:
        raise ProfileError("Contribution calendar is not UTF-8 HTML") from exc
    if len(parser.description) != 1 or not 350 <= len(parser.cells) <= 378:
        raise ProfileError("Public contribution calendar is incomplete or its markup has changed")
    total = re.fullmatch(r"(No|[\d,]+) contributions? in the last year", parser.description[0], re.IGNORECASE)
    if not total:
        raise ProfileError("Public contribution total could not be verified")
    result = []
    for cell in parser.cells:
        tip = parser.tooltips.get(cell.get("id", ""), "")
        match = re.match(r"^(No|[\d,]+) contributions?\b", tip, re.IGNORECASE)
        level = cell.get("data-level", "")
        if not match or not re.fullmatch(r"[0-4]", level):
            raise ProfileError("Contribution cells require verified count tooltips and levels")
        count = _display_count(match[1], "calendar count")
        if (count == 0) != (level == "0"):
            raise ProfileError("Contribution count and level disagree")
        result.append({"date": cell["data-date"], "count": _count(count, "calendar count"), "level": int(level)})
    result = _validate_calendar(result)
    today = now.astimezone(UTC).date()
    if any(not today - timedelta(days=378) <= date.fromisoformat(c["date"]) <= today for c in result):
        raise ProfileError("Calendar dates are outside the public last-year window")
    return result, _display_count(total[1], "visible contributions")


def _validate_calendar(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) > 378:
        raise ProfileError("Invalid cached contribution calendar")
    result, dates = [], set()
    for cell in raw:
        if not isinstance(cell, dict) or set(cell) != {"date", "count", "level"}:
            raise ProfileError("Malformed contribution cell")
        value = cell["date"]
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ProfileError("Contribution date must be ISO 8601")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ProfileError("Invalid contribution date") from exc
        if value in dates:
            raise ProfileError("Duplicate contribution date")
        dates.add(value)
        count, level = _count(cell["count"], "calendar count"), _count(cell["level"], "calendar level")
        if level > 4 or (count == 0) != (level == 0):
            raise ProfileError("Invalid calendar level/count")
        result.append({"date": value, "count": count, "level": level})
    return sorted(result, key=lambda cell: cell["date"])


class _Archive(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.article_depth = 0
        self.title_tags: list[str] = []
        self.links: set[str] = set()
        self.pages: set[str] = set()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "article":
            self.article_depth += 1
        if self.article_depth and (
            tag in {"h1", "h2", "h3", "h4"} or (tag == "p" and "title" in values.get("class", "").split())
        ):
            self.title_tags.append(tag)
        if tag == "a" and values.get("href"):
            url = urllib.parse.urljoin(BLOG + "/", values["href"])
            if self.article_depth and self.title_tags:
                self.links.add(_blog_url(url, article=True))
            if re.fullmatch(r"https://clouddev\.blog/archives/(?:page/[1-9]\d*/)?", url):
                self.pages.add(_blog_url(url))

    def handle_endtag(self, tag):
        if self.title_tags and self.title_tags[-1] == tag:
            self.title_tags.pop()
        if tag == "article":
            self.article_depth = max(0, self.article_depth - 1)


def _posts() -> tuple[int, int, dict[str, Any]]:
    response = _fetch(ATOM_URL, limit=HTML_LIMIT, accept="application/atom+xml, application/xml, text/xml")
    body = _response(response, ATOM_URL, {"application/atom+xml", "application/xml", "text/xml"})
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise ProfileError("Atom feed must not contain external entities")
    try:
        atom = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ProfileError("Malformed public Atom feed") from exc
    ns = "{http://www.w3.org/2005/Atom}"
    if atom.tag != ns + "feed":
        raise ProfileError("Expected a public Atom feed")
    entries = atom.findall(ns + "entry")
    if not 1 <= len(entries) < 50:
        raise ProfileError("Atom feed is empty or may be truncated at its 50-entry cap")
    urls, years = set(), []
    for entry in entries:
        links = [link.get("href") for link in entry.findall(ns + "link") if link.get("rel", "alternate") == "alternate"]
        if len(links) != 1:
            raise ProfileError("Atom entries require one canonical alternate link")
        url = _blog_url(links[0], article=True)
        if url in urls:
            raise ProfileError("Duplicate public Atom article")
        urls.add(url)
        _text(entry.findtext(ns + "title"), "Atom article title", 400)
        years.append(_instant(entry.findtext(ns + "published"), "Atom publication date").year)
    visited, remaining, archive = set(), {ARCHIVE_URL}, set()
    while remaining:
        if len(visited) >= 8:
            raise ProfileError("Public archive exceeds the bounded pagination limit")
        url = sorted(remaining)[0]
        remaining.remove(url)
        response = _fetch(url, limit=HTML_LIMIT, accept="text/html")
        try:
            parser = _Archive()
            parser.feed(_response(response, url, {"text/html"}).decode("utf-8"))
            parser.close()
        except UnicodeError as exc:
            raise ProfileError("Public archive is not UTF-8") from exc
        if not parser.links:
            raise ProfileError("Public archive article links could not be verified")
        visited.add(url)
        archive.update(parser.links)
        remaining.update(parser.pages - visited)
    if archive != urls:
        raise ProfileError("Atom and public archive disagree about published article count")
    return len(urls), min(years), {"url": ATOM_URL, "archive_url": ARCHIVE_URL, "posts": len(urls), "first_year": min(years)}


GROUPS = {
    "commits": "commitContributionsByRepository",
    "prs": "pullRequestContributionsByRepository",
    "issues": "issueContributionsByRepository",
    "reviews": "pullRequestReviewContributionsByRepository",
}
GRAPHQL_QUERY = """query($login:String!,$from:DateTime!,$to:DateTime!){
  user(login:$login){contributionsCollection(from:$from,to:$to){
    commitContributionsByRepository(maxRepositories:100){repository{id isPrivate} contributions{totalCount}}
    issueContributionsByRepository(maxRepositories:100){repository{id isPrivate} contributions{totalCount}}
    pullRequestContributionsByRepository(maxRepositories:100){repository{id isPrivate} contributions{totalCount}}
    pullRequestReviewContributionsByRepository(maxRepositories:100){repository{id isPrivate} contributions{totalCount}}
  }}
}"""


def _public_mix(raw: Any) -> tuple[dict[str, int], int]:
    payload = _json(raw)
    if not isinstance(payload, dict) or payload.get("errors"):
        raise ProfileError("GitHub public contribution query was unavailable")
    try:
        collection = payload["data"]["user"]["contributionsCollection"]
    except (KeyError, TypeError) as exc:
        raise ProfileError("GitHub public contribution query returned no user collection") from exc
    if not isinstance(collection, dict):
        raise ProfileError("Invalid public contribution collection")
    mix, public_repositories = {}, set()
    for key, group in GROUPS.items():
        rows = collection.get(group)
        if not isinstance(rows, list) or len(rows) >= 100:
            raise ProfileError("Contribution repository cap may truncate the public last-year snapshot")
        total = 0
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("repository"), dict):
                raise ProfileError("Malformed contribution repository")
            repository = row["repository"]
            if type(repository.get("isPrivate")) is not bool:
                raise ProfileError("Contribution repository privacy could not be verified")
            if repository["isPrivate"]:
                continue
            public_id = _text(repository.get("id"), "public repository ID", 200)
            counts = row.get("contributions")
            if not isinstance(counts, dict):
                raise ProfileError("Malformed public contribution count")
            count = _count(counts.get("totalCount"), "public contribution count")
            total += count
            if count:
                public_repositories.add(public_id)
        mix[key] = _count(total, key)
    return mix, len(public_repositories)


def _mix(now: datetime, github_cli: bool) -> tuple[dict[str, int], int, dict[str, Any]]:
    start, end = _iso(now - timedelta(days=365)), _iso(now)
    variables = {"login": "Ricky-G", "from": start, "to": end}
    if github_cli:
        try:
            result = subprocess.run(
                ["gh", "api", "graphql", "-f", "query=" + GRAPHQL_QUERY,
                 "-f", "login=Ricky-G", "-f", "from=" + start, "-f", "to=" + end],
                capture_output=True, timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProfileError("Read-only GitHub CLI contribution query could not complete") from exc
        if result.returncode or len(result.stdout) > JSON_LIMIT:
            raise ProfileError("Read-only GitHub CLI contribution query was unavailable")
        raw = result.stdout
    else:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise ProfileError("Public contribution mix unavailable: no GitHub CLI or built-in token was supplied")
        response = _fetch(
            GRAPHQL_URL, limit=JSON_LIMIT, accept="application/json", token=token,
            body=_json_bytes({"query": GRAPHQL_QUERY, "variables": variables}),
        )
        raw = _response(response, GRAPHQL_URL, {"application/json"})
    mix, contributed = _public_mix(raw)
    return mix, contributed, {
        "url": GRAPHQL_URL, "from": start, "to": end, "privacy": "public repositories only",
        "mix": mix, "contributed_to": contributed,
    }


def _metadata(key: str) -> dict[str, Any]:
    repository = PROJECTS[key]
    url = "https://api.github.com/repos/" + repository
    response = _fetch(url, limit=JSON_LIMIT, accept="application/vnd.github+json")
    raw = _json(_response(response, url, {"application/json"}))
    if not isinstance(raw, dict) or raw.get("private") is not False or raw.get("full_name") != repository:
        raise ProfileError("Named project is not the expected public repository")
    if raw.get("html_url") != "https://github.com/" + repository:
        raise ProfileError("Project canonical GitHub URL is unexpected")
    return {
        "repository": repository, "url": raw["html_url"], "source": url, "visibility": "public",
        "description": _text(raw.get("description"), "public repository description", 2_000),
        "language": _text(raw["language"], "repository language", 100) if raw.get("language") else None,
        "stars": _count(raw.get("stargazers_count"), "stars"),
        "forks": _count(raw.get("forks_count"), "forks"),
    }


def _public_repos() -> int:
    response = _fetch(USER_URL, limit=JSON_LIMIT, accept="application/vnd.github+json")
    user = _json(_response(response, USER_URL, {"application/json"}))
    if not isinstance(user, dict) or not isinstance(user.get("login"), str) or user["login"].lower() != "ricky-g":
        raise ProfileError("Public repository count belongs to an unexpected GitHub user")
    return _count(user.get("public_repos"), "public repositories")


def _safe_path(root: Path, relative: str) -> Path:
    logical = PurePosixPath(relative)
    if logical.is_absolute() or any(part in {"", ".", ".."} for part in logical.parts) or "\\" in relative:
        raise ProfileError("Cache path is not a safe repository-relative path")
    path = root.joinpath(*logical.parts)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ProfileError("Cache path escapes the repository")
    return path


def _read(root: Path, relative: str, limit: int = HTML_LIMIT) -> bytes:
    path = _safe_path(root, relative)
    try:
        with path.open("rb") as stream:
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise ProfileError("Cached input exceeds its size budget")
        return content
    except OSError as exc:
        raise ProfileError(f"Verified cache file is unavailable: {relative}") from exc


def _validate_stats(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ProfileError("Malformed public stats cache")
    required = {"contributions", "mix", "repos", "contributed_to", "posts", "first_year",
                "agt_stars", "agt_forks", "hub_stars", "projects", "availability", "calendar_total"}
    if set(raw) != required:
        raise ProfileError("Stats cache is not a verified public snapshot")
    for key in ("contributions", "repos", "contributed_to", "posts", "hub_stars", "calendar_total"):
        if raw[key] is not None:
            _count(raw[key], key)
    if raw["first_year"] is not None and (type(raw["first_year"]) is not int or not 1900 <= raw["first_year"] <= 9999):
        raise ProfileError("Invalid first publication year")
    for key in ("agt_stars", "agt_forks"):
        if raw[key] is not None and (
            not isinstance(raw[key], str) or not re.fullmatch(r"\d+(?:\.\d)?k?", raw[key])
        ):
            raise ProfileError("Invalid formatted public project count")
    if raw["mix"] is not None:
        if not isinstance(raw["mix"], dict) or set(raw["mix"]) != set(GROUPS):
            raise ProfileError("Invalid contribution mix")
        for key, value in raw["mix"].items():
            _count(value, key)
    if (raw["mix"] is None) != (raw["contributed_to"] is None):
        raise ProfileError("Public mix and scoped repository count must share availability")
    if not isinstance(raw["projects"], dict) or not isinstance(raw["availability"], dict):
        raise ProfileError("Public snapshot lacks explicit metadata/availability")
    state_keys = {"repos", "posts", "calendar", "mix"} | set(PROJECTS)
    if set(raw["availability"]) != state_keys or any(
        value not in {"available", "unavailable"} for value in raw["availability"].values()
    ):
        raise ProfileError("Public snapshot has invalid availability states")
    for state, key in (("repos", "repos"), ("posts", "posts"), ("calendar", "contributions"), ("mix", "mix")):
        if (raw["availability"][state] == "available") != (raw[key] is not None):
            raise ProfileError("Metric availability disagrees with its public evidence")
    if (raw["posts"] is None) != (raw["first_year"] is None):
        raise ProfileError("Post count and first publication year must share availability")
    for key, project in raw["projects"].items():
        if key not in PROJECTS or not isinstance(project, dict) or project.get("visibility") != "public":
            raise ProfileError("Cached project is not explicitly public")
        if set(project) != {"repository", "url", "source", "visibility", "description", "language", "stars", "forks"}:
            raise ProfileError("Cached project contains unexpected fields")
        if project.get("repository") != PROJECTS[key] or project.get("source") != "https://api.github.com/repos/" + PROJECTS[key]:
            raise ProfileError("Unexpected cached public project identity")
        if project.get("url") != "https://github.com/" + PROJECTS[key]:
            raise ProfileError("Unsafe project link")
        _text(project.get("description"), "cached repository description", 2_000)
        if project.get("language") is not None:
            _text(project["language"], "cached language", 100)
        _count(project.get("stars"), "cached stars")
        _count(project.get("forks"), "cached forks")
    for key in PROJECTS:
        if (raw["availability"][key] == "available") != (key in raw["projects"]):
            raise ProfileError("Project availability disagrees with its public metadata")
    agt, hub = raw["projects"].get("agt"), raw["projects"].get("hub")
    if raw["agt_stars"] != (_format_k(agt["stars"]) if agt else None) or raw["agt_forks"] != (_format_k(agt["forks"]) if agt else None):
        raise ProfileError("Formatted toolkit counts disagree with exact public metadata")
    if raw["hub_stars"] != (hub["stars"] if hub else None):
        raise ProfileError("Scenario Hub count disagrees with exact public metadata")
    return raw


def _cache_sources(receipt: dict[str, Any], data: dict[str, Any]):
    sources = receipt.get("sources")
    if not isinstance(sources, dict) or set(sources) - ({"feed", "readiness", "repos", "posts", "calendar", "mix"} | set(PROJECTS)):
        raise ProfileError("Cache contains unexpected or missing public-source receipts")
    for source in sources.values():
        if not isinstance(source, dict) or set(source) != {"payload", "retrieved_at"} or not isinstance(source["payload"], dict):
            raise ProfileError("Malformed public-source receipt")
        _instant(source["retrieved_at"], "source retrieval time")
    payloads = {key: value["payload"] for key, value in sources.items()}
    if payloads.get("feed") != {"url": FEED_URL, "sha256": _hash(_json_bytes(data["feed"]))}:
        raise ProfileError("Feed cache is not bound to the canonical public source")
    if payloads.get("readiness") != {"articles": receipt["readiness"]}:
        raise ProfileError("Public readiness receipts disagree")
    stats = data["stats"]
    for key, project in stats["projects"].items():
        if payloads.get(key) != project:
            raise ProfileError("Project snapshot is not bound to its public source")
    if stats["repos"] is not None and payloads.get("repos") != {"url": USER_URL, "public_repos": stats["repos"]}:
        raise ProfileError("Repository count lacks matching public evidence")
    if stats["posts"] is not None and payloads.get("posts") != {
        "url": ATOM_URL, "archive_url": ARCHIVE_URL, "posts": stats["posts"], "first_year": stats["first_year"]
    }:
        raise ProfileError("Post count lacks matching public Atom/archive evidence")
    if stats["contributions"] is not None:
        calendar_source = payloads.get("calendar", {})
        if (
            calendar_source.get("url") != CALENDAR_URL
            or calendar_source.get("contributions") != stats["contributions"]
            or calendar_source.get("calendar_sha256") != _hash(_json_bytes(data["calendar"]))
            or calendar_source.get("privacy") != "anonymous public GitHub calendar; may include publicly displayed aggregate"
            or not 350 <= len(data["calendar"]) <= 378
        ):
            raise ProfileError("Contribution snapshot lacks its anonymous public calendar evidence")
    elif data["calendar"] or stats["calendar_total"] is not None:
        raise ProfileError("Unavailable contributions must not fabricate a calendar")
    if stats["mix"] is not None:
        mix_source = payloads.get("mix", {})
        if (
            set(mix_source) != {"url", "from", "to", "privacy", "mix", "contributed_to"}
            or mix_source.get("url") != GRAPHQL_URL
            or mix_source.get("privacy") != "public repositories only"
            or mix_source.get("mix") != stats["mix"]
            or mix_source.get("contributed_to") != stats["contributed_to"]
            or _instant(mix_source["to"], "mix period end") - _instant(mix_source["from"], "mix period start") != timedelta(days=365)
        ):
            raise ProfileError("Contribution mix lacks its public-only scoped evidence")


def _cache(root: Path, *, offline: bool) -> tuple[dict[str, Any] | None, list[str]]:
    receipt_path = _safe_path(root, "data/provenance.json")
    if not receipt_path.exists():
        if offline:
            raise ProfileError("Offline mode requires a previously verified public cache, not starter samples")
        legacy = any(_safe_path(root, f"data/{name}.json").exists() for name in CACHE_FILES)
        return None, ["Unverified starter samples ignored; public snapshots will be fetched"] if legacy else []
    receipt = _json(_read(root, "data/provenance.json"))
    if not isinstance(receipt, dict) or receipt.get("version") != 1 or receipt.get("feed_url") != FEED_URL:
        raise ProfileError("Invalid public cache provenance")
    data = {name: _json(_read(root, f"data/{name}.json")) for name in CACHE_FILES}
    if not isinstance(receipt.get("hashes"), dict) or set(receipt["hashes"]) != set(CACHE_FILES):
        raise ProfileError("Cache requires exact hashes for all critical input files")
    for name in CACHE_FILES:
        if receipt.get("hashes", {}).get(name) != _hash(_json_bytes(data[name])):
            raise ProfileError(f"Cached {name} does not match its last-good evidence")
    data["feed"] = validate_feed(data["feed"])
    data["stats"] = _validate_stats(data["stats"])
    data["calendar"] = _validate_calendar(data["calendar"])
    readiness = receipt.get("readiness")
    if not isinstance(readiness, list) or len(readiness) != len(data["feed"]["items"]):
        raise ProfileError("Cache lacks exact public article-readiness evidence")
    for item, proof in zip(data["feed"]["items"], readiness):
        if proof != {"url": item["url"], "canonical": item["url"], "title": item["title"], "status": 200}:
            raise ProfileError("Cached article identity is not last-good published evidence")
    manifest = data["covers"]
    if not isinstance(manifest, dict) or set(manifest) - {it["url"] for it in data["feed"]["items"]}:
        raise ProfileError("Invalid cached cover manifest")
    decoded = {}
    for article_url, cover in manifest.items():
        if not isinstance(cover, dict) or set(cover) != {"source_url", "path", "sha256", "bytes", "width", "height"}:
            raise ProfileError("Malformed cached cover receipt")
        _blog_url(article_url, article=True)
        _blog_url(cover["source_url"])
        if not isinstance(cover["path"], str) or not re.fullmatch(r"assets/src/covers/[a-f0-9]{64}\.jpg", cover["path"]):
            raise ProfileError("Unsafe cached cover path")
        if cover["path"] != "assets/src/covers/" + _hash(cover["source_url"].encode("ascii")) + ".jpg":
            raise ProfileError("Cached cover path is not bound to its public image URL")
        jpeg = _jpeg(_read(root, cover["path"], JPEG_LIMIT), cached=True)
        if (
            cover["sha256"] != _hash(jpeg) or cover["bytes"] != len(jpeg)
            or cover["width"] != 1316 or cover["height"] != 564
        ):
            raise ProfileError("Cached raster does not match its public receipt")
        decoded[article_url] = base64.b64encode(jpeg).decode("ascii")
    if data["calendar"] and data["stats"]["calendar_total"] != sum(c["count"] for c in data["calendar"]):
        raise ProfileError("Cached calendar total does not match verified public cells")
    _cache_sources(receipt, data)
    data["decoded_covers"], data["receipt"] = decoded, receipt
    return data, []


def _stable_sources(old: dict[str, Any], new: dict[str, Any], observed: str) -> dict[str, Any]:
    result = {}
    for key, payload in new.items():
        previous = old.get(key)
        if isinstance(previous, dict) and previous.get("payload") == payload:
            result[key] = previous
        else:
            result[key] = {"payload": payload, "retrieved_at": observed}
    return result


def _fetch_feed() -> tuple[dict[str, Any], bytes]:
    response = _fetch(FEED_URL, limit=JSON_LIMIT, accept="application/feed+json, application/json")
    body = _response(response, FEED_URL, {"application/json", "application/feed+json"})
    return validate_feed(body), body


def _ready_articles(feed: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    readiness, observations = [], {}
    for item in feed["items"]:
        response = _fetch(item["url"], limit=HTML_LIMIT, accept="text/html")
        proof = _article_receipt(response, item)
        readiness.append({key: proof[key] for key in ("url", "canonical", "title", "status")})
        observations[item["url"]] = proof
    return readiness, observations


def _stable_feed(feed: dict[str, Any], first_body: bytes) -> str:
    second_feed, second_body = _fetch_feed()
    if second_feed != feed or _hash(second_body) != _hash(first_body):
        raise ProfileError("Public feed changed during readiness checks; previous published output must be retained")
    return _hash(second_body)


def check_feed(root: Path) -> bool:
    """Cheap unchanged-feed gate; changed feeds must pass public readiness first."""
    cache, _ = _cache(Path(root), offline=True)
    feed, body = _fetch_feed()
    if feed == cache["feed"]:
        return False
    _ready_articles(feed)
    _stable_feed(feed, body)
    return True


def read_inputs(root: Path, *, now: datetime, offline: bool = False, github_cli: bool = False) -> ProfileInputs:
    """Return validated public inputs and changed cache bytes; never write files."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ProfileError("now must be a timezone-aware instant")
    root = Path(root)
    cache, messages = _cache(root, offline=offline)
    if offline:
        assert cache is not None
        feed, manifest = cache["feed"], cache["covers"]
        for item in feed["items"]:
            if item["url"] not in manifest:
                messages.append(f"Explicit drawn placeholder cover: {item['title']}")
            elif manifest[item["url"]]["source_url"] != item.get("image"):
                messages.append(f"Retained last-good public cover: {item['title']}")
        return ProfileInputs(
            feed, cache["stats"], cache["decoded_covers"], cache["calendar"],
            warnings=messages, provenance={"mode": "offline", "last_good": cache["receipt"]},
        )

    feed, first_body = _fetch_feed()
    observed = _iso(now)
    observations: dict[str, Any] = {
        "feed": {"url": FEED_URL, "raw_sha256": _hash(first_body), "bytes": len(first_body)},
    }
    readiness, article_observations = _ready_articles(feed)
    observations.update(article_observations)

    covers, manifest, raster_pending = {}, {}, {}
    for item in feed["items"]:
        article_url, image_url = item["url"], item.get("image")
        if image_url:
            try:
                response = _fetch(image_url, limit=COVER_LIMIT, accept="image/png, image/jpeg, image/webp")
                raw = _response(response, image_url, {"image/png", "image/jpeg", "image/webp"})
                jpeg = _jpeg(raw, content_type=response.content_type)
                relative = "assets/src/covers/" + _hash(image_url.encode("ascii")) + ".jpg"
                manifest[article_url] = {
                    "source_url": image_url, "path": relative, "sha256": _hash(jpeg),
                    "bytes": len(jpeg), "width": 1316, "height": 564,
                }
                covers[article_url] = base64.b64encode(jpeg).decode("ascii")
                raster_pending[relative] = jpeg
                observations[image_url] = {
                    "url": image_url, "raw_sha256": _hash(raw), "jpeg_sha256": _hash(jpeg), "bytes": len(raw),
                }
                continue
            except ProfileError as exc:
                messages.append(f"Cover unavailable for {item['title']}: {exc}")
        if cache and article_url in cache["decoded_covers"]:
            covers[article_url] = cache["decoded_covers"][article_url]
            manifest[article_url] = cache["covers"][article_url]
            messages.append(f"Retained last-good public cover: {item['title']}")
        else:
            messages.append(f"Explicit drawn placeholder cover: {item['title']}")

    observations["feed"]["stable_reread_sha256"] = _stable_feed(feed, first_body)

    stats = dict(cache["stats"]) if cache else {
        "contributions": None, "mix": None, "repos": None, "contributed_to": None, "posts": None,
        "first_year": None, "agt_stars": None, "agt_forks": None, "hub_stars": None, "calendar_total": None,
        "projects": {}, "availability": {},
    }
    stats["projects"], stats["availability"] = dict(stats["projects"]), dict(stats["availability"])
    calendar = cache["calendar"] if cache else []
    sources = {key: value["payload"] for key, value in cache["receipt"].get("sources", {}).items()} if cache else {}
    sources["feed"] = {"url": FEED_URL, "sha256": _hash(_json_bytes(feed))}
    sources["readiness"] = {"articles": readiness}

    def unavailable(name: str, exc: ProfileError, retained: bool):
        messages.append(f"{name}: {exc}; {'last-good public snapshot retained' if retained else 'explicitly unavailable'}")
        stats["availability"][name] = "available" if retained else "unavailable"

    try:
        stats["repos"] = _public_repos()
        stats["availability"]["repos"] = "available"
        sources["repos"] = {"url": USER_URL, "public_repos": stats["repos"]}
    except ProfileError as exc:
        unavailable("repos", exc, stats["repos"] is not None)
    for key in PROJECTS:
        try:
            metadata = _metadata(key)
            stats["projects"][key] = metadata
            stats["availability"][key] = "available"
            sources[key] = metadata
            if key == "agt":
                stats["agt_stars"], stats["agt_forks"] = _format_k(metadata["stars"]), _format_k(metadata["forks"])
            elif key == "hub":
                stats["hub_stars"] = metadata["stars"]
        except ProfileError as exc:
            unavailable(key, exc, key in stats["projects"])
    try:
        stats["posts"], stats["first_year"], sources["posts"] = _posts()
        stats["availability"]["posts"] = "available"
    except ProfileError as exc:
        unavailable("posts", exc, stats["posts"] is not None)
    try:
        response = _fetch(CALENDAR_URL, limit=HTML_LIMIT, accept="text/html")
        calendar, stats["contributions"] = _calendar(response, now)
        stats["calendar_total"] = sum(cell["count"] for cell in calendar)
        stats["availability"]["calendar"] = "available"
        sources["calendar"] = {
            "url": CALENDAR_URL, "privacy": "anonymous public GitHub calendar; may include publicly displayed aggregate",
            "contributions": stats["contributions"], "calendar_sha256": _hash(_json_bytes(calendar)),
        }
        observations["calendar"] = {"url": CALENDAR_URL, "raw_sha256": _hash(response.body), "bytes": len(response.body)}
    except ProfileError as exc:
        unavailable("calendar", exc, stats["contributions"] is not None)
    try:
        stats["mix"], stats["contributed_to"], mix_source = _mix(now, github_cli)
        stats["availability"]["mix"] = "available"
        previous = sources.get("mix")
        if isinstance(previous, dict) and previous.get("mix") == mix_source["mix"] and previous.get("contributed_to") == mix_source["contributed_to"]:
            sources["mix"] = previous
        else:
            sources["mix"] = mix_source
        observations["mix"] = mix_source
    except ProfileError as exc:
        unavailable("mix", exc, stats["mix"] is not None)
    stats = _validate_stats(stats)
    payloads = {"feed": feed, "stats": stats, "calendar": calendar, "covers": manifest}
    hashes = {key: _hash(_json_bytes(value)) for key, value in payloads.items()}
    receipt = {
        "version": 1, "feed_url": FEED_URL, "hashes": hashes, "readiness": readiness,
        "sources": _stable_sources(cache["receipt"].get("sources", {}) if cache else {}, sources, observed),
    }
    proposed = {f"data/{key}.json": _json_bytes(value) for key, value in payloads.items()}
    proposed["data/provenance.json"] = _json_bytes(receipt)
    proposed.update(raster_pending)
    pending = {}
    for relative, value in proposed.items():
        path = _safe_path(root, relative)
        try:
            unchanged = path.exists() and path.read_bytes() == value
        except OSError as exc:
            raise ProfileError(f"Cannot compare the cache baseline for {relative}") from exc
        if not unchanged:
            pending[relative] = value
    return ProfileInputs(
        feed, stats, covers, calendar, pending, messages,
        {"mode": "online", "retrieved_at": observed, "observations": observations, "last_good": receipt},
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile the public writing feed without rendering unchanged content.")
    parser.add_argument("--check-feed", action="store_true", required=True)
    parser.parse_args(argv)
    try:
        changed = check_feed(Path(__file__).resolve().parents[1])
        message = (
            "Changed public feed passed article readiness and a stable re-read; refresh is required."
            if changed else "Canonical public feed is unchanged; expensive refresh skipped with no commit."
        )
        output = os.environ.get("GITHUB_OUTPUT")
        if output:
            with Path(output).open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(f"changed={'true' if changed else 'false'}\n")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with Path(summary).open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(message + "\n")
        print(message)
        return 0
    except (ProfileError, OSError) as exc:
        message = f"Public feed check FAILED: {exc}. Previous published output retained."
        print(message, file=sys.stderr)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            safe = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print(f"::error::{safe}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
