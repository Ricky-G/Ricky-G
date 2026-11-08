import base64
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_profile as build


POST = {
    "id": "https://clouddev.blog/Azure/Architecture/fixture-post/",
    "url": "https://clouddev.blog/Azure/Architecture/fixture-post/",
    "title": "Fixture article: a complete accessible title",
    "date_published": "2026-10-01T00:00:00Z",
    "content_text": "A test-only excerpt with <literal> & safe text.",
}
STATS = {
    "contributions": 100, "mix": {"commits": 20, "prs": 10, "issues": 5, "reviews": 2},
    "repos": 12, "contributed_to": 4, "posts": 8, "first_year": 2022,
    "agt_stars": "6.5k", "agt_forks": "1.1k", "hub_stars": 9,
}


def portrait_fixture():
    buffer = io.BytesIO()
    build.Image.new("RGB", (16, 16), "#888888").save(buffer, "JPEG")
    return base64.b64encode(buffer.getvalue()).decode()


class BoundaryTests(unittest.TestCase):
    def source(self):
        return "owner prefix\r\n<!-- WRITING:START -->\nseed\n<!-- WRITING:END -->\r\nowner suffix"

    def test_first_adoption_is_explicit(self):
        with self.assertRaisesRegex(ValueError, "First adoption"):
            build.replace_block(self.source(), "WRITING", "generated")

    def test_owner_prefix_and_suffix_bytes_are_preserved(self):
        result = build.replace_block(self.source(), "WRITING", "generated", True)
        self.assertTrue(result.startswith("owner prefix\r\n"))
        self.assertTrue(result.endswith("\r\nowner suffix"))

    def test_repeated_update_is_an_exact_noop(self):
        initial = build.replace_block(self.source(), "WRITING", "generated", True)
        self.assertEqual(initial, build.replace_block(initial, "WRITING", "generated"))

    def test_changed_feed_replaces_only_owned_body(self):
        initial = build.replace_block(self.source(), "WRITING", "old", True)
        changed = build.replace_block(initial, "WRITING", "new")
        self.assertIn("\nnew\n", changed)
        self.assertNotIn("\nold\n", changed)

    def test_duplicate_start_fails(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            build.replace_block(self.source() + "\n<!-- WRITING:START -->", "WRITING", "", True)

    def test_duplicate_end_fails(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            build.replace_block(self.source() + "\n<!-- WRITING:END -->", "WRITING", "", True)

    def test_inverted_markers_fail(self):
        with self.assertRaisesRegex(ValueError, "invalid"):
            build.replace_block("<!-- WRITING:END --><!-- WRITING:START -->", "WRITING", "", True)

    def test_author_edit_inside_generated_block_fails(self):
        initial = build.replace_block(self.source(), "WRITING", "generated", True)
        with self.assertRaisesRegex(ValueError, "edited"):
            build.replace_block(initial.replace("\ngenerated\n", "\nowner edit\n"), "WRITING", "replacement")

    def test_author_edit_outside_generated_block_survives(self):
        initial = build.replace_block(self.source(), "WRITING", "generated", True).replace("owner prefix", "later prefix")
        self.assertIn("later prefix", build.replace_block(initial, "WRITING", "replacement"))

    def test_legacy_explanatory_marker_requires_initialization(self):
        legacy = self.source().replace("<!-- WRITING:START -->", "<!-- WRITING:START (old explanation) -->")
        self.assertIn("<!-- WRITING:START -->", build.replace_block(legacy, "WRITING", "generated", True))


class RenderingTests(unittest.TestCase):
    def test_long_unbroken_title_stays_inside_measured_width(self):
        lines = build.wrap("X" * 300, 24, "bold", 250, 3)
        self.assertLessEqual(len(lines), 3)
        self.assertTrue(all(build.tw(line, 24, "bold") <= 250 for line in lines))
        self.assertTrue(lines[-1].endswith("…"))

    def test_invalid_wrap_dimensions_are_rejected(self):
        with self.assertRaises(ValueError):
            build.wrap("title", 24, "bold", 0, 3)

    def test_href_and_alt_are_escaped(self):
        image = build.picture("assets/example", '"<title>&', "275", 'https://example.com/" onclick="bad')
        self.assertIn("&quot;&lt;title&gt;&amp;", image)
        self.assertNotIn('" onclick="', image)

    def test_motion_is_selected_only_by_explicit_parent_preference(self):
        image = build.picture("assets/example", "Example", "275", phone=True)
        self.assertIn("(prefers-reduced-motion: no-preference)", image)
        self.assertIn("assets/example-phone-dark.svg#motion", image)
        self.assertIn('<img src="assets/example-light.svg"', image)
        svg = build.hero_desktop(build.THEMES["light"], "fixture", portrait_fixture())
        self.assertIn('id="motion"', svg)
        self.assertIn(".fixture:not(:target) *{animation:none!important}", svg)

    def test_first_post_feed_has_no_missing_second_or_third_card(self):
        stats = dict(STATS)
        build.set_stamp(stats, datetime(2026, 11, 7, tzinfo=timezone.utc).astimezone(build.NZ))
        rendered = build.build_all(build.THEMES["light"], "light",
                                   {"stats": stats, "feed": {"items": [POST]}, "now": datetime.now(build.NZ)},
                                   portrait_fixture(), build.stand_in)
        self.assertIn("generated/post-1", rendered)
        self.assertIn("generated/post-1-phone", rendered)
        self.assertNotIn("generated/post-2", rendered)
        self.assertNotIn("generated/post-3", rendered)

    def test_every_generated_variant_is_safe_and_within_its_budget(self):
        stats = dict(STATS)
        build.set_stamp(stats, datetime(2026, 11, 7, tzinfo=timezone.utc).astimezone(build.NZ))
        data = {"stats": stats, "feed": {"items": [POST]}, "now": datetime.now(build.NZ)}
        for theme, tokens in build.THEMES.items():
            rendered = build.build_all(tokens, theme, data, portrait_fixture(), build.stand_in)
            for name, svg in rendered.items():
                with self.subTest(theme=theme, asset=name):
                    build.validate_svg(f"assets/{name}-{theme}.svg", svg.encode())
                    self.assertNotIn("<animate", svg)

    def test_readable_text_links_keep_full_titles_and_excerpts(self):
        block = build.writing_block({"items": [POST]}, STATS)
        self.assertIn('<a href="' + POST["url"] + '">' + POST["title"] + "</a>", block)
        self.assertIn("&lt;literal&gt; &amp; safe text", block)
        self.assertNotIn("<literal>", block)
        self.assertIn('width="275"', block)

    def test_unavailable_activity_never_uses_sample_statistics(self):
        stats = dict(STATS, contributions=None, mix=None, contributed_to=None)
        for renderer in (build.pulse, build.pulse_phone):
            svg = renderer(build.THEMES["light"], "fixture", stats)
            self.assertIn("unavailable", svg.lower())
            self.assertNotIn("1153", svg)
            self.assertNotIn("None", svg)
            build.validate_svg("assets/generated/pulse-light.svg", svg.encode())

    def test_unavailable_archive_is_not_a_fabricated_count(self):
        svg = build.archive_card(build.THEMES["light"], "fixture", dict(STATS, posts=None, first_year=None))
        self.assertIn("count unavailable", svg)
        self.assertNotIn("posts since None", svg)
        self.assertNotIn(">26<", svg)

    def test_timezone_handles_both_auckland_offsets(self):
        summer, winter = {}, {}
        build.set_stamp(summer, datetime(2026, 11, 7, tzinfo=timezone.utc).astimezone(build.NZ))
        build.set_stamp(winter, datetime(2026, 6, 7, tzinfo=timezone.utc).astimezone(build.NZ))
        self.assertEqual(summer["utc"], "UTC+13")
        self.assertEqual(winter["utc"], "UTC+12")

    def test_calendar_uses_actual_input_dates_and_counts(self):
        svg = build.snake(build.THEMES["light"], "fixture",
                          [{"date": "2026-11-07", "count": 5, "level": 4}])
        self.assertIn("2026-11-07: 5 contributions", svg)
        self.assertIn("prefers-reduced-motion", svg)

    def test_missing_calendar_is_labelled_not_simulated(self):
        svg = build.snake(build.THEMES["light"], "fixture", [])
        self.assertIn("No simulated", svg)
        self.assertNotIn('class="snake"', svg)


class ValidationTests(unittest.TestCase):
    def svg(self, body):
        return ('<svg xmlns="http://www.w3.org/2000/svg"><style>'
                '@media(prefers-reduced-motion:reduce){*{animation:none!important}}</style>'
                + body + '</svg>').encode()

    def test_external_svg_resources_fail(self):
        with self.assertRaisesRegex(ValueError, "external"):
            build.validate_svg("assets/test.svg", self.svg('<image href="https://example.com/a.png"/>'))

    def test_svg_script_and_foreign_object_fail(self):
        for element in ("script", "foreignObject", "iframe", "animateTransform"):
            with self.subTest(element=element), self.assertRaisesRegex(ValueError, "unsupported"):
                build.validate_svg("assets/test.svg", self.svg(f"<{element}/>"))

    def test_external_css_and_event_handlers_fail(self):
        for element in ('<rect style="fill:url(https://example.com/x)"/>', '<rect onload="bad"/>'):
            with self.assertRaises(ValueError):
                build.validate_svg("assets/test.svg", self.svg(element))

    def test_missing_reduced_motion_fails(self):
        with self.assertRaisesRegex(ValueError, "reduced-motion"):
            build.validate_svg("assets/test.svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>')

    def test_size_budget_is_measured_in_bytes(self):
        with self.assertRaisesRegex(ValueError, "budget"):
            build.validate_svg("assets/test.svg", self.svg("<!--" + "x" * 100000 + "-->"))

    def test_missing_or_remote_readme_image_fails(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            build.verify_references(b'<img src="assets/missing.svg">', {})
        with self.assertRaisesRegex(ValueError, "repository-local"):
            build.verify_references(b'<img src="https://example.com/image.svg">', {})

    def test_only_the_known_local_motion_fragment_is_allowed(self):
        build.verify_references(b'<img src="assets/example.svg#motion">', {"assets/example.svg": b"fixture"})
        with self.assertRaisesRegex(ValueError, "Unknown image fragment"):
            build.verify_references(b'<img src="assets/example.svg#unexpected">', {"assets/example.svg": b"fixture"})

    def test_workflow_snake_adapter_is_static_by_default_and_idempotent(self):
        source = self.svg("<g/>").decode()
        adapted = build.stop_snake_motion(source)
        self.assertIn('id="motion"', adapted)
        self.assertIn('id="profile-motion"', adapted)
        self.assertIn("svg:not(:target) *{animation:none!important}", adapted)
        self.assertEqual(adapted, build.stop_snake_motion(adapted))
        build.validate_svg("assets/generated/snake-light.svg", adapted.encode())


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.patch = patch.object(build, "ROOT", self.root)
        self.patch.start()
        (self.root / "a").write_bytes(b"old-a")
        (self.root / "b").write_bytes(b"old-b")
        self.baseline = {name: build.digest((self.root / name).read_bytes()) for name in ("a", "b")}

    def tearDown(self):
        self.patch.stop()
        self.directory.cleanup()

    def test_transaction_replaces_complete_valid_batch(self):
        build.atomic_write({"a": b"new-a", "b": b"new-b"}, self.baseline)
        self.assertEqual((self.root / "a").read_bytes(), b"new-a")
        self.assertEqual((self.root / "b").read_bytes(), b"new-b")

    def test_concurrent_owner_edit_fails_before_writes(self):
        (self.root / "b").write_bytes(b"owner edit")
        with self.assertRaisesRegex(ValueError, "Concurrent"):
            build.atomic_write({"a": b"new-a", "b": b"new-b"}, self.baseline)
        self.assertEqual((self.root / "a").read_bytes(), b"old-a")
        self.assertEqual((self.root / "b").read_bytes(), b"owner edit")

    def test_io_failure_rolls_back_prior_replacements(self):
        original_replace = os.replace
        calls = 0

        def fail_once(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("test-only disk failure")
            return original_replace(source, target)

        with patch.object(build.os, "replace", side_effect=fail_once):
            with self.assertRaises(OSError):
                build.atomic_write({"a": b"new-a", "b": b"new-b"}, self.baseline)
        self.assertEqual((self.root / "a").read_bytes(), b"old-a")
        self.assertEqual((self.root / "b").read_bytes(), b"old-b")

    def test_path_escape_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "escaped"):
            build.atomic_write({"../outside": b"no"}, self.baseline)

    def test_unexpected_new_file_is_preserved(self):
        (self.root / "c").write_bytes(b"owner new file")
        with self.assertRaisesRegex(ValueError, "Concurrent new"):
            build.atomic_write({"c": b"replacement"}, self.baseline)
        self.assertEqual((self.root / "c").read_bytes(), b"owner new file")

    def test_asset_ownership_rejects_manual_changes(self):
        with self.assertRaisesRegex(ValueError, "edited"):
            build.verify_ownership({"files": {"assets/test.svg": "expected"}}, {"assets/test.svg": "changed"}, False, False)


class SnakeRefreshTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="profile-snake-refresh-")
        self.root = Path(self.directory.name)
        source = Path(__file__).resolve().parents[1]
        for name in ("assets", "data"):
            shutil.copytree(source / name, self.root / name)
        for name in ("README.md", "requirements.txt"):
            shutil.copyfile(source / name, self.root / name)
        self.patches = [
            patch.object(build, "ROOT", self.root),
            patch.object(build, "ASSETS", self.root / "assets"),
            patch.object(build, "GEN", self.root / "assets" / "generated"),
            patch.object(build, "DATA", self.root / "data"),
        ]
        for value in self.patches:
            value.start()
        self.state_path = self.root / "data" / "build-state.json"
        state = json.loads(self.state_path.read_bytes())
        self.now = datetime.fromisoformat(state["rendered_at"]) + timedelta(minutes=1)
        state["snake_source"] = "snk"
        for theme in ("light", "dark"):
            name = f"assets/generated/snake-{theme}.svg"
            path = self.root / name
            adapted = build.stop_snake_motion(path.read_text(encoding="utf-8")).encode()
            path.write_bytes(adapted)
            state["files"][name] = build.digest(adapted)
        self.state_path.write_bytes((json.dumps(state, indent=2, sort_keys=True) + "\n").encode())
        self.before = self.snapshot()

    def tearDown(self):
        for value in reversed(self.patches):
            value.stop()
        self.directory.cleanup()

    def snapshot(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def provider_output(self, changed=False):
        for theme in ("light", "dark"):
            path = self.root / f"assets/generated/snake-{theme}.svg"
            root = build.ET.fromstring(path.read_bytes())
            for node in list(root):
                if node.get("id") == "profile-motion":
                    root.remove(node)
            if changed:
                build.ET.SubElement(root, "{http://www.w3.org/2000/svg}circle",
                                    {"cx": "10", "cy": "10", "r": "2", "fill": "#d97706"})
            path.write_bytes(build.ET.tostring(root, encoding="utf-8"))

    def render_cached_provider(self):
        from profile_data import read_inputs
        inputs = read_inputs(self.root, now=self.now, offline=True)
        with patch("profile_data.read_inputs", return_value=inputs), patch("sys.stdout", new_callable=io.StringIO):
            return build.main(["--preserve-snake", "--now", self.now.isoformat()])

    def test_unchanged_provider_rewrite_preserves_all_published_bytes_and_stamp(self):
        self.provider_output()
        self.assertNotEqual(self.snapshot(), self.before)
        self.assertEqual(self.render_cached_provider(), 0)
        self.assertEqual(self.snapshot(), self.before)
        self.provider_output()
        self.assertEqual(self.render_cached_provider(), 0)
        self.assertEqual(self.snapshot(), self.before)

    def test_real_snake_change_advances_stamp_once_then_repeats_exactly(self):
        self.provider_output(changed=True)
        self.assertEqual(self.render_cached_provider(), 0)
        changed = self.snapshot()
        self.assertNotEqual(changed["README.md"], self.before["README.md"])
        self.assertEqual(json.loads(changed["data/build-state.json"])["rendered_at"],
                         self.now.astimezone(build.NZ).isoformat())
        self.provider_output()
        self.now += timedelta(minutes=1)
        self.assertEqual(self.render_cached_provider(), 0)
        self.assertEqual(self.snapshot(), changed)


class WorkflowTests(unittest.TestCase):
    def test_workflow_has_timezone_and_only_repository_write_job(self):
        text = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "profile.yml").read_text()
        self.assertIn('timezone: "Pacific/Auckland"', text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn("contents: read", text)
        self.assertEqual(text.count("contents: write"), 1)
        self.assertNotIn("PROFILE_DISPATCH_TOKEN", text)
        self.assertNotIn("target_branch: output", text)

    def test_workflow_push_is_nonforce_and_detects_concurrent_branch_changes(self):
        text = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "profile.yml").read_text()
        self.assertIn('if [ "$live" != "$baseline" ]', text)
        self.assertIn('git push origin "HEAD:$TARGET_BRANCH"', text)
        self.assertNotIn("--force", text)

    def test_refresh_is_default_branch_only_and_hourly_work_is_gated(self):
        text = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "profile.yml").read_text()
        self.assertIn("if: github.ref == format('refs/heads/{0}', github.event.repository.default_branch)", text)
        self.assertIn('ref: ${{ github.event.repository.default_branch }}', text)
        self.assertIn('cron: "0 6 * * *"', text)
        self.assertIn('cron: "17 * * * *"', text)
        self.assertIn("scripts/profile_data.py --check-feed", text)
        self.assertGreaterEqual(text.count("if: steps.feed.outputs.changed != 'false'"), 6)
        self.assertNotIn("repository_dispatch:", text)
        self.assertNotIn("workflow_run:", text)

    def test_hosted_checks_are_read_only_on_windows_and_linux(self):
        text = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "checks.yml").read_text()
        self.assertIn("contents: read", text)
        self.assertNotIn("contents: write", text)
        self.assertNotIn("git push", text)
        self.assertNotIn("GITHUB_TOKEN", text)
        self.assertIn("persist-credentials: false", text)
        self.assertIn("os: [ubuntu-latest, windows-latest]", text)
        self.assertIn("python -B -m unittest discover -s tests", text)
        self.assertEqual(text.count("scripts/build_profile.py --offline"), 2)
        self.assertEqual(text.count("git diff --exit-code -- README.md assets data"), 2)

    def test_actions_are_immutable_pins_in_both_workflows(self):
        import re
        directory = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        for path in (directory / "checks.yml", directory / "profile.yml"):
            refs = re.findall(r"uses:\s+([^\s]+)", path.read_text())
            self.assertTrue(refs)
            self.assertTrue(all(re.fullmatch(r"[^@]+@[a-f0-9]{40}", ref) for ref in refs), refs)

    def test_lf_attributes_protect_generated_checksums_and_binary_originals(self):
        text = (Path(__file__).resolve().parents[1] / ".gitattributes").read_text()
        for extension in ("md", "json", "svg", "py", "yml"):
            self.assertIn(f"*.{extension} text eol=lf", text)
        for extension in ("jpg", "gif"):
            self.assertIn(f"*.{extension} binary", text)


if __name__ == "__main__":
    unittest.main()
