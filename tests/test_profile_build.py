import io
import json
import os
from pathlib import Path
import re
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
        svg = build.hero_desktop(build.THEMES["light"], "fixture")
        self.assertIn('id="motion"', svg)
        self.assertIn(".fixture:not(:target) *{animation:none!important}", svg)

    def test_first_post_feed_has_no_missing_second_or_third_card(self):
        stats = dict(STATS)
        build.set_stamp(stats, datetime(2026, 11, 7, tzinfo=timezone.utc).astimezone(build.NZ))
        rendered = build.build_all(build.THEMES["light"], "light",
                                   {"stats": stats, "feed": {"items": [POST]}, "now": datetime.now(build.NZ)},
                                   build.stand_in)
        self.assertIn("generated/post-1", rendered)
        self.assertIn("generated/post-1-phone", rendered)
        self.assertNotIn("generated/post-2", rendered)
        self.assertNotIn("generated/post-3", rendered)

    def test_every_generated_variant_is_safe_and_within_its_budget(self):
        stats = dict(STATS)
        build.set_stamp(stats, datetime(2026, 11, 7, tzinfo=timezone.utc).astimezone(build.NZ))
        data = {"stats": stats, "feed": {"items": [POST]}, "now": datetime.now(build.NZ)}
        for theme, tokens in build.THEMES.items():
            rendered = build.build_all(tokens, theme, data, build.stand_in)
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

    def test_all_hero_variants_are_employer_free_and_keep_the_aurora_design(self):
        for theme, tokens in build.THEMES.items():
            for renderer in (build.hero_desktop, build.hero_phone):
                with self.subTest(theme=theme, renderer=renderer.__name__):
                    svg = renderer(tokens, "fixture")
                    self.assertNotIn("Microsoft", svg)
                    self.assertIn("Cloud and AI Solution Architect", svg)
                    self.assertIn("Cloud &amp; AI Solution", svg)
                    self.assertIn("Engineer at heart", svg)
                    self.assertIn("Auckland", svg)
                    self.assertNotIn("<image", svg)
                    self.assertNotIn("data:image", svg)
                    self.assertIn('data-illustration="trusted-cloud-agent-governance"', svg)
                    for node in ("cloud", "agent", "tool"):
                        self.assertIn(f'data-node="{node}"', svg)
                    self.assertIn("The hard part isn&#x27;t creating AI.", svg)
                    self.assertIn("It&#x27;s governing it.", svg)
                    self.assertIn(".fixture:not(:target) *{animation:none!important}", svg)
                    build.validate_svg("assets/hero-light.svg", svg.encode())

    def test_intro_keeps_owner_website_narrative_and_the_complete_blog_link(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
        intro = readme.split("<h2><picture>", 1)[0]
        self.assertNotIn("Microsoft", intro)
        self.assertIn("Cloud &amp; AI Solution Architect · Auckland, NZ", intro)
        self.assertIn("Engineer at heart.", intro)
        self.assertIn("I design cloud architectures, build AI systems you can trust", intro)
        self.assertIn("create developer platforms that accelerate delivery", intro)
        self.assertIn('I write about the journey on <a href="https://clouddev.blog">my blog</a>.', intro)
        self.assertIn("<strong>About me</strong>", intro)
        self.assertIn("I'm an engineer from Auckland, passionate about cloud, AI, and startups.", intro)
        self.assertIn("turning gnarly architecture into things that actually ship.", intro)

    def test_full_off_clock_copy_is_accessible_text_when_images_shorten_it(self):
        stats = dict(STATS)
        build.set_stamp(stats, datetime(2026, 11, 8, tzinfo=timezone.utc).astimezone(build.NZ))
        block = build.signoff_block(stats)
        self.assertIn(f'<p align="center">{build.esc(build.OFF_CLOCK_TEXT)}</p>', block)
        self.assertIn(build.esc(build.OFF_CLOCK_TEXT) + " Auckland, New Zealand.", block)
        self.assertIn("I love meeting founders and building crazy, impossible ideas together", block)
        for renderer in (build.signoff, build.signoff_phone):
            svg = renderer(build.THEMES["light"], "fixture", stats)
            root = build.ET.fromstring(svg)
            self.assertEqual(root.find("{http://www.w3.org/2000/svg}title").text, build.OFF_CLOCK_TEXT)
            build.validate_svg("assets/generated/signoff-light.svg", svg.encode())

    def test_employer_removal_does_not_remove_microsoft_technology_names(self):
        for theme, tokens in build.THEMES.items():
            with self.subTest(theme=theme):
                svg = build.stack(tokens, "fixture")
                self.assertIn("Microsoft Agent Framework", svg)
                self.assertIn("Microsoft Foundry", svg)


class TypographyTests(unittest.TestCase):
    def setUp(self):
        self.stats = dict(STATS)
        self.now = datetime(2026, 11, 9, tzinfo=timezone.utc).astimezone(build.NZ)
        build.set_stamp(self.stats, self.now)
        self.tokens = build.THEMES["light"]

    def projected(self, svg, role, displayed_width):
        root = build.ET.fromstring(svg)
        source_width = float(root.get("viewBox").split()[2])
        nodes = [node for node in root.iter() if node.get("data-font-role") == role]
        self.assertTrue(nodes, role)
        fonts = [float(re.search(r"font-size:([\d.]+)px", node.get("style"))[1]) for node in nodes]
        return min(fonts) * displayed_width / source_width

    def test_desktop_type_is_measurably_larger_at_the_actual_profile_width(self):
        cases = [
            (build.hero_desktop(self.tokens, "fixture"), "hero-role", 837.5, 23),
            (build.hero_desktop(self.tokens, "fixture"), "hero-meta", 837.5, 17),
            (build.introduction(self.tokens, "fixture"), "intro-body", 837.5, 20),
            (build.post_featured(self.tokens, "fixture", POST, build.stand_in(POST), self.now),
             "post-title", 837.5, 25),
            (build.post_featured(self.tokens, "fixture", POST, build.stand_in(POST), self.now),
             "post-body", 837.5, 15),
            (build.post_featured(self.tokens, "fixture", POST, build.stand_in(POST), self.now),
             "post-meta", 837.5, 12),
            (build.post_mini(self.tokens, "fixture", POST, build.stand_in(POST), self.now),
             "post-title", 275, 18),
            (build.tile_agt(self.tokens, "fixture", self.stats), "project-body", 550, 15),
            (build.tile_agt(self.tokens, "fixture", self.stats), "project-meta", 550, 12),
            (build.stack(self.tokens, "fixture"), "chip-body", 837.5, 14),
            (build.pulse(self.tokens, "fixture", self.stats), "pulse-meta", 837.5, 12),
            (build.signoff(self.tokens, "fixture", self.stats), "signoff-body", 837.5, 16),
        ]
        for svg, role, width, minimum in cases:
            with self.subTest(role=role, width=width):
                self.assertGreaterEqual(self.projected(svg, role, width), minimum)

    def test_narrow_profile_uses_reflowed_larger_tokens_not_desktop_shrink(self):
        cases = [
            (build.introduction(self.tokens, "fixture", phone=True), "intro-body", 235.6, 20),
            (build.hero_phone(self.tokens, "fixture"), "hero-role", 235.6, 17),
            (build.hero_phone(self.tokens, "fixture"), "hero-meta", 235.6, 12),
            (build.post_phone(self.tokens, "fixture", POST, build.stand_in(POST), self.now),
             "post-body", 235.6, 15),
            (build.post_phone(self.tokens, "fixture", POST, build.stand_in(POST), self.now),
             "post-meta", 235.6, 12),
            (build.post_mini(self.tokens, "fixture", POST, build.stand_in(POST), self.now),
             "post-title", 238, 15),
            (build.post_mini(self.tokens, "fixture", POST, build.stand_in(POST), self.now),
             "post-meta", 238, 12),
            (build.tile_docker(self.tokens, "fixture", self.stats), "terminal-body", 238, 12),
            (build.tile_agt_phone(self.tokens, "fixture", self.stats), "project-body", 235.6, 15),
            (build.tile_agt_phone(self.tokens, "fixture", self.stats), "terminal-body", 235.6, 12),
            (build.stack_phone(self.tokens, "fixture"), "chip-body", 235.6, 15),
            (build.pulse_phone(self.tokens, "fixture", self.stats), "pulse-meta", 235.6, 12),
            (build.signoff_phone(self.tokens, "fixture", self.stats), "signoff-body", 235.6, 15),
        ]
        for svg, role, width, minimum in cases:
            with self.subTest(role=role, width=width):
                self.assertGreaterEqual(self.projected(svg, role, width), minimum)

    def test_full_primary_narrative_never_ellipsizes_on_either_canvas(self):
        for phone in (False, True):
            svg = build.introduction(self.tokens, "fixture", phone=phone)
            root = build.ET.fromstring(svg)
            body = " ".join(node.text for node in root.iter()
                            if node.get("data-font-role") == "intro-body")
            self.assertEqual(body, build.INTRO_BODY + " " + build.ABOUT_BODY)
            self.assertNotIn("…", body)
            self.assertNotIn("<image", svg)
            build.validate_svg("assets/intro-light.svg", svg.encode())

    def test_native_intro_and_title_hierarchy_uses_supported_heading_semantics(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
        self.assertIn('<h3 align="center">Cloud &amp; AI Solution Architect · Auckland, NZ</h3>', readme)
        self.assertIn('src="assets/intro-light.svg"', readme)
        self.assertIn("Read the introduction as text", readme)
        self.assertIn('<a href="https://clouddev.blog">my blog</a>', readme)
        self.assertNotRegex(readme, r"<(?:big|font|style|script)\b")
        self.assertNotRegex(readme, r"\bstyle=")
        writing = build.writing_block({"items": [POST]}, self.stats)
        self.assertIn(f'<h3><a href="{POST["url"]}">{POST["title"]}</a></h3>', writing)
        projects = build.projects_block(self.stats)
        self.assertIn('<h3><a href="https://github.com/microsoft/agent-governance-toolkit">', projects)

    def test_governance_hero_has_no_raster_resource_or_portrait_dependency(self):
        for renderer in (build.hero_desktop, build.hero_phone):
            svg = renderer(self.tokens, "fixture")
            root = build.ET.fromstring(svg)
            self.assertFalse(list(root.iter("{http://www.w3.org/2000/svg}image")))
            self.assertNotIn("base64", svg)
            self.assertNotIn("portrait", svg.lower())
            self.assertIn("layered policy shield", root.get("aria-label").lower())
            self.assertEqual(root.find("{http://www.w3.org/2000/svg}title").text, build.HERO_TITLE)
            self.assertEqual({node.get("data-node") for node in root.iter() if node.get("data-node")},
                             {"cloud", "agent", "tool"})

    def test_desktop_audit_chip_does_not_cover_a_redundant_core_caption(self):
        desktop = build.ET.fromstring(build.hero_desktop(self.tokens, "fixture"))
        phone = build.ET.fromstring(build.hero_phone(self.tokens, "fixture"))
        self.assertFalse([node for node in desktop.iter()
                          if node.get("data-font-role") == "illustration-caption"])
        self.assertEqual(len([node for node in phone.iter()
                              if node.get("data-font-role") == "illustration-caption"]), 1)
        self.assertIn("identity, policy and audit", desktop.get("aria-label"))

    def test_project_identity_wraps_at_the_owner_repository_boundary(self):
        for renderer in (build.tile_agt, build.tile_agt_phone):
            root = build.ET.fromstring(renderer(self.tokens, "fixture", self.stats))
            labels = [node.text for node in root.iter() if node.get("data-font-role") == "project-meta"]
            self.assertEqual(labels[:2], ["microsoft/", "agent-governance-toolkit"])

    def test_stack_layer_heading_has_its_own_column_before_the_first_chip(self):
        root = build.ET.fromstring(build.stack(self.tokens, "fixture"))
        labels = [node for node in root.iter() if node.get("data-font-role") == "stack-meta"
                  and node.text in {name for name, _ in build.STACK["layers"]}]
        self.assertEqual(len(labels), 3)
        first_chip_x = 22 + 212
        for node in labels:
            right = float(node.get("x")) + build.tw(node.text, 18, "mono", ls=1)
            self.assertLessEqual(right + 16, first_chip_x)

    def test_larger_text_has_reserved_vertical_space_for_long_titles_and_metadata(self):
        long_post = dict(POST, title="Long unbroken article " + "X" * 280)
        data = {"stats": self.stats, "feed": {"items": [long_post]}, "now": self.now}
        for theme, tokens in build.THEMES.items():
            rendered = build.build_all(tokens, theme, data, build.stand_in)
            for name, svg in rendered.items():
                root = build.ET.fromstring(svg)
                height = float(root.get("viewBox").split()[3])
                for node in root.iter("{http://www.w3.org/2000/svg}text"):
                    if not node.get("data-font-role"):
                        continue
                    size = float(re.search(r"font-size:([\d.]+)px", node.get("style"))[1])
                    self.assertLessEqual(float(node.get("y")) + size * .25, height,
                                         (name, node.text, height))
                build.validate_svg(f"assets/{name}-{theme}.svg", svg.encode())

    def test_docker_illustration_wraps_commands_without_shrinking_metadata(self):
        root = build.ET.fromstring(build.tile_docker(self.tokens, "fixture", self.stats))
        lines = [node for node in root.iter("{http://www.w3.org/2000/svg}text")
                 if node.get("data-font-role") == "terminal-body"]
        self.assertEqual(len(lines), 4)
        for node in lines:
            self.assertLessEqual(build.tw("".join(node.itertext()), 21, "mono"), 308)
            self.assertIn("font-size:21px", node.get("style"))


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

    def test_real_fourteen_day_new_boundary_changes_once_then_is_an_exact_noop(self):
        feed = json.loads((self.root / "data" / "feed.json").read_bytes())
        published = datetime.fromisoformat(feed["items"][0]["date_published"].replace("Z", "+00:00"))
        self.now = published + timedelta(days=14, seconds=-1)
        self.assertEqual(self.render_cached_provider(), 0)
        self.assertIn(b">NEW</text>", (self.root / "assets" / "generated" / "post-1-light.svg").read_bytes())
        self.now += timedelta(seconds=1)
        self.assertEqual(self.render_cached_provider(), 0)
        expired = self.snapshot()
        self.assertNotIn(b">NEW</text>", expired["assets/generated/post-1-light.svg"])
        self.assertEqual(json.loads(expired["data/build-state.json"])["rendered_at"],
                         self.now.astimezone(build.NZ).isoformat())
        self.now += timedelta(hours=1)
        self.assertEqual(self.render_cached_provider(), 0)
        self.assertEqual(self.snapshot(), expired)

    def test_profile_build_no_longer_opens_or_needs_the_original_portrait(self):
        (self.root / "assets" / "src" / "portrait.jpg").unlink()
        self.assertEqual(self.render_cached_provider(), 0)
        for name in ("hero-light.svg", "hero-dark.svg", "hero-phone-light.svg", "hero-phone-dark.svg"):
            self.assertNotIn(b"<image", (self.root / "assets" / name).read_bytes())


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

    def test_refresh_is_default_branch_only_and_exactly_weekly_or_manual(self):
        text = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "profile.yml").read_text()
        self.assertIn("if: github.ref == format('refs/heads/{0}', github.event.repository.default_branch)", text)
        self.assertIn('ref: ${{ github.event.repository.default_branch }}', text)
        triggers = text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertEqual(re.findall(r"^  ([a-z_]+):", triggers, re.MULTILINE),
                         ["schedule", "workflow_dispatch"])
        self.assertEqual(re.findall(r'cron:\s*"([^"]+)"', triggers), ["0 6 * * 1"])
        self.assertEqual(triggers.count('timezone: "Pacific/Auckland"'), 1)
        self.assertNotIn("steps.feed", text)
        self.assertNotIn("--check-feed", text)
        self.assertNotIn("hourly", text.lower())

    def test_weekly_auckland_schedule_keeps_monday_six_across_daylight_saving(self):
        for local, utc_hour in (
            (datetime(2026, 11, 9, 6, tzinfo=build.NZ), 17),
            (datetime(2026, 6, 8, 6, tzinfo=build.NZ), 18),
        ):
            with self.subTest(local=local):
                self.assertEqual(local.weekday(), 0)
                utc = local.astimezone(timezone.utc)
                self.assertEqual(utc.weekday(), 6)
                self.assertEqual(utc.hour, utc_hour)
                self.assertEqual(utc.minute, 0)

    def test_readonly_checks_keep_source_pushes_and_pull_requests(self):
        text = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "checks.yml").read_text()
        triggers = text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertEqual(re.findall(r"^  ([a-z_]+):", triggers, re.MULTILINE),
                         ["push", "pull_request", "workflow_dispatch"])
        self.assertNotIn("pull_request_target", text)
        self.assertNotIn("workflow_run", text)
        self.assertNotIn("contents: write", text)
        self.assertNotIn("git push", text)

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
