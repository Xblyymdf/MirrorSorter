# Mirror·Sorter

A local-first multi-platform content sorter for Windows & Android. It aggregates
content from **Bilibili / Baidu Tieba / Xiaoheihe (HeyBox)**, filters and ranks it
with a rule engine plus AI (any OpenAI-compatible API), and serves an interactive
feed with built-in AI chat.

- **Single-file backend**: `app.py` uses only the Python standard library — zero third-party dependencies
- **Bundled frontend**: vanilla HTML/JS served directly by the local HTTP service
- **One codebase, two platforms**: desktop runs as-is; Android embeds the same Python service inside the APK
- **Tunable feed & AI chat**: single / dual / large-image display modes, search with sort modes
  (relevance / views / newest / trending-hot, `*` prefix keeps personal blocks) and an AI chat that
  can act on your behalf (search, tune preferences, ask-about-a-card, daily digest with preference suggestions)
- **AI floating ball (desktop)**: an always-on-top **wireframe dodecahedron hologram**
  (real 3D rotation, edge bloom, scanlines) that expands in place into a chat panel on
  **click** (hovering only highlights it) — web-evidence fact checking
  (true/false/uncertain + evidence links) and evidence-backed clapback generation;
  drag it to a screen edge to tuck it away behind a thin restore strip; colors follow
  the main UI accent

## Repository Layout

```
├─ desktop/   Desktop: local Python service + browser UI (app.py + web/ + one-click scripts)
├─ android/   Android: Chaquopy shell, APK ships with its own Python runtime, WebView loads the local service
├─ docs/      Project standard: layout, versioning, tests, build, release & auto-update
├─ releases/  Build artifacts (APK / exe, git-ignored)
├─ .gitattributes / .gitignore
└─ README.md / FEEDBACK.md
```

> Login credentials (Bilibili cookies / Tieba BDUSS / HeyBox session) are stored
> only in the local data directory on each device. The repository contains no
> user data or secrets.

## Desktop

See [desktop/README.md](desktop/README.md). Quick start:

```bat
cd desktop
启动.bat          :: start the app window (Python 3.10+ + WebView2)
构建exe.bat       :: build a single-file exe with PyInstaller into desktop\build\dist\
发布更新.bat       :: maintainers only: push the branch + publish the exe to GitHub Releases
python test_smoke.py   :: regression tests
```

Project-wide conventions (version bumps, CRLF-safe editing, three-layer acceptance,
release/rollback) live in **[docs/STANDARD.md](docs/STANDARD.md)**.

- Runtime data lives in `%USERPROFILE%\AppData\LocalLow\MirrorSorter` (never pollutes the repo)
- Platform adapters: Bilibili Web API / Tieba mobile API / HeyBox signed API (with QR login)
- **Auto-update (v0.4.1dsk+)**: the left rail has a 「检测更新」 button under Settings.
  It reads the latest GitHub Release, shows the new version / size / notes, downloads and
  verifies the exe, then replaces itself and restarts. Source runs and Android hide it.
  The exe is published as a Release asset and is **never committed to git**.

## Android

See [android/README.md](android/README.md). Quick start:

```bat
android\build_apk.bat    :: sync desktop sources → Gradle build → artifact in releases\
```

- The one-click script syncs `app.py + web/` from `../desktop` into the APK assets
- Artifact: `releases/洞鉴虚明-<version>.apk` (~35MB, debug-signed, install directly)
- Versioning: desktop versions end with `dsk` (e.g. `v0.4.1dsk`), Android with `e` (e.g. `0.1.5.30e`)

## Feedback & Community

For bug reports, feature requests and discussion, join our QQ group: **533068204**.
Please read the [feedback guide](FEEDBACK.md) first (common issues + report template).

## Disclaimer

- This project is for learning and personal use only. **Commercial use is strictly prohibited.**
- Content aggregation uses public APIs of each platform only. Please keep request rates low and respect the platforms' terms of service.
- AI features require your own OpenAI-compatible API endpoint (e.g. DeepSeek). Keys are stored locally only.

---

反馈与交流请加 QQ 群：**533068204**（反馈前请先看上方反馈指南）
