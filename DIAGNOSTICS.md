# ChessVision diagnostics

_For when it stops seeing the board, suggests nonsense, or ignores your move._

Read the status card first. Most failures are one of three things: **the board
rectangle is off**, **the theme does not match the screen**, or **the board is not
visible in the capture**. Everything below is ordered around those three.

## 30-second triage

| Status card says | What it means | Do this |
| --- | --- | --- |
| `YOUR MOVE …` but the move is wrong or not legal in the real game | Recognition is reading a position that is not your game (stale read, wrong theme, or the starting position fallback) | Press `d`, then see [Compare the FEN](#compare-the-fen-to-the-real-game) |
| `WAITING FOR WHITE/BLACK…` forever | The app saw your move but not the opponent's; or the side-to-move is inverted | Check the board orientation and **Flip board**; press **Resync** (`r`) |
| `READING BOARD…` | The app cannot explain the screen. It will not suggest a move (by design) | [Board rectangle](#1-board-rectangle-misaligned-most-common) and [theme](#2-theme-mismatch) |
| `THINKING…` stuck | Stockfish died, is very slow, or the queue is blocked | Wait for `--think` seconds; if stuck, restart. Check the terminal for engine errors |
| `BOARD READ LOOKS WRONG` | The engine's move contradicted the screen read; the hint was refused | [Theme mismatch](#2-theme-mismatch), then **Change → Match screen** |
| `CANNOT RESYNC` / `CANNOT FLIP` | The screen cannot form a legal position | [Board rectangle](#1-board-rectangle-misaligned-most-common) first, then theme |
| `SCREEN CHANGED` | Capture resolution/source changed | **Recalibrate** |
| `WAITING FOR SCREEN` | No frames arriving from capture | [Capture problems](#capture-problems) |
| `ENGINE` / `ENGINE FAILED` | Stockfish missing or crashed | [Engine problems](#engine-problems) |

## The debug snapshot

Press **`d`** in the play view. It writes two files in the repo folder:

- `debug_YYYYMMDD_HHMMSS.png` — the exact image the recognizer sees (the board
  rectangle crop, not the whole screen).
- `debug_YYYYMMDD_HHMMSS.txt` — settings, the tracked FEN, and one line per square
  with what the recognizer read.

Example:

```
side=White skill=20 pieces=ejgfv board=9rdwe bottom=white frame_size=[2560, 1600]
fen=rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2
a8: br
b8: bn
...
e4: wp
e8: None
```

How to read it:

- **Image is not the 64 squares** (margins, browser chrome, part of another
  window) → the board rectangle is wrong. Recalibrate.
- **Image is right but many occupied squares say `None`** → wrong piece theme,
  wrong square alignment, or a `--threshold` that is too high.
- **Image is right but some pieces are the wrong colour** → theme mismatch or
  highlight handling. Try **Change → Match screen**.
- **FEN looks right** → recognition is fine; the problem is turn/orientation or
  the engine side.

## It does not detect the move I just played

The app only advances the board when a new stable screen equals a legal move from
its tracked position. Check in this order:

1. **Does the app's board view match the real board before you move?** If not,
   fix that first — a move can never be detected from a wrong position.
2. **Press `d` right after playing.** If the snapshot shows the new position but
   the app is on `READING BOARD…`, the read or the move match failed. If the
   snapshot still shows the old position, capture is stale (see
   [Capture problems](#capture-problems)).
3. **Wait 3 seconds.** If one square or a fast opponent move broke the match, the
   app automatically resyncs from the screen after `AUTO_RESYNC_SEC = 3.0`. If it
   recovers, it was a missed ply, not a dead app.
4. **Check the side to move.** If the app thinks it is your move when it is not
   (or vice versa), let the next real move happen — the auto-resync toggles the
   guess — or press **Resync**. The turn is inferred when the position is rebuilt
   from one screenshot.
5. **Promotion:** while the promotion picker is open, the board is unreadable.
   The app catches up after you choose the piece.
6. **Board covered:** a tooltip, menu, the ChessVision window, or another window
   over any part of the board breaks that read. The app parks itself right of the
   board for this reason.

## The three setup failures

### 1. Board rectangle misaligned (most common)

Symptom: `READING BOARD…`, debug snapshot with margins or shifted squares, zero or
garbage pieces even though the theme is right.

The rectangle must cover **only the 64 squares** — no outer frame, no coordinate
border, no page background. A drag that is ~20 px too large shifts every cell and
nothing matches. This is the single most common failure.

- Press **Recalibrate** and drag tightly from the outer edge of `a8`/`h1` (whichever
  corner the board starts at) to the outer edge of the opposite square.
- Watch the "both kings visible" line after saving. If it is not a clean read,
  drag again — do not keep playing on it.
- If the browser window moved, the old rectangle is invalid even though
  `config.json` still has it. Recalibrate.

### 2. Theme mismatch

Symptom: pieces missed, colours swapped, `BOARD READ LOOKS WRONG`, or the board
reads empty with the correct rectangle.

- Open **Change**. The left preview is your live screen; the right is the selected
  theme.
- Press **Match screen**. It reads the board and piece set from the live image.
- Press **Apply**. If it warns that the screen looks like another theme, accept
  the detected pair or press **Apply** again only if you are sure.
- On startup, the app now tries this automatically once when the saved theme
  cannot read the board. If it cannot confidently match, it stays on
  `READING BOARD…` instead of guessing.

Theme names on this machine's live chess.com board: **`ejgfv` pieces on the
`9rdwe` board**. If those names read nothing, match the screen again — chess.com
theme changes are account/game settings, not something the app infers.

### 3. Wrong orientation or side

Symptom: legal moves refused, `WAITING FOR …` stuck, hint arrow on impossible
squares.

- **YOU PLAY** must be the side you actually play. `s` switches it.
- The board can be white-bottom or black-bottom. **Flip board** (`f`) changes the
  mapping; **Resync** (`r`) re-reads it and tries both orientations.
- chess.com flips the board when you start a game as Black. The app handles this
  at startup, but a manual flip during a game can desync it.

## Compare the FEN to the real game

The snapshot's `fen` is the position the app tracked. Compare it with the real
board:

- Ignore the halfmove/fullmove counters.
- Check the side to move (the `w`/`b` field).
- Compare piece placement rank by rank; the app view shows its board picture, so
  a visual comparison is usually enough.
- If the FEN has no kings or is missing many pieces, the read is bad, not the
  engine.

A match means tracking is correct and any wrong suggestion is an engine/side
issue. A mismatch means recognition or the rectangle.

## Reset procedures

Work down this list; stop when it reads again.

1. **Resync** — button or `r`. Re-reads the current screen.
2. **Flip board** — `f`. Try this if the side/orientation looks inverted.
3. **Recalibrate** — drag the 64 squares tightly. This is the fix for rectangle
   drift.
4. **Change → Match screen → Apply** — fixes theme drift.
5. **Hard reset** (app closed):

   ```bash
   cd ~/chessvision
   rm config.json templates.npz
   .venv/bin/python chessvision.py
   ```

   It starts in calibration. Know your theme names first, or use screenshot
   calibration with the board at the starting position.

6. **Known-good settings for this machine** (only valid while the browser window
   is in its usual place):

   ```json
   {
     "roi": [0.166015625, 0.16875, 0.47890625, 0.76625],
     "side": "white",
     "skill": 20,
     "pieces": "ejgfv",
     "board": "9rdwe",
     "bottom": "white",
     "frame_size": [2560, 1600]
   }
   ```

   The `roi` above is the exact `425,270 → 1651,1496` square box on a 2560×1600
   frame. If the window moved, it is wrong; recalibrate instead.

## Capture problems

Run the app from a terminal to see capture messages on stdout:

- `Capturing monitor eDP-1 through GNOME ScreenCast (no share prompt).` — Mutter
  path, normal on this machine.
- `GNOME screen-share prompt: …` — portal fallback. Pick the browser window (best)
  or the screen.

Checks:

- **`WAITING FOR SCREEN`** — no frames. Confirm the compositor is GNOME; the
  Mutter ScreenCast API is private and can change on a GNOME upgrade. The portal
  fallback needs `xdg-desktop-portal` and `xdg-desktop-portal-gnome`.
- **Only the first monitor is captured** on the Mutter path. Put the board on the
  first monitor, or share the browser window through the portal.
- **X11 capture tools cannot see windows here.** `import`, `mss`, and `scrot`
  return black under Wayland. Use the app's own capture path or the `d` snapshot.
- **`SCREEN CHANGED`** — frame size changed (resolution, monitor, portal source).
  **Recalibrate**.
- **The app's own window on the board** — it will read its own UI. Move the window
  off the board or use window capture through the portal.

To see what the capture actually delivers (wrong monitor, black frame, board
covered), save one full frame:

```bash
.venv/bin/python - <<'PY'
import cv2, time
import chessvision as core
cap = core.ScreenCast()
frame = None
end = time.time() + 5
while frame is None and time.time() < end:
    frame = cap.grab(timeout=1)
if frame is not None:
    cv2.imwrite('/tmp/chessvision_screen.png', frame)
cap.close()
print('saved' if frame is not None else 'no frames')
PY
```

## Engine problems

- `Stockfish not found` — install it (`sudo apt install stockfish`) or pass
  `--engine /path/to/stockfish`. The app also checks `$STOCKFISH`,
  `~/.local/bin/stockfish`, and `./stockfish`.
- `ENGINE FAILED` — the binary exists but would not start. Run the path directly
  and check `stockfish` prints a banner.
- `THINKING…` hangs — the worker thread is stuck. Restart the app; lower `--think`
  if the machine is slow.
- Skill 0–20 is Stockfish's `Skill Level`, not strength in Elo. `[` / `]` change
  it live.

## Self-test

From the repo, with the app closed:

```bash
cd ~/chessvision
.venv/bin/python selftest.py
```

It renders synthetic boards from `assets/` and checks recognition, tracking,
flips, en passant, castling, promotion positions, and the engine path.
`SELFTEST PASSED` means the code and assets are fine, so a live failure is
rectangle/theme/capture. It takes a couple of minutes and needs Stockfish.

Narrow down a bad recognition theme without the full test:

```bash
.venv/bin/python - <<'PY'
import cv2
import chessvision as core
img = cv2.imread('debug_YYYYMMDD_HHMMSS.png')   # the ROI crop `d` saved
rec = core.Recognizer.load()
grid = rec.classify(img)
print(sum(p is not None for p in grid), 'pieces')
print(sorted({p for p in grid if p}))
PY
```

## Known limitations (these are not bugs)

- **Castling rights after a resync.** A position rebuilt straight from the screen
  has no castling or en-passant state, so the engine may not suggest castling at
  the exact moment. Rights are only complete when the app tracked the game from
  the first move; start it before the game starts if castling advice matters.
- **Turn inference.** When the app first reads a mid-game board, it guesses the
  side to move (your side). If the guess is wrong, let the next real move happen —
  the 3 s auto-resync toggles the guess — or press **Resync**.
- **Sparse endgames.** Theme auto-detection needs enough pieces to be confident.
  With very few pieces it refuses and leaves `READING BOARD…`; pick the theme
  manually with **Change**.
- **Fast opponent replies.** Two plies within one stable screen look like an
  illegal jump; the app waits 3 s and resyncs. It catches up but loses castling
  rights.
- **Promotion picker, drag animations, menus, tooltips** over the board are
  unreadable until they close.
- **Coordinate/highlight overlays** are handled for chess.com-style boards. Other
  sites need a matching asset theme or screenshot calibration.
- **The rectangle is not remembered per website or per monitor**; it is one saved
  box. Changing browser zoom or window size invalidates it.

## If you file a bug

Include:

- The status card text and what you did just before.
- `debug_*.png` and `debug_*.txt` from `d`.
- `config.json` and the theme label (`pieces · board · bottom · skill`).
- Whether `selftest.py` passes.
- Monitor layout, browser zoom, and whether the board is on the first monitor.

Do not include screenshots of rated games you do not want to share; the debug
image is the board crop only, but it is still your game.
