# ChessVision

Watches a chessboard on your screen and shows the engine's best reply for **your
side** in a small always-on-top window, with an arrow on the board. Recommended mode
builds exact templates from the provided board/piece assets (`--pieces`, `--board`),
so the piece colours are known precisely. A screenshot calibration still works as a
fallback.

Play White or Black — the side is **fixed**: it changes only with `--side` or the `s`
key, never on its own. The program suggests your moves and stays quiet while your
opponent moves. The opponent can be a chess.com bot or a person. After each opponent
move the window shows your best reply, including `mate in N` when a forced mate
exists. It uses Stockfish, so "checkmate in the fewest moves" comes for free, and the
engine strength is adjustable.

## Requirements

Linux with the XDG ScreenCast portal (tested on GNOME 48 Wayland; X11 also works
through the same portal). On this machine everything is already installed:

- `python3-gi`, GStreamer with `pipewiresrc`, `xdg-desktop-portal-gnome`
- OpenCV, NumPy (system/user site packages)
- `python-chess` (in `.venv`)
- Stockfish 17 at `~/.local/bin/stockfish`
- Board/piece PNGs in `assets/boards` and `assets/pieces` (bundled from
  <https://github.com/GiorgioMegrelli/chess.com-boards-and-pieces>, branch v2.x)

On another Debian/Ubuntu machine:

```bash
sudo apt install python3-gi gstreamer1.0-pipewire xdg-desktop-portal-gnome \
                 python3-opencv python3-numpy stockfish
python3 -m venv --system-site-packages .venv
.venv/bin/pip install python-chess
```

## Calibrate once

Pick your theme names from `assets/boards` and `assets/pieces`, then (add
`--side black` if you play Black):

```bash
cd /home/walktheearth/chessvision
.venv/bin/python chessvision.py --recalibrate --side white \
    --pieces 8_bit --board icy_sea
```

1. GNOME asks what to share. Pick the **screen** that shows the board and click
   Share. (One permission prompt per run; that is how Wayland screen capture works.)
2. Drag a box around exactly the **64 squares**, not the coordinates or any border,
   then press ENTER.
3. Asset mode reads the current position (it warns if it cannot see both kings);
   the board can be at any position. It also checks the chosen piece set against the
   live screen and switches to a clearly better-fitting set automatically, so a wrong
   `--pieces` name cannot silently produce colour confusion. The theme is remembered,
   so later runs just need `chessvision.py`.

Screenshot mode (no `--pieces/--board`) is the fallback: put chess.com on the
**starting position**, run `--recalibrate`, and the tool checks the position against
the screenshot. Asset mode is more reliable because it knows the exact piece colours
and does not depend on a screenshot.

Calibrate on the actual game board. If you play Black, start the game first (chess.com
may flip the board for you); a flip after calibration is also detected automatically,
and `f` fixes it immediately. `--bottom white|black` says which side is at the bottom
if the automatic choice is wrong.

This saves `config.json` and `templates.npz` next to the script. Keep the browser
window at the same size and position while using the tool; if you resize or move the
board, recalibrate.

## Run

```bash
.venv/bin/python chessvision.py                 # remembers side, theme, skill
.venv/bin/python chessvision.py --side black    # switch side (persists)
```

Your side is fixed — it only changes with `--side` or `s`; nothing is inferred from
the board. Suggestions are produced only on your turn, so you will never be handed a
move for the opponent.

Approve the share prompt, then play. The small window shows:

- `Best: Nf3   +0.35` or `Best: Qxf7#   mate in 1` (score from your side's view)
- `Waiting for White...` / `Waiting for Black...` while it is the opponent's turn
- `Checkmate - you win! GG` / `Draw`
- `Reading board...` while a position is unclear, then `Resynced...` automatically

Keys: `q` quits, `s` switches sides, `f` flips the board, `r` forces a resync,
`[` / `]` change Stockfish strength, `d` saves a debug snapshot, `e` is a secret.

The tracker automatically recovers a single misread square and, if a position stays
unreadable for about 3 seconds, rebuilds it straight from the screen. It tries both
board orientations, so a board that chess.com flipped (for example when a game starts
as Black) is detected automatically and keeps its turn and castling rights. If the
colours ever look swapped, press `f` to flip immediately. A rebuild from a mid-game
position cannot know castling rights, so they are dropped (the program will not
suggest an illegal castle), and the side to move is inferred. Press `r` only when it
is your turn.

Keep the ChessVision window **off the board**. It is always on top, and the screen
share includes it; if it overlaps a square, that square cannot be read.

## Options

```bash
--side white    # or black; remembered in config.json
--pieces 8_bit  # piece set folder under assets/pieces; remembered
--board icy_sea # board PNG under assets/boards; remembered
--bottom white  # side at the bottom of the board (default: saved, else your side)
--screenshot    # ignore the asset theme and use screenshot-calibration templates
--assets DIR    # different asset folder (default: ./assets)
--skill 20      # Stockfish Skill Level 0-20; remembered; 20 = full strength
--engine PATH   # UCI engine, default: stockfish from PATH / ~/.local/bin
--think 1.0     # seconds of engine thinking per position
--threshold 0.6 # raise if a square is falsely read as a piece, lower if pieces are missed
--recalibrate   # redo the board selection (and asset templates if a theme is set)
```

`ls assets/boards assets/pieces` lists the available theme names. Once a theme is
chosen it is remembered; `--recalibrate` only re-selects the board area.

`[` and `]` adjust the skill live; the current value is saved. Lower skill values make
weaker, more human-ish suggestions without changing the time spent per move.

## Offline self-test

Uses the bundled board/piece assets to render synthetic screenshots and verify the
recognizer, move tracker and engine:

```bash
.venv/bin/python selftest.py            # uses ./assets
.venv/bin/python selftest.py /path/to/chess.com-boards-and-pieces
```

It checks five asset-built themes plus six screenshot-calibrated themes, castling and
en-passant tracking, single-square misread recovery, flip detection, promoted queens
on off-colour squares, a mate-in-1 from both sides' point of view, and exits non-zero
on any error.

## Secrets (spoilers)

ChessVision is a little silly if you poke it:

- En passant is greeted properly: `Holy hell, en passant!`
- Castling and underpromotions get their own small notes in the status bar.
- Deliver checkmate and the fish says `GG`.
- A mate in one gets a fish (`><>`).
- Press `e` in the window to turn the title into a fish. Press it again to stop.

## Limitations and troubleshooting

- It assumes standard chess (not 960) and needs a fresh calibration for a new game
  only if the board window moved. Swapping sides does not need recalibration.
- With a theme selected (`--pieces/--board`), `--recalibrate` works at any position,
  including mid-game. Starting without recalibrating uses the automatic rebuild; that
  loses castling rights and guesses the turn, so a fresh calibration is cleaner.
- Very low-contrast themes (e.g. newsprint pieces on the newsprint board) are the
  hardest. Screenshot calibration rejects them; asset mode is more robust but no
  recognizer can do much with pieces that match their board exactly.
- Screenshot-calibrated 3D sets can drift; asset mode is noticeably better for 3D
  themes (it is covered by the self-test).
- `Stockfish not found`: install it with `sudo apt install stockfish`, or pass
  `--engine /path/to/stockfish`.
- No share dialog: install/restart `xdg-desktop-portal-gnome` and
  `xdg-desktop-portal`, then log out and back in.
- Board read one square wrong repeatedly: recalibrate and select tighter around the
  64 squares. Board coordinates drawn inside the edge squares are cropped by the
  15% cell margin; turning coordinates off in chess.com also helps.
- It suggests a move you cannot play: check the side (`s` toggles; the window title
  shows White/Black). Suggestions only appear on your turn. If the board is misread,
  the status says `Reading board...` and the program resyncs itself.
- White and black pieces look swapped: the board was probably flipped after
  calibration. Press `f` (the resync does this automatically). If it persists, use
  asset mode with your exact theme — that is the most reliable fix for colour
  confusion.
- Colour confusion or impossible moves at all: switch to asset mode, e.g.
  `--recalibrate --pieces <set> --board <board>` (names from `assets/pieces` and
  `assets/boards`). Asset templates know every piece's exact colour and both square
  colours. `d` still saves a debug snapshot if something looks off.
- Adjustable strength: `--skill 0` to `--skill 20`, or `[` / `]` while running. It is
  saved in `config.json`.
- The screen is only shared while the program runs; the GNOME sharing indicator is
  shown while it is active.

This is a personal analysis/hint tool. Use it only where computer assistance is
allowed.
