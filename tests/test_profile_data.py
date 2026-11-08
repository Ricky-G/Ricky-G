"""Public-data contracts; fixture writes occur only in test-owned temporary roots."""
from __future__ import annotations

import base64
import copy
import html
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import profile_data as pd

NOW = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)


def feed(count=3):
    return {
        "version": "https://jsonfeed.org/version/1.1", "title": "Building with AI & Azure",
        "home_page_url": pd.BLOG + "/", "feed_url": pd.FEED_URL,
        "items": [{
            "id": pd.BLOG + f"/AI/post-{i}/", "url": pd.BLOG + f"/AI/post-{i}/",
            "title": f"Public article {i}", "content_text": "Public, plain-text article summary.",
            "date_published": f"2026-11-0{6-i}T20:00:00Z",
        } for i in range(count)],
    }


def response(url, body, mime="text/html", status=200):
    return pd.Response(body.encode("utf-8") if isinstance(body, str) else body, url, mime, status)


def article(item, *, canonical=None, title=None, heading=None):
    title = title if title is not None else item["title"]
    heading = heading if heading is not None else item["title"]
    return (
        "<!doctype html><html><head>"
        f'<link rel="canonical" href="{html.escape(canonical or item["url"], quote=True)}">'
        f"<title>{html.escape(title)} | Building with AI &amp; Azure</title></head>"
        f"<body><h1>{html.escape(heading)}</h1></body></html>"
    )


def calendar_html():
    cells, tips = [], []
    for index in range(365):
        day = (NOW.date() - timedelta(days=364 - index)).isoformat()
        count = 2 if index == 364 else 0
        cells.append(f'<td id="day-{index}" data-date="{day}" data-level="{1 if count else 0}"></td>')
        tips.append(f'<tool-tip for="day-{index}">{"2" if count else "No"} contributions on a public date.</tool-tip>')
    return (
        '<h2 id="js-contribution-activity-description">2 contributions in the last year</h2>'
        + "".join(cells + tips)
    )


class PublicFixture:
    def __init__(self, value=None):
        self.feed = value or feed()
        self.calls = []
        self.overrides = {}
        self.feed_reads = 0
        self.unstable = False

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url in self.overrides:
            value = self.overrides[url]
            if isinstance(value, Exception):
                raise value
            return value
        if url == pd.FEED_URL:
            self.feed_reads += 1
            value = copy.deepcopy(self.feed)
            if self.unstable and self.feed_reads > 1:
                value["items"][0]["title"] = "Changed while checking publication"
            return response(url, pd._json_bytes(value), "application/json")
        for item in self.feed["items"]:
            if url == item["url"]:
                return response(url, article(item))
        if url == pd.USER_URL:
            return response(url, json.dumps({"login": "Ricky-G", "public_repos": 129}), "application/json")
        for key, repository in pd.PROJECTS.items():
            if url == "https://api.github.com/repos/" + repository:
                return response(url, json.dumps({
                    "private": False, "full_name": repository, "html_url": "https://github.com/" + repository,
                    "description": "A verified public repository description.", "language": "Python",
                    "stargazers_count": 6533 if key == "agt" else 9,
                    "forks_count": 1127 if key == "agt" else 0,
                }), "application/json")
        if url == pd.ATOM_URL:
            entries = "".join(
                f"<entry><title>{html.escape(item['title'])}</title>"
                f"<published>{item['date_published']}</published>"
                f'<link rel="alternate" href="{item["url"]}"/></entry>' for item in self.feed["items"]
            )
            return response(url, f'<feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>', "application/atom+xml")
        if url == pd.ARCHIVE_URL:
            cards = "".join(
                f'<article><h2><a href="{item["url"]}">{html.escape(item["title"])}</a></h2></article>'
                for item in self.feed["items"]
            )
            return response(url, cards)
        if url == pd.CALENDAR_URL:
            return response(url, calendar_html())
        raise AssertionError("Unexpected public fixture request: " + url)


def snapshots(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def cache_fixture(root, inputs):
    for relative, value in inputs.pending.items():
        path = root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)


def mix_payload():
    collection = {group: [] for group in pd.GROUPS.values()}
    collection[pd.GROUPS["commits"]] = [
        {"repository": {"id": "PUBLIC_A", "isPrivate": False}, "contributions": {"totalCount": 7}},
        {"repository": {"id": "NEVER_RETAIN_PRIVATE", "isPrivate": True}, "contributions": {"totalCount": 900}},
    ]
    collection[pd.GROUPS["prs"]] = [
        {"repository": {"id": "PUBLIC_A", "isPrivate": False}, "contributions": {"totalCount": 2}},
        {"repository": {"id": "PUBLIC_B", "isPrivate": False}, "contributions": {"totalCount": 3}},
    ]
    collection[pd.GROUPS["reviews"]] = [
        {"repository": {"id": "PUBLIC_B", "isPrivate": False}, "contributions": {"totalCount": 1}},
    ]
    return {"data": {"user": {"contributionsCollection": collection}}}


class FeedContracts(unittest.TestCase):
    def test_one_first_post_is_valid(self):
        self.assertEqual(len(pd.validate_feed(feed(1))["items"]), 1)

    def test_orders_publication_instants_not_updated_or_input_order(self):
        value = feed()
        value["items"][0]["date_published"] = "2026-11-07T12:00:00+13:00"
        value["items"][1]["date_published"] = "2026-11-06T23:30:00Z"
        value["items"][2]["date_modified"] = "2099-01-01T00:00:00Z"
        result = pd.validate_feed(value)
        self.assertEqual(result["items"][0]["url"], value["items"][1]["url"])
        self.assertNotIn("date_modified", result["items"][2])

    def test_url_breaks_publication_ties(self):
        value = feed()
        for item in value["items"]:
            item["date_published"] = "2026-11-07T00:00:00Z"
        value["items"].reverse()
        self.assertEqual(
            [item["url"] for item in pd.validate_feed(value)["items"]],
            sorted(item["url"] for item in value["items"]),
        )

    def test_rejects_empty_overlarge_duplicate_and_nonobject(self):
        for items in ([], feed()["items"] * 2, [feed(1)["items"][0]] * 2, ["not an object"]):
            value = feed()
            value["items"] = items
            with self.subTest(items=items), self.assertRaises(pd.ProfileError):
                pd.validate_feed(value)

    def test_rejects_unsafe_and_noncanonical_urls(self):
        urls = [
            "http://clouddev.blog/AI/post/", "https://example.com/AI/post/", pd.BLOG + "/",
            "https://clouddev.blog@evil.example/AI/post/", "https://user@clouddev.blog/AI/post/",
            "https://clouddev.blog:443/AI/post/", pd.BLOG + "/AI/post/?x=1", pd.BLOG + "/AI/post/#x",
            pd.BLOG + "/AI/../post/", pd.BLOG + "/AI/%2e%2e/post/", pd.BLOG + "/AI/%252e%252e/post/",
            pd.BLOG + "/AI/%2Fpost/", pd.BLOG + "/AI/%ff/", pd.BLOG + "/AI/%00/", pd.BLOG + "/archives/",
            pd.BLOG + "/AI\\post/", pd.BLOG + "/AI//post/", pd.BLOG + "/AI/post",
        ]
        for url in urls:
            value = feed(1)
            value["items"][0]["url"] = value["items"][0]["id"] = url
            with self.subTest(url=url), self.assertRaises(pd.ProfileError):
                pd.validate_feed(value)

    def test_rejects_mismatched_id_and_source_identity(self):
        for change in ("id", "home_page_url", "feed_url", "version"):
            value = feed(1)
            if change == "id":
                value["items"][0]["id"] = "not canonical"
            else:
                value[change] = "unexpected"
            with self.subTest(change=change), self.assertRaises(pd.ProfileError):
                pd.validate_feed(value)

    def test_rejects_dates_without_full_explicit_instant(self):
        for value in ("2026-11-07", "2026-11-07T00:00:00", "2026-02-30T00:00:00Z",
                      "2026-11-07T00:00Z", "2026-11-07T00:00:00+15:00", None):
            data = feed(1)
            data["items"][0]["date_published"] = value
            with self.subTest(value=value), self.assertRaises(pd.ProfileError):
                pd.validate_feed(data)

    def test_rejects_missing_or_unsafe_plain_text(self):
        for key, value in (("title", ""), ("content_text", None), ("content_text", "<script>bad</script>"),
                           ("title", "hidden\u202econtrol"), ("title", "null\x00byte"), ("title", "\ud800")):
            data = feed(1)
            data["items"][0][key] = value
            with self.subTest(key=key, value=repr(value)), self.assertRaises(pd.ProfileError):
                pd.validate_feed(data)

    def test_literal_special_characters_remain_plain_text(self):
        value = feed(1)
        value["items"][0]["title"] = 'AI & Azure "tools" < 3'
        item = pd.validate_feed(value)["items"][0]
        self.assertEqual(item["title"], 'AI & Azure "tools" < 3')
        pd._article_receipt(response(item["url"], article(item)), item)

    def test_rejects_duplicate_json_keys_nonfinite_and_invalid_bytes(self):
        for raw in (b'{"version":1,"version":2}', b'{"x":NaN}', b"\xff", b"not JSON"):
            with self.subTest(raw=raw), self.assertRaises(pd.ProfileError):
                pd.validate_feed(raw)

    def test_optional_cover_must_be_safe_same_origin(self):
        value = feed(1)
        value["items"][0]["image"] = "https://example.com/image.png"
        with self.assertRaises(pd.ProfileError):
            pd.validate_feed(value)


class ReadinessAndCacheContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aurora-profile-data-")
        self.root = Path(self.temporary.name)
        self.fixture = PublicFixture()
        self.network = patch.object(pd, "_fetch", side_effect=self.fixture)
        self.network.start()
        self.token = patch.dict(pd.os.environ, {"GITHUB_TOKEN": ""})
        self.token.start()

    def tearDown(self):
        self.token.stop()
        self.network.stop()
        self.temporary.cleanup()

    def test_returns_only_safe_pending_bytes_and_does_not_write(self):
        before = snapshots(self.root)
        result = pd.read_inputs(self.root, now=NOW)
        self.assertEqual(snapshots(self.root), before)
        self.assertTrue(result.pending)
        self.assertTrue(all(not Path(key).is_absolute() and ".." not in key and isinstance(value, bytes)
                            for key, value in result.pending.items()))
        self.assertEqual(self.fixture.feed_reads, 2)
        self.assertEqual(result.stats["repos"], 129)
        self.assertEqual(result.stats["posts"], 3)
        self.assertEqual(result.stats["contributions"], 2)
        self.assertIsNone(result.stats["mix"])
        self.assertIsNone(result.stats["contributed_to"])
        self.assertEqual(result.stats["availability"]["mix"], "unavailable")
        self.assertTrue(any("explicitly unavailable" in message for message in result.warnings))

    def test_first_post_and_future_public_ready_article_are_eligible(self):
        self.fixture.feed = feed(1)
        self.fixture.feed["items"][0]["date_published"] = "2027-01-01T00:00:00Z"
        result = pd.read_inputs(self.root, now=NOW)
        self.assertEqual(len(result.feed["items"]), 1)
        self.assertEqual(result.feed["items"][0]["date_published"], "2027-01-01T00:00:00Z")

    def test_critical_readiness_failure_never_returns_a_proposal(self):
        item = self.fixture.feed["items"][0]
        for bad in (
            response(item["url"], "pending", status=404),
            response(item["url"], article(item, canonical="https://evil.example/article/")),
            response(item["url"], article(item, title="Wrong title")),
            response(item["url"], article(item, heading="Wrong heading")),
            response(item["url"], article(item).replace("</head>", f'<link rel="canonical" href="{item["url"]}"></head>')),
            response(item["url"], article(item), mime="application/json"),
        ):
            self.fixture.overrides[item["url"]] = bad
            with self.subTest(bad=bad), self.assertRaises(pd.ProfileError):
                pd.read_inputs(self.root, now=NOW)
            self.assertEqual(snapshots(self.root), {})

    def test_network_and_unstable_feed_are_explicit_critical_errors(self):
        self.fixture.overrides[pd.FEED_URL] = pd.ProfileError("Bounded network fetch failed")
        with self.assertRaisesRegex(pd.ProfileError, "network"):
            pd.read_inputs(self.root, now=NOW)
        self.fixture.overrides.clear()
        self.fixture.unstable = True
        with self.assertRaisesRegex(pd.ProfileError, "changed during"):
            pd.read_inputs(self.root, now=NOW)
        self.assertEqual(snapshots(self.root), {})

    def test_normalized_but_byte_changed_reread_is_not_promoted(self):
        original = self.fixture
        reads = 0

        def changing(url, **kwargs):
            nonlocal reads
            if url == pd.FEED_URL:
                reads += 1
                body = pd._json_bytes(original.feed)
                return response(url, body + (b" " if reads == 2 else b""), "application/json")
            return original(url, **kwargs)

        with patch.object(pd, "_fetch", side_effect=changing), self.assertRaises(pd.ProfileError):
            pd.read_inputs(self.root, now=NOW)

    def test_offline_verified_cache_is_network_free_and_byte_stable(self):
        initial = pd.read_inputs(self.root, now=NOW)
        cache_fixture(self.root, initial)
        before = snapshots(self.root)
        with patch.object(pd, "_fetch", side_effect=AssertionError("Offline network call")):
            result = pd.read_inputs(self.root, now=NOW + timedelta(days=7), offline=True)
        self.assertEqual(result.pending, {})
        self.assertEqual(result.feed, initial.feed)
        self.assertEqual(result.stats, initial.stats)
        self.assertEqual(snapshots(self.root), before)

    def test_online_same_semantics_keep_cache_timestamps_and_bytes(self):
        initial = pd.read_inputs(self.root, now=NOW)
        cache_fixture(self.root, initial)
        result = pd.read_inputs(self.root, now=NOW + timedelta(days=1))
        self.assertEqual(result.pending, {})
        self.assertNotEqual(initial.provenance["retrieved_at"], result.provenance["retrieved_at"])
        self.assertEqual(initial.provenance["last_good"], result.provenance["last_good"])

    def test_unchanged_feed_check_only_fetches_feed_and_writes_nothing(self):
        cache_fixture(self.root, pd.read_inputs(self.root, now=NOW))
        self.fixture.calls.clear()
        before = snapshots(self.root)
        self.assertFalse(pd.check_feed(self.root))
        self.assertEqual([url for url, _ in self.fixture.calls], [pd.FEED_URL])
        self.assertEqual(snapshots(self.root), before)

    def test_changed_feed_check_requires_article_readiness_and_stable_reread(self):
        cache_fixture(self.root, pd.read_inputs(self.root, now=NOW))
        self.fixture.feed["items"][0]["title"] = "Newly published public title"
        self.fixture.calls.clear()
        before = snapshots(self.root)
        self.assertTrue(pd.check_feed(self.root))
        urls = [url for url, _ in self.fixture.calls]
        self.assertEqual(urls.count(pd.FEED_URL), 2)
        self.assertEqual(set(urls), {pd.FEED_URL} | {item["url"] for item in self.fixture.feed["items"]})
        self.assertEqual(snapshots(self.root), before)

    def test_feed_check_rejects_invalid_unavailable_unstable_or_unready_inputs(self):
        cache_fixture(self.root, pd.read_inputs(self.root, now=NOW))
        before = snapshots(self.root)
        self.fixture.overrides[pd.FEED_URL] = pd.ProfileError("Public provider unavailable")
        with self.assertRaises(pd.ProfileError):
            pd.check_feed(self.root)
        self.fixture.overrides[pd.FEED_URL] = response(pd.FEED_URL, b"{}", "application/json")
        with self.assertRaises(pd.ProfileError):
            pd.check_feed(self.root)
        self.fixture.overrides.clear()
        item = self.fixture.feed["items"][0]
        item["title"] = "Changed title"
        self.fixture.overrides[item["url"]] = response(item["url"], article(item, title="Not deployed yet"))
        with self.assertRaises(pd.ProfileError):
            pd.check_feed(self.root)
        self.fixture.overrides.clear()
        self.fixture.feed_reads = 0
        self.fixture.unstable = True
        with self.assertRaises(pd.ProfileError):
            pd.check_feed(self.root)
        self.assertEqual(snapshots(self.root), before)

    def test_feed_check_requires_verified_cache_not_starter_samples(self):
        with self.assertRaisesRegex(pd.ProfileError, "previously verified"):
            pd.check_feed(self.root)

    def test_unverified_starter_stats_are_not_used_online_or_offline(self):
        self.root.joinpath("data").mkdir()
        self.root.joinpath("data", "feed.json").write_bytes(pd._json_bytes(feed()))
        self.root.joinpath("data", "stats.json").write_bytes(pd._json_bytes({
            "contributions": 1153, "mix": {"commits": 55, "prs": 21, "issues": 17, "reviews": 7},
            "repos": 129, "posts": 26,
        }))
        with self.assertRaisesRegex(pd.ProfileError, "starter samples"):
            pd.read_inputs(self.root, now=NOW, offline=True)
        result = pd.read_inputs(self.root, now=NOW)
        self.assertEqual(result.stats["contributions"], 2)
        self.assertEqual(result.stats["posts"], 3)
        self.assertIsNone(result.stats["mix"])
        self.assertTrue(any("Unverified starter" in message for message in result.warnings))

    def test_corrupt_cache_fails_even_when_online(self):
        cache_fixture(self.root, pd.read_inputs(self.root, now=NOW))
        path = self.root / "data" / "stats.json"
        value = json.loads(path.read_bytes())
        value["repos"] += 1
        path.write_bytes(pd._json_bytes(value))
        before = snapshots(self.root)
        for offline in (True, False):
            with self.subTest(offline=offline), self.assertRaisesRegex(pd.ProfileError, "last-good evidence"):
                pd.read_inputs(self.root, now=NOW, offline=offline)
        self.assertEqual(snapshots(self.root), before)

    def test_rehashed_fabrication_without_matching_source_receipt_fails(self):
        cache_fixture(self.root, pd.read_inputs(self.root, now=NOW))
        path = self.root / "data" / "stats.json"
        value = json.loads(path.read_bytes())
        value["repos"] += 1
        path.write_bytes(pd._json_bytes(value))
        receipt_path = self.root / "data" / "provenance.json"
        receipt = json.loads(receipt_path.read_bytes())
        receipt["hashes"]["stats"] = pd._hash(pd._json_bytes(value))
        receipt_path.write_bytes(pd._json_bytes(receipt))
        with self.assertRaisesRegex(pd.ProfileError, "matching public evidence"):
            pd.read_inputs(self.root, now=NOW, offline=True)

    def test_readiness_receipt_corruption_is_rejected(self):
        cache_fixture(self.root, pd.read_inputs(self.root, now=NOW))
        path = self.root / "data" / "provenance.json"
        value = json.loads(path.read_bytes())
        value["readiness"].append(value["readiness"][0])
        path.write_bytes(pd._json_bytes(value))
        with self.assertRaisesRegex(pd.ProfileError, "exact public"):
            pd.read_inputs(self.root, now=NOW, offline=True)

    def test_unavailable_providers_are_none_with_visible_warnings_not_fabricated_zeros(self):
        for url in (pd.USER_URL, pd.CALENDAR_URL, pd.ATOM_URL):
            self.fixture.overrides[url] = pd.ProfileError("Provider unavailable")
        for repository in pd.PROJECTS.values():
            self.fixture.overrides["https://api.github.com/repos/" + repository] = pd.ProfileError("Provider unavailable")
        result = pd.read_inputs(self.root, now=NOW)
        for key in ("repos", "contributions", "posts", "first_year", "mix", "agt_stars", "agt_forks", "hub_stars"):
            self.assertIsNone(result.stats[key], key)
        self.assertEqual(result.calendar, [])
        self.assertTrue(all(state == "unavailable" for state in result.stats["availability"].values()))
        cache_fixture(self.root, result)
        pd.read_inputs(self.root, now=NOW, offline=True)

    def test_temporary_provider_failure_retains_only_valid_public_snapshot(self):
        initial = pd.read_inputs(self.root, now=NOW)
        cache_fixture(self.root, initial)
        self.fixture.overrides[pd.USER_URL] = pd.ProfileError("Provider unavailable")
        result = pd.read_inputs(self.root, now=NOW + timedelta(days=1))
        self.assertEqual(result.stats["repos"], 129)
        self.assertTrue(any("last-good public snapshot retained" in message for message in result.warnings))
        self.assertEqual(result.pending, {})

    def test_archive_and_atom_mismatch_cannot_invent_post_count(self):
        self.fixture.overrides[pd.ARCHIVE_URL] = response(
            pd.ARCHIVE_URL, f'<article><h2><a href="{pd.BLOG}/AI/other/">Other</a></h2></article>'
        )
        result = pd.read_inputs(self.root, now=NOW)
        self.assertIsNone(result.stats["posts"])
        self.assertTrue(any("Atom and public archive disagree" in message for message in result.warnings))

    def test_current_paragraph_title_markup_and_archive_pagination(self):
        items = self.fixture.feed["items"]
        page_two = pd.ARCHIVE_URL + "page/2/"

        def cards(selected):
            return "".join(
                f'<article class="media"><div class="media-content"><p class="date">2026</p>'
                f'<p class="title"><a href="{item["url"]}">{html.escape(item["title"])}</a></p>'
                '<p class="categories"><a href="/categories/AI/">AI</a></p></div></article>'
                for item in selected
            )

        self.fixture.overrides[pd.ARCHIVE_URL] = response(
            pd.ARCHIVE_URL, cards(items[:1]) + f'<a class="pagination-link" href="{page_two}">2</a>'
        )
        self.fixture.overrides[page_two] = response(
            page_two, cards(items[1:]) + f'<a class="pagination-link" href="{pd.ARCHIVE_URL}">1</a>'
        )
        result = pd.read_inputs(self.root, now=NOW)
        self.assertEqual(result.stats["posts"], 3)
        self.assertEqual(result.stats["first_year"], 2026)
        self.assertIn(page_two, [url for url, _ in self.fixture.calls])

    def test_truncated_or_entity_atom_feed_is_explicitly_unavailable(self):
        for body in (
            '<!DOCTYPE feed [<!ENTITY unsafe SYSTEM "file:///private">]><feed/>',
            '<feed xmlns="http://www.w3.org/2005/Atom">' + "<entry/>" * 50 + "</feed>",
        ):
            self.fixture.overrides[pd.ATOM_URL] = response(pd.ATOM_URL, body, "application/atom+xml")
            with self.subTest(body=body[:100]):
                result = pd.read_inputs(self.root, now=NOW)
                self.assertIsNone(result.stats["posts"])
                self.assertTrue(any(message.startswith("posts:") for message in result.warnings))

    def test_no_timezone_guessing(self):
        with self.assertRaisesRegex(pd.ProfileError, "timezone-aware"):
            pd.read_inputs(self.root, now=NOW.replace(tzinfo=None))

    def test_failed_cover_is_an_explicit_placeholder(self):
        item = self.fixture.feed["items"][0]
        item["image"] = pd.BLOG + "/img/public.png"
        self.fixture.overrides[item["image"]] = pd.ProfileError("Cover network failed")
        result = pd.read_inputs(self.root, now=NOW)
        self.assertNotIn(item["url"], result.covers)
        self.assertTrue(any("Explicit drawn placeholder" in message for message in result.warnings))


class PublicContributionContracts(unittest.TestCase):
    def test_filters_private_before_reading_counts_and_deduplicates_public_ids(self):
        value = mix_payload()
        private = value["data"]["user"]["contributionsCollection"][pd.GROUPS["commits"]][1]
        private["id"] = "private"
        private["repository"]["id"] = None
        private["contributions"] = object()
        mix, repos = pd._public_mix(value)
        self.assertEqual(mix, {"commits": 7, "prs": 5, "issues": 0, "reviews": 1})
        self.assertEqual(repos, 2)

    def test_cap_may_truncate_and_privacy_must_be_explicit(self):
        value = mix_payload()
        rows = value["data"]["user"]["contributionsCollection"][pd.GROUPS["issues"]]
        rows.extend([{"repository": {"isPrivate": True}}] * 100)
        with self.assertRaisesRegex(pd.ProfileError, "cap may truncate"):
            pd._public_mix(value)
        value = mix_payload()
        value["data"]["user"]["contributionsCollection"][pd.GROUPS["commits"]][0]["repository"].pop("isPrivate")
        with self.assertRaisesRegex(pd.ProfileError, "privacy"):
            pd._public_mix(value)

    def test_missing_collection_and_negative_public_counts_are_errors(self):
        for value in ({}, {"errors": [{"message": "do not retain raw error"}]}, {"data": {"user": None}}):
            with self.subTest(value=value), self.assertRaises(pd.ProfileError):
                pd._public_mix(value)
        value = mix_payload()
        value["data"]["user"]["contributionsCollection"][pd.GROUPS["commits"]][0]["contributions"]["totalCount"] = -1
        with self.assertRaises(pd.ProfileError):
            pd._public_mix(value)

    def test_cli_receipt_contains_no_private_ids_counts_or_raw_authenticated_response(self):
        result = Mock(returncode=0, stdout=pd._json_bytes(mix_payload()))
        with patch.object(pd.subprocess, "run", return_value=result) as command:
            mix, repos, receipt = pd._mix(NOW, True)
        serialized = json.dumps(receipt)
        self.assertNotIn("NEVER_RETAIN_PRIVATE", serialized)
        self.assertNotIn("900", serialized)
        self.assertNotIn("PUBLIC_A", serialized)
        self.assertEqual(mix["commits"], 7)
        self.assertEqual(repos, 2)
        self.assertEqual(command.call_args.kwargs["timeout"], 30)
        self.assertNotIn("auth", command.call_args.args[0])

    def test_anonymous_calendar_is_parsed_not_synthetic(self):
        cells, visible = pd._calendar(response(pd.CALENDAR_URL, calendar_html()), NOW)
        self.assertEqual(len(cells), 365)
        self.assertEqual(visible, 2)
        self.assertEqual(sum(cell["count"] for cell in cells), 2)
        self.assertEqual(cells[-1], {"date": "2026-11-07", "count": 2, "level": 1})

    def test_calendar_missing_tooltip_duplicate_date_and_wrong_level_are_errors(self):
        for body in (
            calendar_html().replace('for="day-364"', 'for="missing"'),
            calendar_html().replace('data-date="2026-11-07"', 'data-date="2026-11-06"'),
            calendar_html().replace('data-level="1"', 'data-level="0"'),
            '<h2 id="js-contribution-activity-description">1205 contributions in the last year</h2>',
        ):
            with self.subTest(body=body[:120]), self.assertRaises(pd.ProfileError):
                pd._calendar(response(pd.CALENDAR_URL, body), NOW)

    def test_malformed_displayed_calendar_numbers_are_explicit_errors(self):
        for value in (",,,", "12,34", "99999999999999999999999"):
            body = calendar_html().replace("2 contributions in the last year", value + " contributions in the last year")
            with self.subTest(value=value), self.assertRaises(pd.ProfileError):
                pd._calendar(response(pd.CALENDAR_URL, body), NOW)


class BoundedFetchContracts(unittest.TestCase):
    def transport(self, body, length=None, url=pd.FEED_URL):
        headers = Message()
        headers["Content-Type"] = "application/json"
        if length is not None:
            headers["Content-Length"] = str(length)
        result = Mock(status=200, headers=headers)
        result.geturl.return_value = url
        result.read.side_effect = lambda count: body[:count]
        context = Mock()
        context.__enter__ = Mock(return_value=result)
        context.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = context
        return opener, result

    def test_response_length_and_actual_bytes_have_hard_limits(self):
        for length in (20, None, "invalid"):
            opener, result = self.transport(b"x" * 20, length)
            with self.subTest(length=length), patch.object(pd.urllib.request, "build_opener", return_value=opener):
                with self.assertRaisesRegex(pd.ProfileError, "size budget"):
                    pd._fetch(pd.FEED_URL, limit=10, accept="application/json")
            if length is None:
                result.read.assert_called_once_with(11)
            else:
                result.read.assert_not_called()

    def test_timeouts_retries_are_bounded_and_fail_explicitly(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError("offline")
        with patch.object(pd.urllib.request, "build_opener", return_value=opener), patch.object(pd.time, "sleep") as sleep:
            with self.assertRaisesRegex(pd.ProfileError, "bounded attempts"):
                pd._fetch(pd.FEED_URL, limit=10, accept="application/json")
        self.assertEqual(opener.open.call_count, 2)
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 12)

    def test_off_origin_redirect_is_rejected_before_following(self):
        request = pd.urllib.request.Request(pd.FEED_URL)
        for url in ("https://evil.example/feed.json", "http://clouddev.blog/latest-posts.json"):
            with self.subTest(url=url), self.assertRaisesRegex(pd.ProfileError, "unsafe redirect"):
                pd._SafeRedirect().redirect_request(request, None, 302, "", {}, url)

    def test_token_is_never_sent_to_a_public_blog_or_cover(self):
        with self.assertRaisesRegex(pd.ProfileError, "Authentication is restricted"):
            pd._fetch(pd.FEED_URL, limit=10, accept="application/json", token="test-only-placeholder")

    def test_safe_cache_path_rejects_absolute_traversal_and_backslashes(self):
        root = Path(tempfile.gettempdir())
        for relative in ("../file", "/file", "assets\\file.jpg"):
            with self.subTest(relative=relative), self.assertRaises(pd.ProfileError):
                pd._safe_path(root, relative)

    def test_feed_check_cli_reports_noop_through_bounded_step_metadata(self):
        with tempfile.TemporaryDirectory(prefix="aurora-feed-step-") as temporary:
            output = Path(temporary) / "output"
            summary = Path(temporary) / "summary"
            with patch.object(pd, "check_feed", return_value=False), patch.dict(pd.os.environ, {
                "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary),
            }), patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(pd.main(["--check-feed"]), 0)
            self.assertEqual(output.read_bytes(), b"changed=false\n")
            self.assertIn("unchanged", summary.read_text())

    def test_failed_feed_check_cli_cannot_produce_a_success_shaped_gate(self):
        with tempfile.TemporaryDirectory(prefix="aurora-feed-step-") as temporary:
            output = Path(temporary) / "output"
            with patch.object(pd, "check_feed", side_effect=pd.ProfileError("Not published")), patch.dict(pd.os.environ, {
                "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": "", "GITHUB_ACTIONS": "true",
            }), patch("sys.stdout", new_callable=io.StringIO) as stdout, patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertEqual(pd.main(["--check-feed"]), 1)
            self.assertFalse(output.exists())
            self.assertIn("::error::", stdout.getvalue())
            self.assertIn("Previous published output retained", stderr.getvalue())


@unittest.skipUnless(importlib.util.find_spec("PIL"), "Parent's isolated Pillow dependency is not installed yet")
class RasterContracts(unittest.TestCase):
    def png(self):
        from PIL import Image
        data = io.BytesIO()
        Image.new("RGBA", (700, 300), (50, 170, 150, 150)).save(data, "PNG")
        return data.getvalue()

    def test_raster_is_resized_cropped_budgeted_and_metadata_free(self):
        from PIL import Image
        raw = self.png()
        jpeg = pd._jpeg(raw, content_type="image/png")
        self.assertLess(len(jpeg), 100_000)
        with Image.open(io.BytesIO(jpeg)) as image:
            self.assertEqual(image.size, (1316, 564))
            self.assertEqual(image.format, "JPEG")
            self.assertFalse(image.getexif())
        self.assertEqual(pd._jpeg(jpeg, cached=True), jpeg)
        self.assertEqual(pd._jpeg(raw, content_type="image/png"), jpeg)

    def test_invalid_mime_size_svg_and_cached_dimensions_are_rejected(self):
        from PIL import Image
        wrong = io.BytesIO()
        Image.new("RGB", (2, 2)).save(wrong, "JPEG")
        for data, options in (
            (b"<svg><script/></svg>", {"content_type": "image/png"}),
            (self.png(), {"content_type": "image/jpeg"}),
            (b"x" * pd.JPEG_LIMIT, {"cached": True}),
            (wrong.getvalue(), {"cached": True}),
        ):
            with self.subTest(options=options), self.assertRaises(pd.ProfileError):
                pd._jpeg(data, **options)

    def test_new_cover_failure_retains_previous_valid_raster_and_offline_pixels(self):
        with tempfile.TemporaryDirectory(prefix="aurora-cover-cache-") as temporary:
            root = Path(temporary)
            fixture = PublicFixture()
            item = fixture.feed["items"][0]
            item["image"] = pd.BLOG + "/img/cover.png"
            fixture.overrides[item["image"]] = response(item["image"], self.png(), "image/png")
            with patch.object(pd, "_fetch", side_effect=fixture), patch.dict(pd.os.environ, {"GITHUB_TOKEN": ""}):
                initial = pd.read_inputs(root, now=NOW)
                self.assertIn(item["url"], initial.covers)
                cache_fixture(root, initial)
                before = snapshots(root)
                fixture.overrides[item["image"]] = pd.ProfileError("Raster temporarily unavailable")
                retained = pd.read_inputs(root, now=NOW + timedelta(days=1))
                self.assertEqual(retained.covers, initial.covers)
                self.assertEqual(retained.pending, {})
                self.assertTrue(any("Retained last-good public cover" in m for m in retained.warnings))
                offline = pd.read_inputs(root, now=NOW, offline=True)
                self.assertEqual(offline.covers, initial.covers)
                self.assertEqual(snapshots(root), before)
                self.assertEqual(base64.b64decode(offline.covers[item["url"]], validate=True),
                                 base64.b64decode(initial.covers[item["url"]], validate=True))


if __name__ == "__main__":
    unittest.main()
