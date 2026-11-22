#!/usr/bin/env python3
"""Offline recognition check: renders synthetic screenshots from chess.com
board/piece PNGs, then verifies calibration, recognition, move tracking and
the engine path.

Usage:
    python selftest.py /path/to/chess.com-boards-and-pieces [--positions 30]
"""
import argparse
import random
import sys
from pathlib import Path

import chess
import chess.engine
import cv2
import numpy as np

import chessvision as cv_


def load_piece(assets, pset, code):
    img = cv2.imread(str(assets / 'pieces' / pset / f'{code}.png'), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise FileNotFoundError(f'missing piece {pset}/{code}.png')
    return img


def render(fen, board_path, assets, pset, size=720, flip=False, last_move=None, coords=True):
    board_img = cv2.resize(cv2.imread(str(board_path)), (size, size),
                           interpolation=cv2.INTER_AREA)
    cell = size // 8
    brd = chess.Board(fen)

    def cell_of(sq):
        rank, file = chess.square_rank(sq), chess.square_file(sq)
        return (rank if flip else 7 - rank), (7 - file if flip else file)

    # last-move tint, under the pieces (like the website)
    if last_move:
        tint = board_img.copy()
        for sq in last_move:
            r, c = cell_of(sq)
            cv2.rectangle(tint, (c * cell, r * cell), ((c + 1) * cell, (r + 1) * cell),
                          (0, 190, 255), -1)
        board_img = cv2.addWeighted(tint, 0.30, board_img, 0.70, 0)

    # chess.com-style coordinates inside the edge squares, under the pieces
    fnt, fs, th = cv2.FONT_HERSHEY_SIMPLEX, cell * 0.006, max(1, cell // 45)
    for c in [] if not coords else range(8):
        col = (40, 40, 40) if c % 2 else (230, 230, 230)
        cv2.putText(board_img, 'abcdefgh'[c], (c * cell + 3, 8 * cell - 5),
                    fnt, fs, col, th, cv2.LINE_AA)
    for r in [] if not coords else range(8):
        col = (40, 40, 40) if r % 2 == 0 else (230, 230, 230)
        cv2.putText(board_img, str(8 - r), (3, r * cell + int(cell * 0.22)),
                    fnt, fs, col, th, cv2.LINE_AA)

    for sq, piece in brd.piece_map().items():
        code = ('w' if piece.color else 'b') + piece.symbol().lower()
        # the repo's renderer resizes the whole sprite to the square; its
        # transparent padding is what leaves the margins
        p = cv2.resize(load_piece(assets, pset, code), (cell, cell),
                       interpolation=cv2.INTER_AREA)
        rgb, a = p[:, :, :3].astype(np.float32), p[:, :, 3:4].astype(np.float32) / 255
        r, c = cell_of(sq)
        y0, x0 = r * cell, c * cell
        roi = board_img[y0:y0 + cell, x0:x0 + cell].astype(np.float32)
        board_img[y0:y0 + cell, x0:x0 + cell] = (a * rgb + (1 - a) * roi).astype(np.uint8)
    return board_img


def labels_of(brd):
    return {chess.square_name(sq): ('w' if p.color else 'b') + p.symbol().lower()
            for sq, p in brd.piece_map().items()}


def grid_of(brd, names):
    labs = labels_of(brd)
    return [labs.get(n) for n in names]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('assets', type=Path, nargs='?', default=cv_.HERE / 'assets')
    ap.add_argument('--positions', type=int, default=24)
    ap.add_argument('--engine', default=None)
    args = ap.parse_args()
    A = args.assets

    boards = [p.stem for p in sorted((A / 'boards').glob('*.png'))]
    psets = [p.name for p in sorted((A / 'pieces').iterdir()) if p.is_dir()]
    if not boards or not psets:
        sys.exit(f'No boards/pieces found under {A}')

    random.seed(7)
    wanted = [('brown', 'classic'), ('orange', 'alpha'), ('burled_wood', 'sky'),
              ('4fs27', '3d_chesskid'), ('marble', 'neo_wood'),
              ('sand', 'tournament')]
    combos = [(b, p) for b, p in wanted if b in boards and p in psets]
    if not combos:
        sys.exit('None of the reference board/piece themes were found.')
    extras = [
        'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBN1 w - - 0 1',  # h1 empty (watermark)
        '8/P6k/8/8/8/8/8/K7 w - - 0 1',                            # promotion ready
        '1Q3Q2/8/4k3/8/8/8/8/4K3 w - - 0 1',                       # queens off home squares
        'Q4Q2/2q5/4k3/8/2Q5/8/8/4K3 w - - 0 1',                    # more queens, both colours
        'r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1',                    # castling position
    ]
    engine_path = cv_.find_engine(args.engine)
    engine = chess.engine.SimpleEngine.popen_uci(engine_path)
    for line in (['e4', 'd5', 'e5', 'f5', 'exf6'],                       # en passant
                 ['e4', 'e5', 'Nf3', 'Nc6', 'Bc4', 'Be7', 'O-O', 'Nf6',  # castling
                  'Re1', 'O-O']):
        line_board, tracked = chess.Board(), chess.Board()
        for san in line:
            line_board.push_san(san)
            tracked, made = cv_.advance(tracked, grid_of(line_board, cv_.square_names(True)),
                                        cv_.square_names(True))
            assert tracked is not None and tracked.fen() == line_board.fen(), f'tracker: {san}'
            assert made is not None, f'tracker move: {san}'
    print('scripted tracker (castling, en passant): OK')

    # One misread square must not stop tracking; many must be rejected.
    names = cv_.square_names(True)
    start = chess.Board()
    after = start.copy()
    after.push_san('e4')
    clean = grid_of(after, names)
    dirty = list(clean)
    dirty[names.index('a2')] = 'br'
    recovered, made = cv_.advance(start, dirty, names)
    assert recovered is not None and made == chess.Move.from_uci('e2e4'), 'misread recovery'
    wrecked = list(clean)
    for square in ('a2', 'b2', 'c2', 'd2', 'a1'):
        wrecked[names.index(square)] = 'br'
    assert cv_.advance(start, wrecked, names) == (None, None), 'wild read rejected'
    print('misread recovery: OK')

    # A board that flipped after calibration (chess.com does this for Black).
    flipped = cv_.flip_mapping(names)
    flipped_screen = [labels_of(start).get(flipped[i]) for i in range(64)]
    rebuilt, chosen = cv_.resync(flipped_screen, names, start, chess.BLACK)
    assert rebuilt is not None and chosen == flipped, 'flip detection'
    assert rebuilt.board_fen() == start.board_fen() and rebuilt.turn == chess.WHITE
    normal_screen = grid_of(start, names)
    rebuilt, chosen = cv_.resync(normal_screen, names, start, chess.BLACK)
    assert chosen == names and rebuilt is not None and rebuilt.board_fen() == start.board_fen()
    mid = chess.Board('rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1')
    mid_flipped = [labels_of(mid).get(flipped[i]) for i in range(64)]
    rebuilt, chosen = cv_.resync(mid_flipped, names, mid, chess.WHITE)
    assert chosen == flipped and rebuilt.fen() == mid.fen(), 'flip keeps turn and rights'
    # A suggestion that contradicts the screen (own piece on the target,
    # missing source piece) is refused.
    start_grid = grid_of(start, names)
    assert not cv_.suggestion_conflicts(chess.Move.from_uci('e2e4'), start_grid,
                                        names, chess.WHITE, start)
    bad = list(start_grid)
    bad[names.index('e4')] = 'wq'
    assert cv_.suggestion_conflicts(chess.Move.from_uci('e2e4'), bad, names,
                                    chess.WHITE, start)
    bad = list(start_grid)
    bad[names.index('e2')] = None
    assert cv_.suggestion_conflicts(chess.Move.from_uci('e2e4'), bad, names,
                                    chess.WHITE, start)
    bad = list(start_grid)               # screen says a black queen on e4 but the
    bad[names.index('e4')] = 'bq'        # tracked board has an empty square there
    assert cv_.suggestion_conflicts(chess.Move.from_uci('e2e4'), bad, names,
                                    chess.WHITE, start)
    print('flip detection and suggestion sanity: OK')

    # Templates built straight from the assets: no screenshot, exact colours.
    for board, pset in (('brown', 'classic'), ('4fs27', '3d_chesskid'),
                        ('9rdwe', '3d_wood'), ('metal', '8_bit'), ('icy_sea', 'neo_wood')):
        if not (A / 'boards' / f'{board}.png').is_file() or not (A / 'pieces' / pset).is_dir():
            continue
        rec = cv_.build_from_assets(A, board, pset, True)
        for fen in (chess.STARTING_FEN,
                    'rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2',
                    '1Q3Q2/8/4k3/8/8/8/8/4K3 w - - 0 1'):
            grid = rec.classify(render(fen, A / 'boards' / f'{board}.png', A, pset,
                                       coords=True))
            got = {n: p for n, p in zip(rec.names, grid) if p}
            want = labels_of(chess.Board(fen))
            assert got == want, (f'assets {pset}/{board}', fen,
                                 sorted(set(got.items()) ^ set(want.items()))[:6])
        # flipped view and a last-move tint
        flip_rec = cv_.build_from_assets(A, board, pset, False)
        flip_img = render(chess.STARTING_FEN, A / 'boards' / f'{board}.png', A, pset,
                          flip=True, coords=True)
        got = {n: p for n, p in zip(flip_rec.names, flip_rec.classify(flip_img)) if p}
        assert got == labels_of(chess.Board()), (board, pset, 'asset flipped')
        tinted_fen = 'rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2'
        tinted = render(tinted_fen, A / 'boards' / f'{board}.png', A, pset, coords=True,
                        last_move=(chess.E7, chess.E5))
        got = {n: p for n, p in zip(rec.names, rec.classify(tinted)) if p}
        assert got == labels_of(chess.Board(tinted_fen)), (board, pset, 'asset tint')
        print(f'asset templates OK: {pset}/{board}')

    # Wrong --pieces is auto-corrected from the live board.
    sets = sorted(p.name for p in (A / 'pieces').iterdir() if p.is_dir())
    probe_fen = 'rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2'
    for rendered_set in ('ejgfv', 'neo_wood'):
        probe = render(probe_fen, A / 'boards' / 'blue.png', A, rendered_set, coords=True)
        ranking = cv_.detect_piece_set(probe, 'blue', A, True, sets)
        assert ranking and ranking[0][1] == rendered_set, (rendered_set, ranking[:5])
        print(f'piece-set auto-detect OK: {rendered_set} ->',
              [(n, round(s, 2)) for s, n in ranking[:3]])
    probe = render(probe_fen, A / 'boards' / 'blue.png', A, 'ejgfv', coords=True)
    boards = cv_.detect_board(probe, cv_.square_names(True), A)
    assert boards and boards[0][1] == 'blue', boards[:5]
    print('board auto-detect OK:', [(n, round(d, 1)) for d, n in boards[:3]])
    assert cv_.detect_theme(probe, A, True) == ('ejgfv', 'blue')
    noise = np.random.default_rng(0).integers(0, 255, (600, 600, 3), dtype=np.uint8)
    assert cv_.detect_theme(noise, A, True) is None  # never guess from a non-board
    print('theme auto-detect OK: ejgfv/blue; non-board refused')

    total = wrong = failures = 0
    confusions = {}
    try:
        for board, pset in combos:
            bpath = A / 'boards' / f'{board}.png'
            start_img = render(chess.STARTING_FEN, bpath, A, pset)
            rec = cv_.Recognizer.from_start_screenshot(start_img)

            got = {n: p for n, p in zip(rec.names, rec.classify(start_img)) if p}
            assert got == labels_of(chess.Board()), (board, pset, 'start mismatch')
            flipped = render(chess.STARTING_FEN, bpath, A, pset, flip=True)
            rec_flip = cv_.Recognizer.from_start_screenshot(flipped)
            assert rec_flip.names[0] == 'h1' and rec.names[0] == 'a8', 'orientation'

            brd = chess.Board()
            game, lasts = [brd.copy()], [None]
            for _ in range(args.positions):
                moves = list(brd.legal_moves)
                if not moves:
                    break
                mv = random.choice(moves)
                brd.push(mv)
                game.append(brd.copy())
                lasts.append((mv.from_square, mv.to_square))
            imgs = [render(b.fen(), bpath, A, pset, last_move=last)
                    for b, last in zip(game, lasts)]

            tracked = chess.Board()
            wrong_before = wrong
            for n, (truth, img) in enumerate(zip(game, imgs)):
                grid = rec.classify(img)
                truth_lab = labels_of(truth)
                for name, label in zip(rec.names, grid):
                    total += 1
                    want = truth_lab.get(name)
                    if label != want:
                        wrong += 1
                        key = (want or 'empty', label or 'empty')
                        confusions[key] = confusions.get(key, 0) + 1
                        if failures < 10:
                            print(f'  miss {board}/{pset} ply{n} {name}: want {want} got {label}')
                nxt, _ = cv_.advance(tracked, grid, rec.names)
                if nxt is None:
                    if failures < 12:
                        print(f'  TRACKER FAIL {board}/{pset} ply{n}')
                    failures += 1
                else:
                    tracked = nxt
                if tracked.fen().split()[:4] != truth.fen().split()[:4]:
                    failures += 1

            for fen in extras:
                grid = rec.classify(render(fen, bpath, A, pset))
                got = {n: p for n, p in zip(rec.names, grid) if p}
                want = labels_of(chess.Board(fen))
                if got != want:
                    bad = sorted(set(got.items()) ^ set(want.items()))
                    print(f'  EXTRA FAIL {board}/{pset} {fen}: {bad[:6]}')
                    failures += 1
            print(f'OK {board}/{pset}: {wrong - wrong_before} wrong cells', flush=True)
    finally:
        engine.close()

    acc = 1 - wrong / max(1, total)
    print(f'cell accuracy {acc:.4%} over {total} cells; tracker/edge failures: {failures}')
    if confusions:
        print('confusions:', sorted(confusions.items(), key=lambda kv: -kv[1])[:10])

    mate = chess.Board('6k1/5ppp/8/8/8/8/5PPP/1R4K1 w - - 0 1')
    engine = chess.engine.SimpleEngine.popen_uci(engine_path)
    try:
        move, text = cv_.analyse(engine, mate, 1.0, chess.WHITE)
        _, black_view = cv_.analyse(engine, mate, 0.5, chess.BLACK)
    finally:
        engine.close()
    print('engine:', move, '|', text, '|', black_view)
    assert move == chess.Move.from_uci('b1b8') and 'mate in 1' in text
    assert 'getting mated in 1' in black_view

    if acc < 0.995 or failures:
        sys.exit('SELFTEST FAILED')
    print('SELFTEST PASSED')


if __name__ == '__main__':
    main()
