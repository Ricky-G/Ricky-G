# Aurora Australis profile

The owner's Aurora artwork is the design surface: Auckland skyline, illustrated portrait, green-to-magenta accents, article covers, project illustrations, an architecture stack and a contribution snake. GitHub still controls its page background, sidebar and navigation. The README contains supported Markdown/HTML and repository-local SVG images, not a website embedded in GitHub.

## Build locally

Python 3.14, Pillow and timezone data are pinned in `requirements.txt`. Use a virtual environment; no global installation is required.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe scripts\build_profile.py --offline
```

`--offline` validates the last-good public cache and preserves its capture time. It performs no network requests. Two offline builds with unchanged inputs must be exact no-ops. A deliberately supplied `--now` must include a timezone.

Repository attributes keep generated/checksummed text in LF form on both Windows and Linux. Do not convert cached JSON, SVG or generated README boundaries to CRLF by hand.

To refresh using an existing local GitHub CLI login:

```powershell
.\.venv\Scripts\python.exe scripts\build_profile.py --github-cli
```

This makes read-only public-data requests. It does not commit or push. A fresh raw starter requires `--initialize` once after reviewing the template; do not use that flag to hide an ownership conflict. The delivered build already includes its initialized state.

## What changes automatically

Only the `WRITING`, `PROJECTS`, `PULSE` and `SIGNOFF` boundaries and generator-owned images/cache are generated. Edit prose outside those boundaries normally. Changes inside a generated block fail its checksum instead of being silently overwritten. Hand-edited generated images likewise fail their ownership check; change the design in `scripts/build_profile.py`.

The source is the public [JSON Feed](https://clouddev.blog/latest-posts.json), with `content_text`, complete titles, canonical URLs and original publication dates. Rank by publication time descending, then canonical URL. Do not reconstruct URLs from edited titles, rank by modification time, or hide already-public future-dated articles.

Promotion requires a valid nonempty feed, individually ready article GETs with matching canonical/title, and a stable feed re-read. An output-repository push or raw file is **not** proof that GitHub Pages has deployed. Invalid, empty, duplicated, unstable or unavailable critical inputs fail visibly before invalid content is promoted. Covers are validated raster images, cropped and embedded; the offline cache retains real covers. Missing optional metrics/covers are labelled rather than replaced by sample data.

Public GitHub repository counters are captured at build time. Contribution-type counts exclude private repositories before aggregation. The anonymous public calendar total may include other contribution types and anonymous aggregates the owner has made public, so it need not equal the four-type breakdown. Unavailable figures remain explicitly unavailable. The supplied sample statistics are not evidence.

Archive count/year come from public publication metadata, not the three-item feed. Provider receipts and public-only cached data are under `data/`; they are not browser-time APIs.

## Refresh pipeline

`.github/workflows/profile.yml` refreshes the default branch daily at **06:00 Pacific/Auckland** and checks for new writing at **17 minutes past each hour**. Native timezone scheduling handles NZST/NZDT. The hourly gate makes a bounded canonical public-feed request and validates the last-good cache. If the normalized feed is unchanged, it skips rendering, GitHub metrics and snake generation without creating a commit. A changed feed must pass article readiness and a stable re-read before the full refresh can proceed; the renderer repeats those checks before publication.

Use **Actions → Profile refresh → Run workflow → main** for an immediate full refresh or recovery. Actions can delay/drop schedules, especially at the start of an hour, and disable public schedules after **60 days without repository activity**. Re-enable in the Actions UI and run manually to recover. There is no few-minute freshness SLA. A same-URL cover-only change is picked up by the daily/manual full refresh, not the unchanged-feed hourly gate.

The workflow uses only its repository-scoped, short-lived `GITHUB_TOKEN`, with `contents: write` limited to the refresh job. No PAT, deploy key, cloud service or cross-repository credential is installed. The owner's separate blog deploy key does not grant profile access. The optional dispatch hook from the starter kit is **not installed** in the blog/output repository. Daily polling alone does not promise a few-minute update after publication.

Snake generation and all README changes share one default-branch-only job and one ordinary, non-force push. Manual runs on feature branches are skipped rather than writing to main. If snake generation fails, the previous valid images are retained with a workflow warning. A branch-head change fails publication instead of rebasing over owner edits. Branch protection may require a separately designed PR-based refresh; do not bypass it or change settings to make this workflow write.

`.github/workflows/checks.yml` is read-only: Ubuntu and Windows runners execute the public-data, renderer and workflow contracts, then verify two network-free builds leave the committed README/images/cache unchanged. Checkout credentials are not persisted, and this workflow cannot publish. Validate a feature-branch candidate there and on the native GitHub README before adopting it on main.

A local build never commits, pushes, runs remote workflows or changes profile settings. Publication and pipeline activation require owner approval; ordinary future design changes should follow the same checks/native-preview gate.

## Rendering and accessibility

GitHub documents [theme-aware picture sources](https://github.blog/changelog/2022-05-19-specify-theme-context-for-images-in-markdown-beta/) and [profile README formatting](https://docs.github.com/en/get-started/writing-on-github/getting-started-with-writing-and-formatting-on-github/quickstart-for-writing-on-github). Light/dark image variants use that pattern. Phone-specific `max-width` sources are locally verified, not a promise about every GitHub sanitizer or mobile-app version; the fallback remains a real image with a descriptive accessible name and adjacent full text.

Fixed pixel tile widths with GitHub's normal image maximum-width behavior allow tiles to wrap into readable phone rows instead of shrinking a three-column grid to tiny text. Each article/project has its own real link; full article titles, dates, excerpts and stack labels are also real text. The terminal, Kanban and snake are illustrations, not interactive controls or measured performance results.

SVGs contain no scripts, external fonts/resources, foreign objects or SMIL animations. Their **default is static**. Only a parent `<picture>` source matching `prefers-reduced-motion: no-preference` selects the explicit `#motion` opt-in. This avoids inconsistent reduced-motion evaluation inside embedded SVG images observed in local Chromium/Firefox probes. Reduced-motion visitors and unsupported-source fallbacks receive the static image URL; the choice uses no JavaScript. The fragment itself is an explicit opt-in intended for the parent README, not a claim that directly opening an opted-in raw URL performs another preference check.

The same static-default adapter processes workflow-generated snake images, retaining their original animation while applying the parent-controlled opt-in. Both normal animated frames and stable reduced-motion frames are local verification requirements. GitHub's handling of compound media conditions and SVG fragments remains an **adoption check**, not something a local proxy can establish. If that rendering cannot preserve the preference gate, keep the static presentation rather than enabling animation indiscriminately.

Non-cover images stay below 100 KB; hero/post images below 200 KB. Text measurement uses a portable bundled Pillow font with conservative slack; visitors still see their system fonts.

Local Chromium, Firefox and WebKit checks are useful proxies. They are not native GitHub rendering, the GitHub mobile app, Edge, or Safari itself. Those remain adoption checks; no draft is uploaded merely to claim they passed.

## Manual profile settings

The README cannot update the bio, website, status, local time or pinned repositories. The starter's suggestions remain owner actions: `ricky-g.com` as website, Auckland/Pacific time, blog and LinkedIn social links, governance status, and selected public project pins. None are changed by this build.

## Rollback

Revert the chosen profile revision to its prior valid README/assets and disable the refresh workflow in Actions if rollback is required. Use an ordinary revert, not a force push or history rewrite. Do not delete unrelated GIF files or the old profile history. Resolve stale build locks only after confirming that their recorded process is no longer running.

## Public source references

- [Latest articles](https://clouddev.blog/latest-posts.json), [publication metadata](https://clouddev.blog/atom.xml), [archive](https://clouddev.blog/archives/)
- [Agent Governance Toolkit](https://github.com/microsoft/agent-governance-toolkit): its coverage statement is project-reported, not an independent security audit.
- [Azure Scenario Hub](https://github.com/Ricky-G/azure-scenario-hub) and the [APIM benchmark](https://clouddev.blog/Azure/APIM/apim-backend-fan-out-shared-backend-rewrite-uri-vs-one-backend-per-api-a-real-benchmark/): 882/874 req/s, 146/153 ms p95 and zero errors are the cited experiment, not universal performance guarantees.
- [Home base and authored off-clock copy](https://ricky-g.com)
- [GitHub schedule limits/timezones](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule), [token scope](https://docs.github.com/en/actions/concepts/security/github_token), [branch protection](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
