#!/usr/bin/env python3
"""ChessVision - watch a chessboard on screen, show the engine's best reply.

First run:
    python chessvision.py --recalibrate
    python chessvision.py --recalibrate --pieces 8_bit --board icy_sea

The first form calibrates from a screenshot (board at the starting position).
The second builds exact templates from the board/piece assets in ./assets, so
it can calibrate at any position and never guesses piece colours.

Then:
    python chessvision.py                 # or: --side black
    python chessvision.py --side black --skill 15

Capture uses the XDG ScreenCast portal, so GNOME asks once per run which
screen to share. Moves are suggested for the fixed side you play; the opponent
can be a computer or a person. The side only changes with --side or the s key.
"""
import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

import chess
import chess.engine
import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / 'config.json'
TEMPLATES_PATH = HERE / 'templates.npz'
WINDOW = 'ChessVision'

WORK = 640          # every captured board is resized to WORK x WORK
INSET = 0.15        # crop cell edges: drops grid lines and board coordinates
STABLE_SEC = 0.35   # a board must look identical this long before we act
THRESHOLD = 0.60    # minimum shape correlation to call a piece
OCC_RATIO = 0.25    # minimum colour residual (vs empty squares) to call a piece
FILL_WEIGHT = 0.15  # tie-break weight for the piece's fill brightness
FILL_SIGMA = 30.0   # tolerated fill-brightness difference (0-255 scale)
MAX_MISREADS = 2    # squares allowed to disagree when recovering a missed move
AUTO_RESYNC_SEC = 3.0  # how long to wait before rebuilding an unreadable position
SKILL_DEFAULT = 20  # Stockfish Skill Level, 0-20 (20 = full strength)


def flip_mapping(names):
    """The same cell order with the board seen from the other side."""
    return square_names(names[0] != 'a8')


# ---------------------------------------------------------------- recognition

def cell_patches(img):
    """Split a WORK x WORK board image into 64 inset BGR cells."""
    n = WORK // 8
    m = int(n * INSET)
    out = []
    for r in range(8):
        for c in range(8):
            cell = img[r * n:(r + 1) * n, c * n:(c + 1) * n]
            out.append(cell[m:n - m, m:n - m])
    return out


def prep(bgr):
    """Per-channel high-pass then unit-normalize: cancels square colour, tints and
    highlights while keeping piece edges and edge colour."""
    f = bgr.astype(np.float32)
    hp = f - cv2.GaussianBlur(f, (0, 0), max(1.5, bgr.shape[0] * 0.06))
    hp -= hp.mean(axis=(0, 1), keepdims=True)
    norm = np.linalg.norm(hp)
    return hp / norm if norm > 1e-3 else np.zeros_like(hp)


def square_parity(name):
    sq = chess.parse_square(name)
    return (chess.square_rank(sq) + chess.square_file(sq)) & 1  # 1 = light


def fill_brightness(cell):
    """How bright the piece is compared with its empty square. The high-pass
    feature cannot see fill colour (it is smooth), this can."""
    f = cell.astype(np.float32)
    bg = corner_colour(cell)
    weight = np.linalg.norm(f - bg, axis=2) ** 2
    lum = 0.114 * f[:, :, 0] + 0.587 * f[:, :, 1] + 0.299 * f[:, :, 2]
    bg_lum = 0.114 * bg[0] + 0.587 * bg[1] + 0.299 * bg[2]
    return float((lum * weight).sum() / (weight.sum() + 1e-6) - bg_lum)


def corner_colour(cell):
    """Colour of the cell's four corners, i.e. what the square looks like empty."""
    h, w = cell.shape[:2]
    k = max(3, int(h * 0.12))
    pts = np.concatenate([cell[:k, :k].reshape(-1, 3), cell[:k, -k:].reshape(-1, 3),
                          cell[-k:, :k].reshape(-1, 3), cell[-k:, -k:].reshape(-1, 3)])
    return np.median(pts, axis=0)


def piece_mask(cell, background):
    """Soft silhouette of the piece: differs from the square's colour. Interior
    holes are filled so the whole piece is moved to the opposite background."""
    d = np.linalg.norm(cell.astype(np.float32) - background, axis=2)
    d8 = np.clip(d / max(1.0, float(np.percentile(d, 99))) * 255, 0, 255).astype(np.uint8)
    _, mask = cv2.threshold(d8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    filled = np.zeros_like(mask)
    for contour in contours:
        if cv2.contourArea(contour) > 0.02 * mask.shape[0] * mask.shape[1]:
            cv2.drawContours(filled, [contour], -1, 255, -1)
    mask = cv2.dilate(filled, np.ones((3, 3), np.uint8), iterations=1)
    mask = cv2.GaussianBlur(mask.astype(np.float32) / 255, (0, 0), 1.0)[:, :, None]
    return np.ones_like(mask) if mask.mean() < 0.04 else mask


def square_names(white_at_bottom):
    files = 'abcdefgh'
    return [files[c] + str(8 - r) if white_at_bottom else files[7 - c] + str(r + 1)
            for r in range(8) for c in range(8)]


def white_on_bottom(img):
    """At the starting position the white back rank is the brighter edge row."""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    row = WORK // 8
    return g[-row:].mean() > g[:row].mean()


class Recognizer:
    def __init__(self, templates, labels, names, fills, occ_norm):
        self.templates = np.asarray(templates, dtype=np.float32)
        self.labels = [str(x) for x in labels]
        self.names = [str(x) for x in names]
        self.fills = np.asarray(fills, dtype=np.float32)
        self.occ_norm = max(float(occ_norm), 1e-6)

    @classmethod
    def from_start_screenshot(cls, board_img):
        img = cv2.resize(board_img, (WORK, WORK), interpolation=cv2.INTER_AREA)
        names = square_names(white_on_bottom(img))
        start = chess.Board()
        cells = cell_patches(img)
        empty_bg = {}  # what each square colour looks like with no piece on it
        for key in (0, 1):
            vals = [c.astype(np.float32) for c, n in zip(cells, names)
                    if start.piece_at(chess.parse_square(n)) is None
                    and square_parity(n) == key]
            empty_bg[key] = (np.median(np.stack(vals), axis=0) if vals
                             else np.median(np.stack(cells), axis=0).astype(np.float32))
        labels, rows, fills, norms = [], [], [], []
        for name, cell in zip(names, cells):
            piece = start.piece_at(chess.parse_square(name))
            if piece is None:
                continue
            label = ('w' if piece.color else 'b') + piece.symbol().lower()
            bg = empty_bg[square_parity(name)]
            labels.append(label)
            rows.append(prep(cell).ravel())
            fills.append(fill_brightness(cell))
            norms.append(float(np.linalg.norm(cell.astype(np.float32) - bg)))
            # The same piece on the other square colour: kings and queens are
            # only visible on one colour at the start, but can move anywhere.
            mask = piece_mask(cell, bg)
            variant = np.clip(cell * mask + empty_bg[1 - square_parity(name)] * (1 - mask),
                              0, 255).astype(np.uint8)
            labels.append(label)
            rows.append(prep(variant).ravel())
            fills.append(fill_brightness(variant))
        return cls(np.vstack(rows), labels, names, fills, np.median(norms))

    def save(self):
        np.savez_compressed(TEMPLATES_PATH, templates=self.templates,
                            labels=np.array(self.labels), names=np.array(self.names),
                            fills=self.fills, occ_norm=self.occ_norm)

    @classmethod
    def load(cls):
        d = np.load(TEMPLATES_PATH)
        return cls(d['templates'], d['labels'], d['names'], d['fills'], d['occ_norm'])

    def classify(self, board_img, threshold=THRESHOLD):
        """Return 64 labels in cell order; None = empty or not confidently a piece."""
        img = cv2.resize(board_img, (WORK, WORK), interpolation=cv2.INTER_AREA)
        grid = []
        for cell in cell_patches(img):
            residual = np.linalg.norm(cell.astype(np.float32) - corner_colour(cell))
            shape = self.templates @ prep(cell).ravel()
            fill = fill_brightness(cell)
            scores = shape + FILL_WEIGHT * np.exp(-((fill - self.fills) / FILL_SIGMA) ** 2)
            k = int(scores.argmax())
            grid.append(None if (residual / self.occ_norm < OCC_RATIO
                                 or shape[k] < threshold) else self.labels[k])
        return grid


def build_from_assets(assets_dir, board_name, pieces_name, white_at_bottom):
    """Templates composited from the theme's board and piece PNGs.

    No screenshot needed, exact piece colours, and every piece exists on both
    square colours. Matches the repo's renderer: each sprite PNG is resized to
    the full square (its own transparent padding provides the margin).
    """
    assets_dir = Path(assets_dir)
    board_path = assets_dir / 'boards' / f'{board_name}.png'
    pieces_dir = assets_dir / 'pieces' / pieces_name
    board_img = cv2.imread(str(board_path), cv2.IMREAD_COLOR)
    if board_img is None:
        known = sorted(p.stem for p in (assets_dir / 'boards').glob('*.png'))
        sys.exit(f'Unknown board {board_name!r}. Available: {", ".join(known)}')
    if not pieces_dir.is_dir():
        known = sorted(p.name for p in (assets_dir / 'pieces').iterdir() if p.is_dir())
        sys.exit(f'Unknown piece set {pieces_name!r}. Available: {", ".join(known)}')
    board_img = cv2.resize(board_img, (WORK, WORK), interpolation=cv2.INTER_AREA)
    names = square_names(white_at_bottom)
    n, m = WORK // 8, int(WORK // 8 * INSET)
    full_cells = [board_img[r * n:(r + 1) * n, c * n:(c + 1) * n]
                  for r in range(8) for c in range(8)]
    full_bg = {}
    for key in (0, 1):
        vals = [cell.astype(np.float32) for cell, name in zip(full_cells, names)
                if square_parity(name) == key]
        full_bg[key] = np.median(np.stack(vals), axis=0)

    labels, rows, fills, norms = [], [], [], []
    for code in ('wp', 'wn', 'wb', 'wr', 'wq', 'wk',
                 'bp', 'bn', 'bb', 'br', 'bq', 'bk'):
        path = pieces_dir / f'{code}.png'
        sprite = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if sprite is None:
            sys.exit(f'Piece image missing: {path}')
        sprite = cv2.resize(sprite, (n, n), interpolation=cv2.INTER_AREA)
        if sprite.shape[2] == 4:
            rgb = sprite[:, :, :3].astype(np.float32)
            alpha = sprite[:, :, 3:4].astype(np.float32) / 255
        else:
            rgb = sprite.astype(np.float32)
            alpha = np.ones((n, n, 1), np.float32)
        for key in (0, 1):
            cell = np.clip(rgb * alpha + full_bg[key] * (1 - alpha),
                           0, 255).astype(np.uint8)
            inset = cell[m:n - m, m:n - m]
            labels.append(code)
            rows.append(prep(inset).ravel())
            fills.append(fill_brightness(inset))
            norms.append(float(np.linalg.norm(
                inset.astype(np.float32) - full_bg[key][m:n - m, m:n - m])))
    return Recognizer(np.vstack(rows), labels, names, fills, np.median(norms))


def detect_piece_set(board_img, board_name, assets_dir, white_at_bottom, piece_sets):
    """Rank piece sets by how well their asset templates explain the live board.

    Used at calibration so a wrong --pieces choice (the usual cause of pieces
    being mistaken for the other colour) is corrected automatically.
    """
    img = cv2.resize(board_img, (WORK, WORK), interpolation=cv2.INTER_AREA)
    cells = cell_patches(img)
    feats = np.vstack([prep(c).ravel() for c in cells])
    residual = np.array([np.linalg.norm(c.astype(np.float32) - corner_colour(c))
                         for c in cells])
    top = np.argsort(residual)[-16:]  # the squares most likely to hold pieces
    results = []
    for pset in piece_sets:
        try:
            candidate = build_from_assets(assets_dir, board_name, pset, white_at_bottom)
        except SystemExit:
            continue
        best = (feats[top] @ candidate.templates.T).max(axis=1)
        results.append((float(np.median(best)), pset))
    results.sort(reverse=True)
    return results


def board_palette(board_img, names):
    """Median light/dark square colours of a board image."""
    img = cv2.resize(board_img, (WORK, WORK), interpolation=cv2.INTER_AREA)
    cells = cell_patches(img)
    palette = {}
    for key in (0, 1):
        vals = [corner_colour(cell) for cell, name in zip(cells, names)
                if square_parity(name) == key]
        palette[key] = np.median(np.stack(vals), axis=0)
    return palette


def detect_board(board_img, names, assets_dir):
    """Rank board themes by how close their square colours are to a live board."""
    live = board_palette(board_img, names)
    results = []
    for path in sorted((Path(assets_dir) / 'boards').glob('*.png')):
        board = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if board is None:
            continue
        sample = board_palette(board, names)
        distance = float(np.mean([np.linalg.norm(live[key] - sample[key])
                                  for key in (0, 1)]))
        results.append((distance, path.stem))
    results.sort()
    return results


def placement_fen(grid, names):
    board = chess.Board('8/8/8/8/8/8/8/8 w - - 0 1')
    for name, piece in zip(names, grid):
        if piece:
            board.set_piece_at(
                chess.parse_square(name),
                chess.Piece.from_symbol(piece[1].upper() if piece[0] == 'w' else piece[1]))
    return board.board_fen()


def placement_labels(board):
    return {chess.square_name(sq): ('w' if p.color else 'b') + p.symbol().lower()
            for sq, p in board.piece_map().items()}


def advance(board, grid, names, max_misreads=MAX_MISREADS):
    """Return (board_after, move) for the move that produces `grid`.

    (board, None) means the screen already equals the board. When no move
    matches exactly, the closest legal move is accepted if at most
    `max_misreads` squares disagree and it is unambiguous, so one bad square
    does not stop tracking. (None, None) means the position cannot be explained.
    """
    if grid is None:
        return None, None
    placement = placement_fen(grid, names)
    if placement == board.board_fen():
        return board, None
    best_distance, best_board, best_move = 10 ** 6, None, None
    second_distance = 10 ** 6
    for move in board.legal_moves:
        after = board.copy(stack=False)
        after.push(move)
        if after.board_fen() == placement:
            return after, move
        moved = after.piece_at(move.to_square)
        moved_label = ('w' if moved.color else 'b') + moved.symbol().lower()
        if grid[names.index(chess.square_name(move.to_square))] != moved_label:
            continue  # never guess a move whose landing square is not visible
        labels = placement_labels(after)
        distance = sum(1 for name, seen in zip(names, grid) if labels.get(name) != seen)
        if distance < best_distance:
            second_distance = best_distance
            best_distance, best_board, best_move = distance, after, move
        elif distance < second_distance:
            second_distance = distance
    if (best_board is not None and best_distance <= max_misreads
            and best_distance < second_distance):
        return best_board, best_move
    return None, None


def rebuild(grid, names, turn):
    """Position straight from the screen. Castling rights are not visible."""
    placement = placement_fen(grid, names)
    letter = 'w' if turn == chess.WHITE else 'b'
    board = chess.Board(f'{placement} {letter} - - 0 1')
    return board if board.is_valid() else None


def placement_distance(first, second):
    a, b = placement_labels(first), placement_labels(second)
    return sum(1 for square in set(a) | set(b) if a.get(square) != b.get(square))


def resync(grid, names, last_board, guess):
    """Best position for a screen read, trying both board orientations.

    A board can flip after calibration (chess.com does it when a game starts as
    Black). The orientation and turn that stay closest to the last known board
    win; if the position is unchanged apart from the flip, the last board is
    kept with its castling rights. Returns (board, names); names changes when
    the board orientation changed.
    """
    start = chess.Board()
    start_fen = start.board_fen()
    options = []
    for rank, candidate_names in enumerate((names, flip_mapping(names))):
        placement = placement_fen(grid, candidate_names)
        if placement == start_fen:
            options.append((placement_distance(start, last_board), rank, 0,
                            start, candidate_names))
            continue
        if placement == last_board.board_fen():
            options.append((0, rank, 0, last_board.copy(), candidate_names))
            continue
        for turn_rank, turn in enumerate((guess, not guess)):
            letter = 'w' if turn == chess.WHITE else 'b'
            candidate = chess.Board(f'{placement} {letter} - - 0 1')
            if candidate.is_valid():
                options.append((placement_distance(candidate, last_board), rank,
                                turn_rank, candidate, candidate_names))
    if not options:
        return None, names
    options.sort(key=lambda option: option[:3])
    return options[0][3], options[0][4]


def suggestion_conflicts(move, grid, names, user_color, board):
    """True when the screen contradicts the move or the tracked board.

    The arrow must start on your piece on screen, must not land on another of
    your pieces, and both squares must agree between the screen and the board
    the engine analysed. This refuses "attacking yourself" nonsense caused by a
    stale or wrong-coloured read.
    """
    if grid is None:
        return False
    mine = 'w' if user_color == chess.WHITE else 'b'

    def screen(square):
        return grid[names.index(chess.square_name(square))]

    def tracked(square):
        piece = board.piece_at(square)
        return None if piece is None else ('w' if piece.color else 'b') + piece.symbol().lower()

    source, target = screen(move.from_square), screen(move.to_square)
    if source is None or source[0] != mine:
        return True
    if target is not None and target[0] == mine:
        return True
    return source != tracked(move.from_square) or target != tracked(move.to_square)


def move_flair(before, move):
    """Easter eggs for notable moves."""
    if move is None:
        return ''
    if before.is_en_passant(move):
        return 'Holy hell, en passant!'
    if before.is_castling(move):
        return 'The king has entered the castle.'
    if move.promotion in (chess.KNIGHT, chess.BISHOP):
        return 'Underpromotion? Bold.'
    return ''


# -------------------------------------------------------------------- capture

class ScreenCast:
    """XDG ScreenCast portal -> PipeWire -> GStreamer appsink -> BGR numpy frames."""

    def __init__(self):
        import gi
        gi.require_version('Gio', '2.0')
        gi.require_version('Gst', '1.0')
        gi.require_version('GstApp', '1.0')
        from gi.repository import Gio, GLib, Gst, GstApp  # noqa: F401
        self.Gio, self.GLib, self.Gst = Gio, GLib, Gst
        Gst.init(None)
        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.sender = self.conn.get_unique_name()[1:].replace('.', '_')
        self.counter = 0
        node, fd = self._open_portal()
        self.fd = fd
        self.pipeline = Gst.parse_launch(
            f'pipewiresrc fd={fd} path={node} do-timestamp=true ! videoconvert ! '
            f'video/x-raw,format=BGR ! appsink name=sink max-buffers=1 drop=true sync=false')
        self.sink = self.pipeline.get_by_name('sink')
        self.pipeline.set_state(Gst.State.PLAYING)
        self.last = None

    def _portal_request(self, method, make_params, options):
        Gio, GLib = self.Gio, self.GLib
        token = f'chessvision{os.getpid()}_{self.counter}'
        self.counter += 1
        opts = {k: GLib.Variant('u' if type(v) is int else 'b' if type(v) is bool else 's', v)
                for k, v in options.items()}
        opts['handle_token'] = GLib.Variant('s', token)
        path = f'/org/freedesktop/portal/desktop/request/{self.sender}/{token}'
        loop, box = GLib.MainLoop(), {}

        def on_response(conn, sender, obj, iface, signal, params):
            box['resp'] = params.unpack()
            loop.quit()

        sub = self.conn.signal_subscribe(
            'org.freedesktop.portal.Desktop', 'org.freedesktop.portal.Request',
            'Response', path, None, Gio.DBusSignalFlags.NONE, on_response)
        reply = self.conn.call_sync(
            'org.freedesktop.portal.Desktop', '/org/freedesktop/portal/desktop',
            'org.freedesktop.portal.ScreenCast', method, make_params(opts),
            None, Gio.DBusCallFlags.NONE, -1, None)
        handle = reply.unpack()[0]
        if handle != path:  # portal picked a different token; listen on its handle instead
            self.conn.signal_unsubscribe(sub)
            sub = self.conn.signal_subscribe(
                'org.freedesktop.portal.Desktop', 'org.freedesktop.portal.Request',
                'Response', handle, None, Gio.DBusSignalFlags.NONE, on_response)
        if method == 'Start':
            print('GNOME screen-share prompt: pick the browser window with the chessboard '
                  '(recommended) or the screen, then Share.', flush=True)
        timeout = GLib.timeout_add_seconds(300, lambda: loop.quit() or False)
        loop.run()
        GLib.source_remove(timeout)
        self.conn.signal_unsubscribe(sub)
        if 'resp' not in box:
            raise TimeoutError(f'{method}: no answer from the screen-share portal')
        code, results = box['resp']
        if code == 1:
            sys.exit('Screen sharing was cancelled.')
        if code != 0:
            sys.exit(f'Screen-share portal error {code} during {method}.')
        return results

    def _open_portal(self):
        Gio, GLib = self.Gio, self.GLib
        res = self._portal_request(
            'CreateSession', lambda o: GLib.Variant('(a{sv})', (o,)),
            {'session_handle_token': f'cv{os.getpid()}'})
        session = res['session_handle']
        self._portal_request(
            'SelectSources', lambda o: GLib.Variant('(oa{sv})', (session, o)),
            {'types': 3, 'multiple': False, 'cursor_mode': 1})  # screens or windows
        res = self._portal_request(
            'Start', lambda o: GLib.Variant('(osa{sv})', (session, '', o)), {})
        node = res['streams'][0][0]
        reply, fd_list = self.conn.call_with_unix_fd_list_sync(
            'org.freedesktop.portal.Desktop', '/org/freedesktop/portal/desktop',
            'org.freedesktop.portal.ScreenCast', 'OpenPipeWireRemote',
            GLib.Variant('(oa{sv})', (session, {})), None,
            Gio.DBusCallFlags.NONE, -1, None, None)
        return node, os.dup(fd_list.get(reply.unpack()[0]))

    def grab(self, timeout=0.0):
        sample = self.sink.try_pull_sample(int(timeout * self.Gst.SECOND))
        if sample is None:
            return self.last
        caps = sample.get_caps().get_structure(0)
        w, h = caps.get_value('width'), caps.get_value('height')
        buf = sample.get_buffer()
        ok, info = buf.map(self.Gst.MapFlags.READ)
        if not ok:
            return self.last
        try:
            stride = info.size // h
            rows = np.ndarray((info.size,), np.uint8, info.data).reshape(h, stride)
            self.last = rows[:, :w * 3].reshape(h, w, 3).copy()
        finally:
            buf.unmap(info)
        return self.last

    def close(self):
        self.pipeline.set_state(self.Gst.State.NULL)
        os.close(self.fd)


def crop_roi(frame, roi):
    h, w = frame.shape[:2]
    x, y, rw, rh = (int(round(v * d)) for v, d in zip(roi, (w, h, w, h)))
    x, y = max(0, x), max(0, y)
    rw, rh = max(16, min(rw, w - x)), max(16, min(rh, h - y))
    return frame[y:y + rh, x:x + rw]


# --------------------------------------------------------------------- engine

def find_engine(explicit):
    for p in (explicit, os.environ.get('STOCKFISH'), shutil.which('stockfish'),
              str(Path.home() / '.local/bin/stockfish'), str(HERE / 'stockfish')):
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    sys.exit('Stockfish not found. Install it with: sudo apt install stockfish')


def side_name(color):
    return 'White' if color == chess.WHITE else 'Black'


def evaluate(engine, board, think, color=chess.WHITE):
    info = engine.analyse(board, chess.engine.Limit(time=think))
    pv = info.get('pv') or []
    if not pv:
        return None, '', 'no move found'
    move = pv[0]
    san = board.san(move)
    score = info['score'].pov(color)
    if score.is_mate():
        n = score.mate()
        verdict = f'mate in {n}' if n > 0 else f'getting mated in {-n}'
        if n == 1:
            verdict += '   ><>'
    else:
        verdict = f'{score.score() / 100:+.2f}'
    return move, san, verdict


def analyse(engine, board, think, color=chess.WHITE):
    move, san, verdict = evaluate(engine, board, think, color)
    if move is None:
        return None, verdict
    return move, f'Best: {san}   {verdict}'


# -------------------------------------------------------------------- display

def draw(board_img, rec, move, status):
    side = 460
    img = cv2.resize(board_img, (side, side), interpolation=cv2.INTER_AREA)
    if move is not None:
        from_cell = rec.names.index(chess.square_name(move.from_square))
        to_cell = rec.names.index(chess.square_name(move.to_square))
        pts = []
        for cell in (from_cell, to_cell):
            r, c = divmod(cell, 8)
            x0, y0 = c * side // 8, r * side // 8
            x1, y1 = (c + 1) * side // 8, (r + 1) * side // 8
            tint = img.copy()
            cv2.rectangle(tint, (x0, y0), (x1 - 1, y1 - 1), (0, 220, 0), -1)
            cv2.addWeighted(tint, 0.35, img, 0.65, 0, img)
            pts.append(((x0 + x1) // 2, (y0 + y1) // 2))
        cv2.arrowedLine(img, pts[0], pts[1], (0, 255, 0),
                        max(2, side // 110), cv2.LINE_AA, tipLength=0.22)
    out = cv2.copyMakeBorder(img, 40, 0, 0, 0, cv2.BORDER_CONSTANT, value=(28, 28, 28))
    scale = 0.62
    (text_w, _), _ = cv2.getTextSize(status, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    if text_w > side - 16:
        scale *= (side - 16) / text_w
    cv2.putText(out, status, (8, 27), cv2.FONT_HERSHEY_SIMPLEX, scale,
                (255, 255, 255), 1, cv2.LINE_AA)
    cv2.imshow(WINDOW, out)


# ----------------------------------------------------------------- flow

def select_roi(cap):
    frame = cap.grab(timeout=30)
    if frame is None:
        sys.exit('No frames arrived from the screen share.')
    h, w = frame.shape[:2]
    scale = min(1.0, 1200.0 / max(w, h))
    preview = cv2.resize(frame, None, fx=scale, fy=scale) if scale < 1 else frame
    print('Drag a box around the 64 squares (inside any border/coordinates), then press ENTER.')
    x, y, bw, bh = cv2.selectROI('Select the chessboard', preview, showCrosshair=False)
    cv2.destroyAllWindows()
    if bw == 0 or bh == 0:
        sys.exit('No board area selected.')
    return frame, [x / scale / w, y / scale / h, bw / scale / w, bh / scale / h]


def calibrate(args, cap, side_key, skill):
    frame, roi = select_roi(cap)
    rec = Recognizer.from_start_screenshot(crop_roi(frame, roi))

    start = chess.Board()
    truth = {chess.square_name(sq): ('w' if p.color else 'b') + p.symbol().lower()
             for sq, p in start.piece_map().items()}
    got = {n: p for n, p in zip(rec.names,
                                rec.classify(crop_roi(frame, roi), args.threshold)) if p}
    if got != truth:
        bad = sorted(set(got) ^ set(truth) or
                     {k for k in got.keys() & truth.keys() if got[k] != truth[k]})
        print('Mismatched squares:', bad[:16])
        sys.exit('Calibration failed: the board must show the starting position and be '
                 'selected exactly, nothing may be moving, and the theme needs enough '
                 'contrast between pieces and squares (very low-contrast piece/board '
                 'combinations cannot be read).')

    CONFIG_PATH.write_text(json.dumps({
        'roi': roi, 'side': side_key, 'skill': skill,
        'bottom': 'white' if rec.names[0] == 'a8' else 'black',
        'frame_size': [int(frame.shape[1]), int(frame.shape[0])]}, indent=2))
    rec.save()
    print(f'Calibrated. You play {side_key.capitalize()}, Stockfish skill {skill}.',
          'White is at the bottom.' if rec.names[0] == 'a8'
          else 'Board is flipped: White is at the top.')


def calibrate_assets(cap, side_key, skill, assets_dir, board_name, pieces_name, bottom):
    frame, roi = select_roi(cap)
    roi_img = crop_roi(frame, roi)
    sets = sorted(p.name for p in (Path(assets_dir) / 'pieces').iterdir() if p.is_dir())
    ranking = detect_piece_set(roi_img, board_name, assets_dir, bottom == 'white', sets)
    if ranking:
        print('Piece-set match:', ', '.join(f'{name} {score:.2f}'
                                            for score, name in ranking[:3]))
        best_score, best_set = ranking[0]
        current_score = next((score for score, name in ranking if name == pieces_name), 0.0)
        if best_set != pieces_name and best_score > current_score + 0.02:
            print(f'{pieces_name} pieces do not fit the screen; using {best_set} instead.')
            pieces_name = best_set
    rec = build_from_assets(assets_dir, board_name, pieces_name, bottom == 'white')
    CONFIG_PATH.write_text(json.dumps({
        'roi': roi, 'side': side_key, 'skill': skill, 'pieces': pieces_name,
        'board': board_name, 'bottom': bottom,
        'frame_size': [int(frame.shape[1]), int(frame.shape[0])]}, indent=2))
    rec.save()
    print(f'Calibrated from assets: {pieces_name} pieces on the {board_name} board.')
    kings = sum(1 for piece in rec.classify(roi_img) if piece in ('wk', 'bk'))
    if kings != 2:
        print('Warning: could not see both kings in the current frame - '
              'check that the selected area is the whole board.')


def run(args, cap, engine_path, side, skill):
    cfg = json.loads(CONFIG_PATH.read_text())
    roi = cfg['roi']
    rec = Recognizer.load()
    engine = chess.engine.SimpleEngine.popen_uci(engine_path)
    skill_option = engine.options.get('Skill Level')
    skill_low = skill_option.min if skill_option else 0
    skill_high = skill_option.max if skill_option else 20
    skill_level = max(skill_low, min(skill_high, skill))
    if skill_option:
        engine.configure({'Skill Level': skill_level})
    user_color = side
    board = chess.Board()
    start_placement = chess.Board().board_fen()
    pending = committed = None
    pending_since = 0.0
    move, status = None, 'Watching the board...'
    tracking_ok = True
    unknown_since = 0.0
    missed = 0
    fish = False
    cv2.namedWindow(WINDOW, cv2.WINDOW_AUTOSIZE)
    cv2.setWindowProperty(WINDOW, cv2.WND_PROP_TOPMOST, 1)

    def set_title():
        cv2.setWindowTitle(WINDOW, '><> ChessVision ><>' if fish
                           else f'ChessVision ({side_name(user_color)})')

    def think(roi_img, grid, prefix):
        draw(roi_img, rec, None, prefix)
        cv2.waitKey(1)
        found, text = analyse(engine, board, args.think, user_color)
        if found is not None and suggestion_conflicts(found, grid, rec.names,
                                                      user_color, board):
            return None, 'Board read looks wrong - press f or r'
        return found, text

    set_title()
    print(f'Watching. You play {side_name(user_color)}, skill {skill_level}/{skill_high}. '
          'q quit, s side, f flip, r resync, [ ] power, d snapshot, e secret. ><>')
    try:
        while True:
            frame = cap.grab()
            if frame is None:
                cv2.waitKey(50)
                continue
            roi_img = crop_roi(frame, roi)
            grid = rec.classify(roi_img, args.threshold)
            now = time.time()

            if grid != pending:
                pending, pending_since = grid, now
            elif grid != committed and now - pending_since >= STABLE_SEC:
                committed = grid
                before = board
                if placement_fen(grid, rec.names) == start_placement:
                    after, made = chess.Board(), None
                else:
                    after, made = advance(board, grid, rec.names)
                if after is None:
                    missed += 1
                    if tracking_ok:
                        unknown_since = now
                    tracking_ok = False
                    move = None
                    status = 'Reading board...'
                else:
                    board, tracking_ok, missed = after, True, 0
                    flair = move_flair(before, made)
                    move = None
                    if board.is_checkmate():
                        status = ('Checkmate - you win! GG' if board.turn != user_color
                                  else 'Checkmate - you lose')
                    elif board.is_game_over():
                        status = 'Draw'
                    elif board.turn != user_color:
                        status = f'Waiting for {side_name(board.turn)}...'
                    else:
                        move, status = think(roi_img, grid, 'Thinking...')
                    if flair:
                        status = f'{status}   {flair}'

            if (not tracking_ok and committed is not None and pending == committed
                    and now - unknown_since >= AUTO_RESYNC_SEC):
                guess = board.turn if missed % 2 == 0 else not board.turn
                rebuilt, chosen_names = resync(committed, rec.names, board, guess)
                unknown_since = now
                if rebuilt is not None:
                    flipped = chosen_names != rec.names
                    rec.names = chosen_names
                    board, tracking_ok, missed, move = rebuilt, True, 0, None
                    if board.is_game_over():
                        status = 'Resynced (flipped board)' if flipped else 'Resynced'
                    elif board.turn == user_color:
                        move, status = think(roi_img, committed, 'Resynced - thinking...')
                        status = ('Resynced (flipped board). ' if flipped
                                  else 'Resynced. ') + status
                    else:
                        status = (('Resynced (flipped board) - ' if flipped
                                   else 'Resynced - ')
                                  + f'{side_name(board.turn)} to move')

            draw(roi_img, rec, move, status)
            key = cv2.waitKey(40) & 0xff
            if key in (27, ord('q')):
                break
            if key == ord('s'):
                user_color = not user_color
                cfg['side'] = side_name(user_color).lower()
                CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
                set_title()
                move = None
                if board.turn == user_color and not board.is_game_over():
                    current = committed if committed is not None else pending
                    move, status = think(roi_img, current, 'Switched sides - thinking...')
                else:
                    status = (f'Now playing {side_name(user_color)}. '
                              + ('Betrayal detected.' if user_color == chess.BLACK
                                 else 'Redemption arc.'))
            if key == ord('f') and pending is not None:
                rec.names = flip_mapping(rec.names)
                source = committed if committed is not None else pending
                if placement_fen(source, rec.names) == start_placement:
                    rebuilt = chess.Board()
                else:
                    rebuilt = rebuild(source, rec.names, user_color)
                    if rebuilt is None:
                        rebuilt = rebuild(source, rec.names, not user_color)
                if rebuilt is None:
                    rec.names = flip_mapping(rec.names)
                    status = 'Cannot flip - check the board'
                else:
                    board, committed, move = rebuilt, source, None
                    tracking_ok, missed = True, 0
                    if board.is_game_over():
                        status = 'Flipped'
                    elif board.turn == user_color:
                        move, status = think(roi_img, source, 'Flipped - thinking...')
                    else:
                        status = f'Flipped - {side_name(board.turn)} to move'
            if key == ord('r') and pending is not None:
                rebuilt, chosen_names = resync(pending, rec.names, board, user_color)
                if rebuilt is None:
                    status = 'Cannot resync - check the board'
                else:
                    flipped = chosen_names != rec.names
                    rec.names = chosen_names
                    board, committed, move = rebuilt, pending, None
                    tracking_ok, missed = True, 0
                    if board.is_game_over():
                        status = 'Resynced (flipped board)' if flipped else 'Resynced'
                    elif board.turn == user_color:
                        move, status = think(roi_img, pending, 'Resynced - thinking...')
                        status = ('Resynced (flipped board). ' if flipped
                                  else 'Resynced. ') + status
                    else:
                        status = (('Resynced (flipped board) - ' if flipped
                                   else 'Resynced - ')
                                  + f'{side_name(board.turn)} to move')
            if key in (ord('['), ord(']')):
                skill_level = max(skill_low, min(
                    skill_high, skill_level + (1 if key == ord(']') else -1)))
                if skill_option:
                    engine.configure({'Skill Level': skill_level})
                cfg['skill'] = skill_level
                CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
                status = f'Stockfish skill {skill_level}/{skill_high}'
            if key == ord('d'):
                stamp = time.strftime('%Y%m%d_%H%M%S')
                image_path = HERE / f'debug_{stamp}.png'
                text_path = HERE / f'debug_{stamp}.txt'
                cv2.imwrite(str(image_path), roi_img)
                current = committed if committed is not None else pending
                shown = current if current is not None else [None] * 64
                text_path.write_text(
                    f'side={side_name(user_color)} skill={skill_level} '
                    f'white_at_bottom={rec.names[0] == "a8"}\n'
                    f'tracked_fen={board.fen()}\n'
                    + '\n'.join(f'{name}: {piece}'
                                for name, piece in zip(rec.names, shown)) + '\n')
                print('Saved', image_path, text_path)
                status = f'Saved {image_path.name}'
            if key == ord('e'):
                fish = not fish
                set_title()
    finally:
        engine.close()
        cap.close()
        cv2.destroyAllWindows()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--recalibrate', action='store_true',
                    help='re-select the board area and rebuild the templates')
    ap.add_argument('--side', choices=('white', 'black'),
                    help='side you play (default: last used, else white)')
    ap.add_argument('--skill', type=int,
                    help=f'Stockfish Skill Level 0-20 (default: last used, else {SKILL_DEFAULT})')
    ap.add_argument('--pieces',
                    help='piece set folder under assets/pieces, e.g. classic or 8_bit')
    ap.add_argument('--board',
                    help='board image under assets/boards, e.g. brown or icy_sea')
    ap.add_argument('--assets', help='asset repo folder (default: ./assets)')
    ap.add_argument('--bottom', choices=('white', 'black'),
                    help='side at the bottom of the board (default: last used, else your side)')
    ap.add_argument('--screenshot', action='store_true',
                    help='ignore --pieces/--board and use the screenshot calibration')
    ap.add_argument('--cli', action='store_true', help='terminal/OpenCV mode instead of the GUI')
    ap.add_argument('--engine', help='path to a UCI engine (default: stockfish)')
    ap.add_argument('--think', type=float, default=1.0, help='engine seconds per move (default 1.0)')
    ap.add_argument('--threshold', type=float, default=THRESHOLD,
                    help=f'piece-match threshold (default {THRESHOLD})')
    args = ap.parse_args()

    if not args.cli:
        import gui
        gui.launch(args)
        return

    cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    side_key = args.side or cfg.get('side', 'white')
    side = chess.WHITE if side_key == 'white' else chess.BLACK
    skill = args.skill if args.skill is not None else int(cfg.get('skill', SKILL_DEFAULT))
    skill = max(0, min(20, skill))
    assets_dir = Path(args.assets).expanduser() if args.assets else HERE / 'assets'
    pieces = None if args.screenshot else (args.pieces or cfg.get('pieces'))
    board = None if args.screenshot else (args.board or cfg.get('board'))
    if bool(pieces) != bool(board):
        sys.exit('Pass both --pieces and --board (see assets/pieces and assets/boards).')
    use_assets = bool(pieces and board)
    if use_assets:
        if not (assets_dir / 'boards' / f'{board}.png').is_file():
            known = sorted(p.stem for p in (assets_dir / 'boards').glob('*.png'))
            sys.exit(f'Unknown board {board!r} in {assets_dir / "boards"}. '
                     f'Available: {", ".join(known)}')
        if not (assets_dir / 'pieces' / pieces).is_dir():
            known = sorted(p.name for p in (assets_dir / 'pieces').iterdir() if p.is_dir())
            sys.exit(f'Unknown piece set {pieces!r} in {assets_dir / "pieces"}. '
                     f'Available: {", ".join(known)}')
    engine_path = find_engine(args.engine)  # fail before asking to share the screen
    cap = None

    if use_assets:
        bottom = args.bottom or cfg.get('bottom')
        if bottom not in ('white', 'black'):
            if TEMPLATES_PATH.exists():
                try:
                    bottom = 'white' if Recognizer.load().names[0] == 'a8' else 'black'
                except Exception:
                    bottom = 'white' if side_key == 'white' else 'black'
            else:
                bottom = 'white' if side_key == 'white' else 'black'
        need_roi = args.recalibrate or not CONFIG_PATH.exists()
        rebuild = (args.recalibrate or not TEMPLATES_PATH.exists()
                   or cfg.get('pieces') != pieces or cfg.get('board') != board
                   or cfg.get('bottom') != bottom)
        if need_roi:
            cap = ScreenCast()
            calibrate_assets(cap, side_key, skill, assets_dir, board, pieces, bottom)
        else:
            if rebuild:
                rec = build_from_assets(assets_dir, board, pieces, bottom == 'white')
                rec.save()
                print(f'Using {pieces} pieces on the {board} board.')
            cfg.update({'side': side_key, 'skill': skill, 'pieces': pieces,
                        'board': board, 'bottom': bottom})
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    else:
        need_calibration = args.recalibrate or not (CONFIG_PATH.exists()
                                                    and TEMPLATES_PATH.exists())
        if need_calibration:
            cap = ScreenCast()
            calibrate(args, cap, side_key, skill)
        cfg = json.loads(CONFIG_PATH.read_text())
        cfg.update({'side': side_key, 'skill': skill})
        cfg.pop('pieces', None)
        cfg.pop('board', None)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    if cap is None:
        cap = ScreenCast()
    try:
        run(args, cap, engine_path, side, skill)
    except KeyboardInterrupt:
        print('\nStopped.')


if __name__ == '__main__':
    main()
