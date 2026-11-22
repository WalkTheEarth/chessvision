# ChessVision — Improvement Plan

_Last updated: 2026-11-22. Current state: working Linux/GNOME prototype with live
board recognition, Black/White hints, a Mutter ScreenCast capture path, a portal
fallback, a Tk GUI, and an offline self-test._

This is a working document, not a promise. Priorities are ordered by what makes the
tool more useful and trustworthy soonest.

## North star

A fast, local, cross-platform chess coach that watches any board on screen and shows
one clear, trustworthy hint — without touching the game, the account, or the network.

Success looks like: install, pick a board, press start, and get a usable suggestion in
under two seconds on Linux, Windows, and macOS, at any common DPI, on one or two
monitors, without a browser extension.

## What is solid today

- Pixel-based recognition that needs no browser integration.
- Asset themes (`assets/boards`, `assets/pieces`) plus screenshot calibration.
- Fixed side, legal-move tracking, flip handling, last-move tint handling, misread
  tolerance, and auto-resync.
- Local Stockfish with a configurable skill level.
- GNOME capture through Mutter's ScreenCast API with no share dialog; XDG portal
  fallback for other Linux desktops.
- A compact always-on-top GUI with a theme picker, live preview, and debug snapshots.
- A 9,536-cell offline self-test covering themes, tracking, flips, promotion, and
  mate-in-one.
- An app icon and a working README.

## What is fragile or missing

| Area | Gap |
| --- | --- |
| Platforms | Linux only. Capture, engine discovery, paths, and packaging are Linux-specific. |
| Capture | GNOME always records the first monitor. No monitor picker, no window capture on the Mutter path, no reconnect after sleep or resolution changes. |
| Calibration | The board area is manual. A stale or slightly wrong ROI is the most common failure. |
| State | `config.json` and `templates.npz` live next to the script; not writable in a packaged install. |
| UI | Tk with fixed pixel sizes. No tray, global hotkeys, click-through overlay, or HiDPI strategy. |
| Observability | Almost no logging. Debug snapshots exist, but there is no structured log or health check. |
| Testing | Excellent synthetic coverage, but no golden-image regression suite, no capture mocks, no CI. |
| Distribution | No packaging, no versioned releases, no license chosen, no changelog. |
| Legal | Stockfish is GPLv3; bundled assets and the icon need a license review before redistribution. |

## Priorities

### P0 — Make the current Linux build dependable

1. **Extract a capture interface.** Current `ScreenCast` mixes Mutter, portal,
   GStreamer, and frame pulling. Split it into a small `Capture` protocol:
   `start()`, `grab(timeout)`, `frame_size`, `describe()`, `close()`.
   Implementations: `GnomeMutterCapture`, `PortalCapture`. Keep `pipewiresrc` in a
   shared helper. This is the seam Windows and macOS need.
2. **Move state out of the repo.** Use `platformdirs` for config, templates, logs,
   and snapshots. Migrate an existing `config.json`/`templates.npz` on first run.
   Keep an explicit `--config DIR` for development and tests.
3. **Add structured logging.** A rotating log with capture source, frame size, ROI,
   classification count, tracking transitions, engine time, and errors. The `d`
   snapshot stays as the user-facing diagnostic. Never log screenshots by default.
4. **Golden-image tests.** Save small synthetic board images per theme and assert
   exact grids; add a test for highlighted squares, a flipped board, a mid-drag
   frame, and a wrong theme. Run in CI.
5. **Package Linux.** A PyPI entry point (`chessvision = chessvision.cli:main`),
   an AppImage, and a Flatpak. Test the portal and Mutter paths under Flatpak.
6. **Choose a license.** MIT or Apache-2.0 for the project code is the simplest
   default; audit the Stockfish GPLv3 and chess.com asset terms before shipping a
   bundle. Do not bundle assets or the engine until this is settled.

### P1 — Cross-platform capture

The main engineering task. Define the abstraction once, then add backends without
touching recognition, tracking, or the GUI.

| Platform | Primary capture | Fallback | Permission |
| --- | --- | --- | --- |
| Linux GNOME | `org.gnome.Mutter.ScreenCast` (exists) | XDG ScreenCast portal | None on Mutter; one portal approval per run |
| Linux other | XDG ScreenCast portal | — | One approval per run |
| Windows 10/11 | Windows.Graphics.Capture via a maintained Python binding (`windows-capture` / WinRT), Direct3D11 frames | `dxcam` (DXGI) or `mss` (GDI) | Usually none; handle capture-disabled windows and protected content |
| macOS 12.3+ | ScreenCaptureKit via PyObjC (`SCShareableContent`, `SCStream`) | `CGWindowListCreateImage` for older systems, or `screencapture` | Screen Recording permission (TCC), first-run onboarding |

Implementation notes:

- **Frames:** return BGR `numpy.ndarray` exactly as today. Keep a `last` frame so a
  slow capture never blocks the tick.
- **Window vs monitor:** support both. Store `source_kind` and a stable identifier
  (monitor connector/name, or window title/app id). A window capture follows the
  window when it moves and avoids the self-capture problem entirely.
- **ROI:** keep the ROI as fractions of the captured frame. Store `frame_size`.
  If the source size changes, ask for recalibration (current behavior) unless the
  new size is an exact scale of the old one, in which case the fractions still work.
- **DPI:** capture in physical pixels. Test 100/125/150/200% on Windows and Retina
  2× on macOS. Tk coordinates and image coordinates are different spaces; isolate
  that conversion in the GUI.
- **Multi-monitor:** enumerate displays, store the chosen one, and show a preview.
  On GNOME, remove the "first monitor" shortcut. If the board is on a second
  display, the app must follow it.
- **Reconnect:** if frames stop for more than ~2 seconds (sleep, monitor hotplug,
  permission revoked), recreate the capture session and show `CAPTURE LOST` rather
  than freezing on the last frame.

### P1 — Platform plumbing

- **Engine discovery:** a per-platform search order; a clear "Install Stockfish"
  screen if missing. Long term, offer a separate, checksum-verified download of the
  official binary rather than bundling it (GPLv3 compliance is simpler).
- **Paths:** `platformdirs.user_config_dir("ChessVision")` etc.; logs in the same
  tree; an "Open config folder" button.
- **Packaging:**
  - Windows: PyInstaller one-folder build, an Inno Setup installer, optional
    MSIX; test on a clean VM without Python.
  - macOS: PyInstaller or py2app `.app`, then codesign + notarize; DMG; universal2
    binary or separate Intel/Apple Silicon builds; TCC onboarding.
  - Linux: PyPI + AppImage + Flatpak; document the portal permission.
- **CI:** GitHub Actions matrix for lint, unit tests, synthetic recognition tests,
  and packaging. No live capture in CI; use recorded frames. Add a smoke test that
  imports the packaged app and opens the capture factory.
- **Versioning:** `__version__`, a changelog, and a release checklist.

### P2 — Make it feel like a product

- **First-run wizard:** explain capture, pick a source, show a live preview, drag
  the board box, auto-detect the theme, test the engine, and finish. One screen per
  decision; remember everything.
- **Auto board detection:** find the 64-square grid without a manual drag using
  line/edge detection (Hough + contour + square-fit), then verify with the
  recognizer. Keep the manual box as the override. This removes the biggest
  calibration failure.
- **Click-through overlay:** a frameless, transparent, always-on-top window that
  draws only the arrow over the live board. Tk cannot do this reliably on all
  platforms; evaluate PySide6/Qt or a small Tauri/native shell. The overlay is
  the feature that makes the tool feel native rather than a second window.
- **Global hotkeys:** show/hide, resync, flip, side, skill. A tray icon with the
  same actions.
- **Window picker:** a native source/window list with thumbnails, remembered per
  site; avoid the generic portal dialog where possible.
- **Better status:** explain *why* a read is wrong, show capture health, and offer
  one-click fixes (`Flip`, `Recalibrate`, `Change theme`).

### P3 — Analysis that teaches

- Multi-PV (3 lines) with the principal variation, not just one move.
- Threats: highlight the opponent's best reply and the piece under attack.
- A short "why" line: material, king safety, a hanging piece, a tactic motif.
- Opening names from a small ECO table; eval bar over the last N moves.
- Move history/PGN export, copy FEN, save a position.
- Engine controls: threads, hash, depth/time, Syzygy tablebases (local).
- A review mode that scans a finished PGN and flags blunders — no live play needed.

### P3 — Reliability and polish

- Watchdog and capture reconnect after sleep, monitor changes, and permission
  revocation.
- Frame-diff optimization: skip recognition when the board pixels did not change;
  adaptive engine time; measure CPU on a laptop.
- Localization-ready strings; high-contrast theme; scalable UI; keyboard navigation.
- A "privacy" panel: no network by default, capture indicator, delete local data.
- Debug bundle export (logs + last snapshot) with user confirmation.

## Cross-platform milestones

| Milestone | Deliverable | Acceptance criteria | Effort |
| --- | --- | --- | --- |
| **M0 — now** | Linux/GNOME live hints, docs, icon | Current behavior; self-test passes | Done |
| **M1 — Linux hardening** | Capture abstraction, platform paths, logging, golden tests, PyPI/AppImage | Clean-machine install; three themes read 64/64; no self-capture | M |
| **M2 — Windows** | Windows Graphics Capture backend, monitor/window picker, engine discovery, installer | Windows 11 at 125% and 150%, one and two monitors: read start position, hint in <2 s, survives alt-tab/resize, no admin rights | L |
| **M3 — macOS** | ScreenCaptureKit backend, TCC onboarding, notarized `.app`/DMG | macOS 14/15 Intel and Apple Silicon, Retina 2×: TCC flow, window/monitor capture, hint in <2 s | L |
| **M4 — Overlay/UX** | Click-through arrow overlay, tray, global hotkeys, first-run wizard | Overlay sits on the board, clicks pass through, show/hide works, no focus steal | L |
| **M5 — Analysis** | Multi-PV, threats, eval bar, PGN/review mode | A reviewed game produces a blunder list; no live-play assistance implied | M |
| **M6 — Distribution** | Flatpak, Windows installer, macOS DMG, docs site, changelog | A new user can install and get a hint without a terminal | M |

## Cross-platform acceptance checklist

- [ ] Capture factory selects the right backend; a missing permission shows a
      human-readable fix, not a stack trace.
- [ ] Start position reads 32/32 with the default theme on each OS.
- [ ] A hinted move is correct after `1.e4`, `1.d4`, and a middlegame position.
- [ ] 100%, 125%, 150%, and 200% scaling; Retina 2×.
- [ ] Single and dual monitor; board on the secondary display.
- [ ] Window capture follows a move/resize; screen capture parks the coach clear.
- [ ] Sleep/wake and monitor hotplug recover without a restart.
- [ ] Engine missing, capture denied, and theme mismatch each have a one-click fix.
- [ ] No network access during normal use.
- [ ] Installer/uninstall leaves no orphaned processes or files outside the config
      directory.

## Risks and open questions

| Risk | Mitigation |
| --- | --- |
| OS capture APIs and permissions change | Keep a fallback backend; test on the oldest supported OS; document permissions. |
| Code signing/notarization cost and complexity | Budget an Apple Developer account; sign Windows builds; publish checksums. |
| Tk limits HiDPI, transparency, and click-through | Prototype the overlay in Qt/Tauri behind an interface; keep the Tk GUI as the fallback. |
| Stockfish GPLv3 and bundled assets | Prefer a user-initiated download; ship licenses and source offers; audit chess.com asset terms. |
| Online chess fair play | Keep the warning; do not add evasion, stealth, or automated play; design for study/unrated use. |
| Large installers (Python + Qt + engine) | One-folder builds, lazy downloads, clear disk usage; measure. |
| Maintenance across three OSes | CI matrix, recorded-frame tests, a small capture surface per platform. |

Open questions:

- Who is the primary user: a developer analysing their own games, or a non-technical
  player? This changes the packaging and UI investment.
- License: MIT/Apache for the code? GPLv3 compatibility with the engine?
- Bundle Stockfish or download it on first run? Bundling is easier; downloading is
  cleaner legally and smaller.
- Overlay: separate always-on-top window or a true click-through layer? The latter
  is the better product but a bigger platform-specific job.
- Is browser/DOM integration acceptable as an optional source, or does it undermine
  the pixel-based, site-agnostic design? If added, make it opt-in and never for
  rated online games.

## What I would do next, in order

1. Extract `Capture` and `open_capture()`; move Mutter and portal behind it without
   changing behavior. Add a fake capture that replays recorded frames.
2. Add `platformdirs`, structured logging, and a clean config migration.
3. Build the golden-image test set and a tiny CI matrix.
4. Spike Windows Graphics Capture on a VM; measure frame latency at 150% DPI.
5. Spike ScreenCaptureKit on a Mac; solve the TCC prompt and 2× Retina frames.
6. Prototype the click-through overlay in Qt or Tauri; compare against Tk.
7. Package Linux (AppImage/Flatpak) and Windows (Inno Setup); write the release
   checklist.
8. Choose a license and do the Stockfish/asset review before any public release.

## Definition of done for the next release

- The app is installable on a clean Linux machine without a terminal, or with one
  documented command.
- A new user can go from launch to a correct hint in under two minutes.
- A wrong theme, stale ROI, or lost capture is surfaced with a one-click fix.
- The self-test and golden-image suite pass in CI.
- The README, Plan, license, changelog, and screenshots are current.
