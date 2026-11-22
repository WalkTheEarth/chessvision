"""Tkinter app for ChessVision."""
import json
import queue
import threading
import time
from pathlib import Path

import chess
import chess.engine
import cv2
import numpy as np
import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageTk

import chessvision as core

BG = '#111318'
PANEL = '#181b22'
CARD = '#20242d'
CARD2 = '#292e3a'
BORDER = '#2c323d'
TEXT = '#e9ecf2'
MUTED = '#8b93a3'
ACCENT = '#4ade80'
ACCENT_DARK = '#0d2a1a'
ACCENT_HOVER = '#3ecf72'
AMBER = '#fbbf24'
RED = '#f87171'
BLUE = '#60a5fa'
GOLD = '#f5c84b'
FONT = 'DejaVu Sans'
BOARD = 560
WIN_W, WIN_H = 1080, 940


def side_name(color):
    return 'White' if color == chess.WHITE else 'Black'


def theme_preview(assets_dir, board_name, pieces_name, size=240):
    """Render the starting position from the asset PNGs for the theme dialog."""
    board_path = Path(assets_dir) / 'boards' / f'{board_name}.png'
    pieces_dir = Path(assets_dir) / 'pieces' / pieces_name
    board = cv2.imread(str(board_path), cv2.IMREAD_COLOR)
    if board is None or not pieces_dir.is_dir():
        return None
    board = cv2.resize(board, (size, size), interpolation=cv2.INTER_AREA)
    cell = size // 8
    for square, piece in chess.Board().piece_map().items():
        code = ('w' if piece.color else 'b') + piece.symbol().lower()
        sprite = cv2.imread(str(pieces_dir / f'{code}.png'), cv2.IMREAD_UNCHANGED)
        if sprite is None:
            continue
        sprite = cv2.resize(sprite, (cell, cell), interpolation=cv2.INTER_AREA)
        rgb = sprite[:, :, :3].astype(np.float32)
        alpha = (sprite[:, :, 3:4].astype(np.float32) / 255
                 if sprite.shape[2] == 4 else np.ones((cell, cell, 1), np.float32))
        r = 7 - chess.square_rank(square)
        c = chess.square_file(square)
        y0, x0 = r * cell, c * cell
        roi = board[y0:y0 + cell, x0:x0 + cell].astype(np.float32)
        board[y0:y0 + cell, x0:x0 + cell] = (alpha * rgb + (1 - alpha) * roi).astype(np.uint8)
    return board


def launch(args):
    cfg = json.loads(core.CONFIG_PATH.read_text()) if core.CONFIG_PATH.exists() else {}
    side_key = args.side or cfg.get('side', 'white')
    side = chess.WHITE if side_key == 'white' else chess.BLACK
    skill = args.skill if args.skill is not None else int(cfg.get('skill', core.SKILL_DEFAULT))
    skill = max(0, min(20, skill))
    assets_dir = Path(args.assets).expanduser() if args.assets else core.HERE / 'assets'
    pieces = None if args.screenshot else (args.pieces or cfg.get('pieces'))
    board_name = None if args.screenshot else (args.board or cfg.get('board'))
    if bool(pieces) != bool(board_name):
        raise SystemExit('Pass both --pieces and --board (see assets/pieces and assets/boards).')
    use_assets = bool(pieces and board_name)
    if use_assets:
        if not (assets_dir / 'boards' / f'{board_name}.png').is_file():
            known = sorted(p.stem for p in (assets_dir / 'boards').glob('*.png'))
            raise SystemExit(f'Unknown board {board_name!r}. Available: {", ".join(known)}')
        if not (assets_dir / 'pieces' / pieces).is_dir():
            known = sorted(p.name for p in (assets_dir / 'pieces').iterdir() if p.is_dir())
            raise SystemExit(f'Unknown piece set {pieces!r}. Available: {", ".join(known)}')
    bottom = args.bottom or cfg.get('bottom')
    if bottom not in ('white', 'black'):
        if core.TEMPLATES_PATH.exists():
            try:
                bottom = 'white' if core.Recognizer.load().names[0] == 'a8' else 'black'
            except Exception:
                bottom = 'white' if side == chess.WHITE else 'black'
        else:
            bottom = 'white' if side == chess.WHITE else 'black'

    engine_path = core.find_engine(args.engine)  # fail before the share prompt
    cap = core.ScreenCast()
    probe = cap.grab(timeout=10)
    probe_size = [int(probe.shape[1]), int(probe.shape[0])] if probe is not None else None
    root = tk.Tk()
    app = App(root, cap, args, cfg, side, skill, pieces, board_name, bottom,
              assets_dir, use_assets, engine_path)

    has_config = core.CONFIG_PATH.exists() and 'roi' in cfg
    has_templates = core.TEMPLATES_PATH.exists()
    need_roi = args.recalibrate or not (has_config and has_templates)
    if probe_size is not None and list(cfg.get('frame_size') or []) != probe_size:
        need_roi = True  # different source size: the old ROI does not fit
    if not need_roi:
        theme_matches = (cfg.get('pieces') == pieces and cfg.get('board') == board_name)
        if use_assets and not theme_matches:
            rec = core.build_from_assets(assets_dir, board_name, pieces, bottom == 'white')
            rec.save()
            cfg.update({'pieces': pieces, 'board': board_name, 'bottom': bottom})
            core.CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
            print(f'Using {pieces} pieces on the {board_name} board.')
        app.roi = cfg['roi']
        app.rec = core.Recognizer.load()
        app.frame_size = probe_size
        want = core.square_names(bottom == 'white')
        if app.rec.names != want:
            app.rec.names = want
        cfg.update({'side': side_key, 'skill': skill, 'bottom': bottom})
        if use_assets:
            cfg.update({'pieces': pieces, 'board': board_name})
        else:
            cfg.pop('pieces', None); cfg.pop('board', None)
        if probe_size is not None:
            cfg['frame_size'] = probe_size
        core.CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
        app.start_play()
    else:
        if has_config and has_templates:
            try:
                app.rec = core.Recognizer.load()
                app.roi = cfg['roi']
                app.frame_size = list(cfg.get('frame_size') or [])
            except Exception:
                pass
        app.start_calibration(startup=True)
    try:
        root.mainloop()
    finally:
        if not app.closed:
            app.close()


class App:
    def __init__(self, root, cap, args, cfg, side, skill, pieces, board_name, bottom,
                 assets_dir, use_assets, engine_path):
        self.root = root
        self.cap = cap
        self.args = args
        self.cfg = cfg
        self.user_color = side
        self.skill = skill
        self.pieces = pieces
        self.board_name = board_name
        self.bottom = bottom
        self.assets_dir = assets_dir
        self.use_assets = use_assets
        self.engine_path = engine_path

        self.roi = None
        self.rec = None
        self.engine = None
        self.worker = None
        self.requests = queue.Queue()
        self.results = queue.Queue()
        self.closing = False
        self.closed = False
        self.mode = 'idle'
        self.startup_calib = False

        self.board = chess.Board()
        self.pending = self.committed = None
        self.pending_since = 0.0
        self.tracking_ok = True
        self.unknown_since = 0.0
        self.missed = 0
        self.move = None
        self.flair = ''
        self.analysis_id = 0
        self.analysis_ctx = None
        self.fish = True

        self.cal_frame = None
        self.cal_box = None
        self.cal_sel = None
        self.cal_scale = 1.0
        self.cal_off = (0, 0)
        self.cal_photo = None
        self.board_photo = None
        self.last_roi_img = None
        self.frame_size = None
        self.pin_var = tk.BooleanVar(value=True)

        self._build_ui()
        self._bind()
        self._set_title()
        self._update_side_buttons()
        self._update_theme_label()
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(100, self.tick)

    # ---------------------------------------------------------------- UI

    def _build_ui(self):
        root = self.root
        root.configure(bg=BG)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        x, y = max(0, (sw - WIN_W) // 2), max(0, (sh - WIN_H) // 2)
        root.geometry(f'{WIN_W}x{WIN_H}+{x}+{y}')
        root.resizable(False, False)
        root.attributes('-topmost', True)

        outer = tk.Frame(root, bg=BG)
        outer.pack(fill='both', expand=True, padx=22, pady=18)
        left = tk.Frame(outer, bg=BG)
        left.grid(row=0, column=0, sticky='n')
        right = tk.Frame(outer, bg=BG, width=430, height=890)
        right.grid(row=0, column=1, sticky='n', padx=(26, 0))
        right.grid_propagate(False)

        wrap = tk.Frame(left, bg=BORDER, padx=1, pady=1)
        wrap.pack()
        self.canvas = tk.Canvas(wrap, width=BOARD, height=BOARD, bg='#0b0d11',
                                highlightthickness=0, cursor='arrow')
        self.canvas.pack()
        self.footer = tk.Label(left, text='', bg=BG, fg=MUTED, font=(FONT, 9))
        self.footer.pack(anchor='w', pady=(10, 0))

        head = tk.Frame(right, bg=BG)
        head.pack(fill='x')
        tk.Label(head, text='CHESSVISION', bg=BG, fg=TEXT,
                 font=(FONT, 16, 'bold')).pack(side='left')
        self.fish_label = tk.Label(head, text=' ><>', bg=BG, fg=ACCENT,
                                   font=(FONT, 12, 'bold'))
        self.fish_label.pack(side='left', pady=(4, 0))
        tk.Label(right, text='screen chess coach  ·  Stockfish', bg=BG, fg=MUTED,
                 font=(FONT, 9), wraplength=400, justify='left').pack(
            anchor='w', pady=(0, 14))

        # side + strength
        card = tk.Frame(right, bg=CARD, padx=14, pady=12)
        card.pack(fill='x')
        tk.Label(card, text='YOU PLAY', bg=CARD, fg=MUTED,
                 font=(FONT, 8, 'bold')).pack(anchor='w')
        row = tk.Frame(card, bg=CARD)
        row.pack(fill='x', pady=(6, 12))
        self.white_btn = self._button(row, 'White', lambda: self.set_side(chess.WHITE), small=True)
        self.white_btn.pack(side='left', expand=True, fill='x', padx=(0, 4))
        self.black_btn = self._button(row, 'Black', lambda: self.set_side(chess.BLACK), small=True)
        self.black_btn.pack(side='left', expand=True, fill='x', padx=(4, 0))
        for button in (self.white_btn, self.black_btn):
            button.bind('<Enter>', lambda _e: self._update_side_buttons())
            button.bind('<Leave>', lambda _e: self._update_side_buttons())
        tk.Label(card, text='STOCKFISH', bg=CARD, fg=MUTED,
                 font=(FONT, 8, 'bold')).pack(anchor='w')
        srow = tk.Frame(card, bg=CARD)
        srow.pack(fill='x', pady=(4, 0))
        self._button(srow, '−', lambda: self.adjust_skill(-1), small=True).pack(side='left')
        self.skill_value = tk.Label(srow, text=str(self.skill), bg=CARD, fg=TEXT,
                                    font=(FONT, 15, 'bold'), width=3)
        self.skill_value.pack(side='left', padx=6)
        self._button(srow, '+', lambda: self.adjust_skill(1), small=True).pack(side='left')
        self.skill_hint = tk.Label(srow, text='0–20', bg=CARD, fg=MUTED,
                                   font=(FONT, 9))
        self.skill_hint.pack(side='right')

        self.mode_holder = tk.Frame(right, bg=BG)
        self.mode_holder.pack(fill='x', pady=(12, 0))

        # play status card
        self.status_card = tk.Frame(self.mode_holder, bg=CARD, padx=16, pady=14)
        tk.Label(self.status_card, text='STATUS', bg=CARD, fg=MUTED,
                 font=(FONT, 8, 'bold')).pack(anchor='w')
        self.headline = tk.Label(self.status_card, text='STARTING…', bg=CARD, fg=ACCENT,
                                 font=(FONT, 11, 'bold'))
        self.headline.pack(anchor='w', pady=(6, 0))
        self.san_label = tk.Label(self.status_card, text='—', bg=CARD, fg=TEXT,
                                  font=(FONT, 28, 'bold'))
        self.san_label.pack(anchor='w')
        self.verdict_label = tk.Label(self.status_card, text='', bg=CARD, fg=MUTED,
                                      font=(FONT, 10), wraplength=380, justify='left')
        self.verdict_label.pack(anchor='w')

        # calibration card
        self.cal_card = tk.Frame(self.mode_holder, bg=CARD, padx=16, pady=14)
        tk.Label(self.cal_card, text='CALIBRATE', bg=CARD, fg=AMBER,
                 font=(FONT, 8, 'bold')).pack(anchor='w')
        self.cal_text = tk.Label(self.cal_card, text='', bg=CARD, fg=TEXT,
                                 font=(FONT, 10), wraplength=380, justify='left')
        self.cal_text.pack(anchor='w', pady=(6, 6))
        self.cal_status = tk.Label(self.cal_card, text='', bg=CARD, fg=MUTED,
                                   font=(FONT, 9), wraplength=380, justify='left')
        self.cal_status.pack(anchor='w', pady=(0, 10))
        crow = tk.Frame(self.cal_card, bg=CARD)
        crow.pack(fill='x')
        self.use_btn = self._button(crow, 'Use board', self.finish_calibration, accent=True)
        self.use_btn.pack(side='left')
        self.use_btn.config(state='disabled')
        self._button(crow, 'Refresh', self.refresh_calibration, small=True).pack(side='left', padx=6)
        self._button(crow, 'Cancel', self.cancel_calibration, small=True).pack(side='left')

        self.controls_card = tk.Frame(right, bg=BG)
        self.controls_card.pack(fill='x', pady=(12, 0))
        grid = tk.Frame(self.controls_card, bg=BG)
        grid.pack(fill='x')
        for i, (label, cmd) in enumerate((
                ('Flip board', self.flip), ('Resync', self.resync),
                ('Recalibrate', self.recalibrate), ('Quit', self.close))):
            b = self._button(grid, label, cmd, danger=(label == 'Quit'))
            b.grid(row=i // 2, column=i % 2, sticky='ew', padx=(0 if i % 2 == 0 else 4, 0),
                   pady=(0 if i < 2 else 6, 0))
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        self.theme_row = tk.Frame(right, bg=BG)
        self.theme_row.pack(fill='x', pady=(12, 6))
        self.theme_label = tk.Label(self.theme_row, text='', bg=BG, fg=MUTED,
                                    font=(FONT, 9), wraplength=330, justify='left')
        self.theme_label.pack(side='left', fill='x', expand=True)
        self._button(self.theme_row, 'Change', self.open_theme_dialog,
                     small=True).pack(side='right')
        pin = tk.Checkbutton(right, text='Keep window on top', variable=self.pin_var,
                             command=self.toggle_pin, bg=BG, fg=MUTED, selectcolor=CARD,
                             activebackground=BG, activeforeground=TEXT, bd=0,
                             highlightthickness=0, font=(FONT, 9))
        pin.pack(anchor='w')
        self.hint = tk.Label(
            right,
            text='Tip: share the browser window, not the screen, so this window can '
                 'overlap it safely.',
            bg=BG, fg='#667080', font=(FONT, 8), wraplength=400, justify='left')
        self.hint.pack(anchor='w', pady=(10, 0))

    def _button(self, parent, text, command, accent=False, small=False, danger=False):
        bg = ACCENT if accent else ('#3a2026' if danger else CARD2)
        fg = ACCENT_DARK if accent else (RED if danger else TEXT)
        hover = ACCENT_HOVER if accent else ('#47262d' if danger else '#333a48')
        b = tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                      activebackground=hover, activeforeground=fg, bd=0,
                      relief='flat', highlightthickness=0, cursor='hand2',
                      disabledforeground='#7a838f',
                      font=(FONT, 9 if small else 10, 'bold'),
                      padx=10, pady=6 if small else 9)
        b.bind('<Enter>', lambda _e: b.config(bg=hover))
        b.bind('<Leave>', lambda _e: b.config(bg=bg) if str(b['state']) != 'disabled' else None)
        return b

    def _bind(self):
        self.root.bind_all('<Key>', self._on_key)
        self.canvas.bind('<ButtonPress-1>', self._cal_press)
        self.canvas.bind('<B1-Motion>', self._cal_drag)
        self.canvas.bind('<ButtonRelease-1>', self._cal_release)

    def _on_key(self, event):
        ch = event.char.lower()
        if ch == 'q':
            self.close()
        elif ch == 's':
            self.set_side(not self.user_color)
        elif ch == 'f':
            self.flip()
        elif ch == 'r':
            self.resync()
        elif ch == '[':
            self.adjust_skill(-1)
        elif ch == ']':
            self.adjust_skill(1)
        elif ch == 'd':
            self.save_snapshot()
        elif ch == 'e':
            self.fish = not self.fish
            self.fish_label.config(text=' ><>' if self.fish else '')

    def _set_title(self):
        self.root.title(f'ChessVision — {side_name(self.user_color)}')

    def _update_side_buttons(self):
        for color, button in ((chess.WHITE, self.white_btn), (chess.BLACK, self.black_btn)):
            active = color == self.user_color
            button.config(bg=ACCENT if active else CARD2,
                          fg=ACCENT_DARK if active else TEXT,
                          activebackground=ACCENT_HOVER if active else CARD2)

    def _update_theme_label(self):
        src = f'{self.pieces} · {self.board_name}' if self.use_assets else 'screenshot templates'
        self.theme_label.config(
            text=f'{src}   ·   {self.bottom.capitalize()} at the bottom   ·   '
                 f'skill {self.skill}')

    def _save(self, updates=None):
        if updates:
            self.cfg.update(updates)
        if self.roi is not None:
            self.cfg['roi'] = self.roi
        self.cfg.update({'side': side_name(self.user_color).lower(), 'skill': self.skill,
                         'bottom': self.bottom})
        if self.use_assets:
            self.cfg.update({'pieces': self.pieces, 'board': self.board_name})
        else:
            self.cfg.pop('pieces', None)
            self.cfg.pop('board', None)
        if self.frame_size:
            self.cfg['frame_size'] = self.frame_size
        core.CONFIG_PATH.write_text(json.dumps(self.cfg, indent=2))

    # ------------------------------------------------------------- view

    def set_view(self, kind, headline, san='', verdict=''):
        colors = {'your': ACCENT, 'wait': MUTED, 'think': BLUE, 'read': AMBER,
                  'win': GOLD, 'lose': RED, 'draw': MUTED, 'error': RED, 'info': TEXT}
        self.headline.config(text=headline, fg=colors.get(kind, TEXT))
        self.san_label.config(text=san or '—', fg=TEXT if san else '#59606d')
        self.verdict_label.config(text=verdict or '',
                                  fg=GOLD if 'mate' in verdict.lower() else MUTED)

    def _with_flair(self, text):
        return f'{text}   {self.flair}' if self.flair else text

    # ------------------------------------------------------- calibration

    def start_calibration(self, startup=False):
        self.mode = 'calibrate'
        self.startup_calib = startup
        self.move = None
        self.committed = self.pending = None
        self.canvas.configure(cursor='crosshair')
        self._show_calibration_ui()
        self.cal_text.config(
            text=('Put the board fully visible, then drag a box around the 64 squares. '
                  'The piece set is checked against the screen automatically.'
                  if self.use_assets else
                  'Put chess.com on the STARTING position, then drag a box around the '
                  '64 squares. This mode reads the pieces from the screenshot.'))
        frame = self.cap.grab(timeout=30)
        if frame is None:
            self._cal_status('No screen frames arrived. Check the share prompt.', RED)
            return
        self._set_cal_frame(frame)
        self._cal_status('Drag a box around the 64 squares.')

    def _set_cal_frame(self, frame):
        self.cal_frame = frame
        self.cal_sel = None
        self.cal_box = None
        self.cal_photo = None
        self.use_btn.config(state='disabled')
        h, w = frame.shape[:2]
        self.cal_scale = min(BOARD / w, BOARD / h)
        nw, nh = int(w * self.cal_scale), int(h * self.cal_scale)
        self.cal_off = ((BOARD - nw) // 2, (BOARD - nh) // 2)
        rgb = cv2.cvtColor(cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA),
                           cv2.COLOR_BGR2RGB)
        self.cal_photo = ImageTk.PhotoImage(Image.fromarray(rgb))
        self._draw_calibration()

    def _draw_calibration(self):
        self.canvas.delete('all')
        self.canvas.create_rectangle(0, 0, BOARD, BOARD, fill='#0b0d11', outline='')
        if self.cal_photo is not None:
            self.canvas.create_image(*self.cal_off, anchor='nw', image=self.cal_photo)
        if self.cal_box is not None:
            self.canvas.create_rectangle(*self.cal_box, fill=ACCENT, stipple='gray12',
                                         outline='')
            self.canvas.create_rectangle(*self.cal_box, outline=ACCENT, width=2)

    def _canvas_to_frame(self, x, y):
        if self.cal_frame is None:
            return None
        h, w = self.cal_frame.shape[:2]
        fx = (x - self.cal_off[0]) / self.cal_scale
        fy = (y - self.cal_off[1]) / self.cal_scale
        return max(0, min(w - 1, fx)), max(0, min(h - 1, fy))

    def _cal_press(self, event):
        if self.mode != 'calibrate' or self.cal_frame is None:
            return
        self._drag_start = self._canvas_to_frame(event.x, event.y)

    def _cal_drag(self, event):
        if self.mode != 'calibrate' or self.cal_frame is None or not hasattr(self, '_drag_start'):
            return
        p = self._canvas_to_frame(event.x, event.y)
        if p is None:
            return
        x0, y0 = int(min(self._drag_start[0], p[0])), int(min(self._drag_start[1], p[1]))
        x1, y1 = int(max(self._drag_start[0], p[0])), int(max(self._drag_start[1], p[1]))
        self.cal_box = (self.cal_off[0] + x0 * self.cal_scale,
                        self.cal_off[1] + y0 * self.cal_scale,
                        self.cal_off[0] + x1 * self.cal_scale,
                        self.cal_off[1] + y1 * self.cal_scale)
        self._draw_calibration()
        if x1 - x0 > 20 and y1 - y0 > 20:
            self.cal_sel = (x0, y0, x1, y1)
            self.use_btn.config(state='normal')
            self._cal_status(f'Selected {x1 - x0} × {y1 - y0} px.')

    def _cal_release(self, event):
        if self.mode != 'calibrate':
            return
        self._cal_drag(event)

    def refresh_calibration(self):
        if self.mode != 'calibrate':
            return
        frame = self.cap.grab(timeout=10)
        if frame is None:
            self._cal_status('No frames arrived; try sharing again.', RED)
            return
        self._set_cal_frame(frame)
        self._cal_status('Frame refreshed. Drag a box around the 64 squares.')

    def cancel_calibration(self):
        if self.mode != 'calibrate':
            return
        if self.rec is None:
            self.close()
        else:
            self.start_play()

    def _cal_status(self, text, color=MUTED):
        self.cal_status.config(text=text, fg=color)

    def _show_calibration_ui(self):
        self.status_card.pack_forget()
        self.controls_card.pack_forget()
        self.cal_card.pack(fill='x')

    def _show_play_ui(self):
        self.cal_card.pack_forget()
        self.status_card.pack_forget()
        self.status_card.pack(fill='x')
        self.controls_card.pack_forget()
        self.controls_card.pack(fill='x', pady=(12, 0), before=self.theme_row)
        self.canvas.configure(cursor='arrow')
        self.mode = 'play'

    def finish_calibration(self):
        if self.mode != 'calibrate' or self.cal_sel is None or self.cal_frame is None:
            return
        x0, y0, x1, y1 = self.cal_sel
        h, w = self.cal_frame.shape[:2]
        roi = [x0 / w, y0 / h, (x1 - x0) / w, (y1 - y0) / h]
        self.frame_size = [int(w), int(h)]
        roi_img = core.crop_roi(self.cal_frame, roi)
        if self.use_assets:
            self._cal_status('Checking which piece set matches the screen…')
            self.root.update()
            sets = sorted(p.name for p in (self.assets_dir / 'pieces').iterdir() if p.is_dir())
            ranking = core.detect_piece_set(roi_img, self.board_name, self.assets_dir,
                                            self.bottom == 'white', sets)
            if ranking:
                best_score, best_set = ranking[0]
                current = next((s for s, p in ranking if p == self.pieces), 0.0)
                if best_set != self.pieces and best_score > current + 0.02:
                    self.pieces = best_set
                    self._cal_status(f'Using {best_set} pieces (match {best_score:.2f}).')
                rec = core.build_from_assets(self.assets_dir, self.board_name, self.pieces,
                                             self.bottom == 'white')
            else:
                rec = core.build_from_assets(self.assets_dir, self.board_name, self.pieces,
                                             self.bottom == 'white')
            rec.save()
            self.rec = rec
            self.roi = roi
            self._save()
            self._update_theme_label()
            kings = sum(1 for p in rec.classify(roi_img) if p in ('wk', 'bk'))
            self._cal_status('Board saved.' if kings == 2
                             else 'Saved, but both kings were not visible — check the box.',
                             MUTED if kings == 2 else AMBER)
        else:
            self._cal_status('Building templates from the screenshot…')
            self.root.update()
            rec = core.Recognizer.from_start_screenshot(roi_img)
            start = chess.Board()
            truth = {chess.square_name(sq): ('w' if p.color else 'b') + p.symbol().lower()
                     for sq, p in start.piece_map().items()}
            got = {n: p for n, p in zip(rec.names,
                                        rec.classify(roi_img, self.args.threshold)) if p}
            if got != truth:
                self._cal_status('That does not match the starting position. Put the board '
                                 'at the start and press Refresh, or use --pieces/--board.',
                                 RED)
                return
            rec.save()
            self.rec = rec
            self.roi = roi
            self.bottom = 'white' if rec.names[0] == 'a8' else 'black'
            self._save()
            self._update_theme_label()
            self._cal_status('Board saved.')
        self._show_play_ui()
        self.start_play()

    # -------------------------------------------------------------- play

    def start_play(self):
        if self.rec is None or self.roi is None:
            return
        if self.engine is None:
            try:
                self.engine = chess.engine.SimpleEngine.popen_uci(self.engine_path)
                self.worker = threading.Thread(target=self._engine_loop, daemon=True)
                self.worker.start()
            except Exception as exc:
                self.engine = None
                self.set_view('error', 'ENGINE FAILED', '', str(exc))
        self._show_play_ui()
        self._update_theme_label()
        self._set_title()
        self.analysis_id += 1
        self.flair = ''
        frame = self.cap.grab(timeout=5)
        if frame is not None:
            self.frame_size = [int(frame.shape[1]), int(frame.shape[0])]
            roi_img = core.crop_roi(frame, self.roi)
            self.last_roi_img = roi_img
            grid = self.rec.classify(roi_img, self.args.threshold)
            self.pending = self.committed = grid
            after, _made = core.advance(chess.Board(), grid, self.rec.names)
            if after is not None:
                self.board = after
            else:
                rebuilt, chosen = core.resync(grid, self.rec.names, chess.Board(),
                                              self.user_color)
                self.rec.names = chosen
                self.board = rebuilt if rebuilt is not None else chess.Board()
            self.tracking_ok, self.missed = True, 0
            self._refresh_view_for_turn()
            self._draw_play(roi_img)
        else:
            self.set_view('read', 'WAITING FOR SCREEN', '', '')

    def _refresh_view_for_turn(self):
        self.analysis_id += 1
        self.move = None
        if self.board.is_checkmate():
            won = self.board.turn != self.user_color
            self.set_view('win' if won else 'lose',
                          'CHECKMATE — YOU WIN' if won else 'CHECKMATE — YOU LOSE',
                          '', self.flair)
        elif self.board.is_game_over():
            self.set_view('draw', 'DRAW', '', self.flair)
        elif self.board.turn != self.user_color:
            self.set_view('wait', f'WAITING FOR {side_name(self.board.turn).upper()}…',
                          '', self._with_flair('Opponent to move.'))
        else:
            self._request_analysis(self.committed)

    def _request_analysis(self, grid):
        if self.engine is None:
            return
        self.analysis_id += 1
        self.analysis_ctx = (self.analysis_id, self.board.fen(), grid)
        self.requests.put((self.analysis_id, self.board.fen(), self.user_color,
                           self.args.think, self.skill))
        self.move = None
        self.set_view('think', 'THINKING…', '', 'Stockfish is looking…')

    def _engine_loop(self):
        while True:
            item = self.requests.get()
            if item is None or self.closing:
                break
            rid, fen, color, think, skill = item
            try:
                if 'Skill Level' in self.engine.options:
                    self.engine.configure({'Skill Level': skill})
                move, san, verdict = core.evaluate(self.engine, chess.Board(fen), think, color)
            except Exception as exc:
                move, san, verdict = None, '', f'engine error: {exc}'
            self.results.put((rid, fen, move, san, verdict))

    def _handle_result(self, rid, fen, move, san, verdict):
        if rid != self.analysis_id or fen != self.board.fen():
            return
        if move is not None:
            grid = self.analysis_ctx[2]
            if core.suggestion_conflicts(move, grid, self.rec.names,
                                         self.user_color, self.board):
                self.move = None
                self.set_view('error', 'BOARD READ LOOKS WRONG', '',
                              'Press F to flip the board or R to resync.')
                return
        self.move = move
        if move is None:
            self.set_view('error', 'ENGINE', '', verdict)
        else:
            self.set_view('your', 'YOUR MOVE', san, self._with_flair(verdict))

    def _poll_results(self):
        while True:
            try:
                rid, fen, move, san, verdict = self.results.get_nowait()
            except queue.Empty:
                break
            self._handle_result(rid, fen, move, san, verdict)

    def _commit(self, grid, now):
        before = self.board
        if core.placement_fen(grid, self.rec.names) == chess.Board().board_fen():
            after, made = chess.Board(), None
        else:
            after, made = core.advance(self.board, grid, self.rec.names)
        if after is None:
            self.missed += 1
            if self.tracking_ok:
                self.unknown_since = now
            self.tracking_ok = False
            self.move = None
            self.set_view('read', 'READING BOARD…', '', 'Trying to recover the position.')
            return
        self.board = after
        self.tracking_ok, self.missed = True, 0
        self.flair = core.move_flair(before, made)
        self._refresh_view_for_turn()

    def _auto_resync(self, now):
        if (self.tracking_ok or self.committed is None or self.pending != self.committed
                or now - self.unknown_since < core.AUTO_RESYNC_SEC):
            return
        guess = self.board.turn if self.missed % 2 == 0 else not self.board.turn
        rebuilt, chosen = core.resync(self.committed, self.rec.names, self.board, guess)
        self.unknown_since = now
        if rebuilt is None:
            return
        self.rec.names = chosen
        self.board = rebuilt
        self.tracking_ok, self.missed = True, 0
        self.bottom = 'white' if chosen[0] == 'a8' else 'black'
        self._save()
        self._update_theme_label()
        self._refresh_view_for_turn()

    def tick(self):
        if self.closing:
            return
        if self.mode == 'play' and self.rec is not None and self.roi is not None:
            self._poll_results()
            frame = self.cap.grab()
            if frame is not None:
                if (self.frame_size is not None
                        and [int(frame.shape[1]), int(frame.shape[0])] != self.frame_size):
                    self.set_view('error', 'SCREEN CHANGED', '',
                                  'The capture size changed. Press Recalibrate (or f if the '
                                  'board is just flipped).')
                    self.root.after(80, self.tick)
                    return
                roi_img = core.crop_roi(frame, self.roi)
                self.last_roi_img = roi_img
                try:
                    grid = self.rec.classify(roi_img, self.args.threshold)
                except Exception:
                    grid = None
                    self.set_view('error', 'RECOGNITION ERROR', '',
                                  'The templates do not fit the screen. Recalibrate.')
                if grid is not None:
                    now = time.time()
                    if grid != self.pending:
                        self.pending, self.pending_since = grid, now
                    elif grid != self.committed and now - self.pending_since >= core.STABLE_SEC:
                        self.committed = grid
                        self._commit(grid, now)
                    self._draw_play(roi_img)
                    self._auto_resync(now)
        self.root.after(80, self.tick)

    # -------------------------------------------------------------- theme

    def apply_theme(self, pieces, board_name):
        try:
            rec = core.build_from_assets(self.assets_dir, board_name, pieces,
                                         self.bottom == 'white')
        except SystemExit as exc:
            return str(exc)
        self.use_assets = True
        self.pieces, self.board_name = pieces, board_name
        self.rec = rec
        rec.save()
        self._save()
        self._update_theme_label()
        self.pending = self.committed = None
        self.analysis_id += 1
        self.move = None
        if self.mode == 'play':
            self.set_view('info', 'THEME CHANGED', '', f'{pieces} · {board_name}')
        return None

    def live_roi_image(self):
        if self.last_roi_img is not None:
            return self.last_roi_img
        if self.roi is None:
            return None
        frame = self.cap.grab(timeout=5)
        return core.crop_roi(frame, self.roi) if frame is not None else None

    def theme_mismatch(self, live_img, pieces, board_name):
        """Describe why a theme does not match the live screen, or None."""
        names = core.square_names(self.bottom == 'white')
        boards = core.detect_board(live_img, names, self.assets_dir)
        piece_list = sorted(p.name for p in (self.assets_dir / 'pieces').iterdir()
                            if p.is_dir())
        ranking = core.detect_piece_set(live_img, board_name, self.assets_dir,
                                        self.bottom == 'white', piece_list)
        best_dist, best_board = boards[0] if boards else (0.0, board_name)
        chosen_dist = next((d for d, b in boards if b == board_name), 10 ** 9)
        best_score, best_pieces = ranking[0] if ranking else (1.0, pieces)
        chosen_score = next((s for s, p in ranking if p == pieces), 0.0)
        board_bad = bool(boards) and board_name != best_board and chosen_dist > best_dist + 12
        pieces_bad = bool(ranking) and pieces != best_pieces and best_score > chosen_score + 0.02
        if board_bad or pieces_bad:
            return (f'Your screen looks like {best_pieces} · {best_board}, not '
                    f'{pieces} · {board_name}.')
        return None

    def save_snapshot(self):
        if self.last_roi_img is None or self.rec is None:
            self.set_view('error', 'NO SNAPSHOT', '', 'No board frame yet.')
            return
        stamp = time.strftime('%Y%m%d_%H%M%S')
        image_path = core.HERE / f'debug_{stamp}.png'
        text_path = core.HERE / f'debug_{stamp}.txt'
        cv2.imwrite(str(image_path), self.last_roi_img)
        grid = self.committed if self.committed is not None else self.pending
        shown = grid if grid is not None else [None] * 64
        text_path.write_text(
            f'side={side_name(self.user_color)} skill={self.skill} '
            f'pieces={self.pieces} board={self.board_name} bottom={self.bottom} '
            f'frame_size={self.frame_size}\nfen={self.board.fen()}\n'
            + '\n'.join(f'{n}: {p}' for n, p in zip(self.rec.names, shown)) + '\n')
        print('Saved', image_path, text_path)
        self.set_view('info', 'SNAPSHOT SAVED', '', image_path.name)

    def open_theme_dialog(self):
        top = tk.Toplevel(self.root)
        top.title('Change theme')
        top.configure(bg=BG)
        top.transient(self.root)
        top.resizable(False, False)
        top.grab_set()
        self.root.update_idletasks()
        rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
        rw, rh = self.root.winfo_width(), self.root.winfo_height()
        w, h = 560, 720
        top.geometry(f'{w}x{h}+{rx + (rw - w) // 2}+{ry + (rh - h) // 2}')

        pieces_list = sorted(p.name for p in (self.assets_dir / 'pieces').iterdir()
                             if p.is_dir())
        boards_list = sorted(p.stem for p in (self.assets_dir / 'boards').glob('*.png'))
        if not pieces_list or not boards_list:
            tk.Label(top, text='No board/piece assets found in ./assets.', bg=BG,
                     fg=RED, font=(FONT, 10)).pack(padx=20, pady=20)
            return
        default_pieces = self.pieces if self.pieces in pieces_list else pieces_list[0]
        default_board = self.board_name if self.board_name in boards_list else boards_list[0]
        live_img = self.live_roi_image()
        state = {'force': False}

        tk.Label(top, text='THEME', bg=BG, fg=AMBER,
                 font=(FONT, 9, 'bold')).pack(anchor='w', padx=20, pady=(16, 2))
        tk.Label(top, text='Pick the pair your chess.com board uses. Apply checks it '
                           'against the live screen.', bg=BG, fg=MUTED,
                 font=(FONT, 9), wraplength=510, justify='left').pack(
            anchor='w', padx=20, pady=(0, 10))

        previews = tk.Frame(top, bg=BG)
        previews.pack(fill='x', padx=20)
        for col, title in enumerate(('YOUR SCREEN', 'TEMPLATE PREVIEW')):
            tk.Label(previews, text=title, bg=BG, fg=MUTED,
                     font=(FONT, 8, 'bold')).grid(row=0, column=col, pady=(0, 4))
        live_canvas = tk.Canvas(previews, width=200, height=200, bg='#0b0d11',
                                highlightthickness=1, highlightbackground=BORDER)
        live_canvas.grid(row=1, column=0, padx=(0, 10))
        theme_canvas = tk.Canvas(previews, width=200, height=200, bg='#0b0d11',
                                 highlightthickness=1, highlightbackground=BORDER)
        theme_canvas.grid(row=1, column=1, padx=(10, 0))
        previews.columnconfigure(0, weight=1)
        previews.columnconfigure(1, weight=1)

        def draw(canvas, img, size=200, empty='no live frame'):
            canvas.delete('all')
            if img is None:
                canvas.create_text(size // 2, size // 2, text=empty, fill=MUTED,
                                   font=(FONT, 9))
                return
            pic = cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)
            photo = ImageTk.PhotoImage(
                Image.fromarray(cv2.cvtColor(pic, cv2.COLOR_BGR2RGB)))
            canvas.create_image(size // 2, size // 2, image=photo)
            canvas.image = photo

        draw(live_canvas, live_img)

        style = ttk.Style(top)
        style.theme_use('clam')
        style.configure('Dark.TCombobox', fieldbackground=CARD2, background=CARD2,
                        foreground=TEXT, arrowcolor=TEXT, bordercolor=BORDER,
                        lightcolor=CARD2, darkcolor=CARD2, padding=6)
        style.map('Dark.TCombobox', fieldbackground=[('readonly', CARD2)],
                  foreground=[('readonly', TEXT)])
        top.option_add('*TCombobox*Listbox.background', CARD2)
        top.option_add('*TCombobox*Listbox.foreground', TEXT)
        top.option_add('*TCombobox*Listbox.selectBackground', ACCENT)
        top.option_add('*TCombobox*Listbox.selectForeground', ACCENT_DARK)

        form = tk.Frame(top, bg=BG)
        form.pack(fill='x', padx=20, pady=(14, 4))
        tk.Label(form, text='Pieces', bg=BG, fg=MUTED, font=(FONT, 9, 'bold')).grid(
            row=0, column=0, sticky='w', pady=4)
        pvar = tk.StringVar(value=default_pieces)
        pcombo = ttk.Combobox(form, textvariable=pvar, values=pieces_list,
                              state='readonly', style='Dark.TCombobox', width=24)
        pcombo.grid(row=0, column=1, sticky='ew', padx=(12, 0), pady=4)
        tk.Label(form, text='Board', bg=BG, fg=MUTED, font=(FONT, 9, 'bold')).grid(
            row=1, column=0, sticky='w', pady=4)
        bvar = tk.StringVar(value=default_board)
        bcombo = ttk.Combobox(form, textvariable=bvar, values=boards_list,
                              state='readonly', style='Dark.TCombobox', width=24)
        bcombo.grid(row=1, column=1, sticky='ew', padx=(12, 0), pady=4)
        form.columnconfigure(1, weight=1)

        message = tk.Label(top, text='', bg=BG, fg=MUTED, font=(FONT, 9),
                           wraplength=510, justify='left')
        message.pack(anchor='w', padx=20, pady=(8, 0))

        def update_preview(*_):
            draw(theme_canvas, theme_preview(self.assets_dir, bvar.get(),
                                             pvar.get(), 200), empty='no preview')

        def changed(*_):
            state['force'] = False
            message.config(text='')
            update_preview()

        def match_screen():
            nonlocal live_img
            live_img = self.live_roi_image()
            if live_img is None:
                message.config(text='No live frame yet. Recalibrate first.', fg=RED)
                return
            draw(live_canvas, live_img)
            message.config(text='Reading your screen…', fg=MUTED)
            top.update()
            names = core.square_names(self.bottom == 'white')
            boards = core.detect_board(live_img, names, self.assets_dir)
            if boards:
                bvar.set(boards[0][1])
            ranking = core.detect_piece_set(live_img, bvar.get(), self.assets_dir,
                                            self.bottom == 'white', pieces_list)
            if ranking:
                pvar.set(ranking[0][1])
            state['force'] = False
            update_preview()
            message.config(text=f'Matched {pvar.get()} · {bvar.get()} to your screen.',
                           fg=ACCENT)

        pcombo.bind('<<ComboboxSelected>>', changed)
        bcombo.bind('<<ComboboxSelected>>', changed)
        update_preview()

        def apply():
            pieces, board_name = pvar.get(), bvar.get()
            if not state['force'] and live_img is not None:
                message.config(text='Checking your screen…', fg=MUTED)
                top.update()
                warn = self.theme_mismatch(live_img, pieces, board_name)
                if warn:
                    message.config(
                        text=warn + '  Use “Match screen”, or click Apply again to force.',
                        fg=AMBER)
                    state['force'] = True
                    return
                message.config(text='')
            error = self.apply_theme(pieces, board_name)
            if error:
                message.config(text=error, fg=RED)
            else:
                top.destroy()

        row = tk.Frame(top, bg=BG)
        row.pack(fill='x', padx=20, pady=16)
        self._button(row, 'Match screen', match_screen, small=True).pack(side='left')
        self._button(row, 'Apply', apply, accent=True).pack(side='right')
        self._button(row, 'Cancel', top.destroy, small=True).pack(side='right', padx=6)

    # ----------------------------------------------------------- controls

    def set_side(self, color):
        if color == self.user_color:
            return
        self.user_color = color
        self.analysis_id += 1
        self._save()
        self._set_title()
        self._update_side_buttons()
        self._update_theme_label()
        self.move = None
        if self.mode == 'play' and not self.board.is_game_over():
            if self.board.turn == color:
                self._request_analysis(self.committed)
            else:
                self.set_view('wait', f'WAITING FOR {side_name(self.board.turn).upper()}…',
                              '', 'Opponent to move.')

    def adjust_skill(self, delta):
        self.analysis_id += 1
        self.skill = max(0, min(20, self.skill + delta))
        self.skill_value.config(text=str(self.skill))
        self._save()
        self._update_theme_label()
        if (self.mode == 'play' and self.board.turn == self.user_color
                and not self.board.is_game_over()):
            self._request_analysis(self.committed)

    def flip(self):
        if self.mode != 'play' or self.rec is None:
            return
        source = self.committed if self.committed is not None else self.pending
        if source is None:
            return
        self.rec.names = core.flip_mapping(self.rec.names)
        if core.placement_fen(source, self.rec.names) == chess.Board().board_fen():
            rebuilt = chess.Board()
        else:
            rebuilt = core.rebuild(source, self.rec.names, self.user_color)
            if rebuilt is None:
                rebuilt = core.rebuild(source, self.rec.names, not self.user_color)
        if rebuilt is None:
            self.rec.names = core.flip_mapping(self.rec.names)
            self.set_view('error', 'CANNOT FLIP', '',
                          'Check the board selection, or open Change and match the theme.')
            return
        self.board = rebuilt
        self.committed = source
        self.tracking_ok, self.missed = True, 0
        self.bottom = 'white' if self.rec.names[0] == 'a8' else 'black'
        self._save()
        self._update_theme_label()
        self._refresh_view_for_turn()

    def resync(self):
        if self.mode != 'play' or self.rec is None:
            return
        source = self.committed if self.committed is not None else self.pending
        if source is None:
            return
        rebuilt, chosen = core.resync(source, self.rec.names, self.board, self.user_color)
        if rebuilt is None:
            self.set_view('error', 'CANNOT RESYNC', '',
                          'Press Recalibrate, or open Change and match the theme to your '
                          'screen.')
            return
        self.rec.names = chosen
        self.board = rebuilt
        self.committed = source
        self.tracking_ok, self.missed = True, 0
        self.bottom = 'white' if chosen[0] == 'a8' else 'black'
        self._save()
        self._update_theme_label()
        self._refresh_view_for_turn()

    def recalibrate(self):
        if self.mode == 'play':
            self.start_calibration(startup=False)

    def toggle_pin(self):
        self.root.attributes('-topmost', self.pin_var.get())

    # ------------------------------------------------------------- paint

    def _draw_play(self, roi_img):
        target = cv2.resize(roi_img, (BOARD, BOARD), interpolation=cv2.INTER_AREA)
        self.board_photo = ImageTk.PhotoImage(
            Image.fromarray(cv2.cvtColor(target, cv2.COLOR_BGR2RGB)))
        self.canvas.delete('all')
        self.canvas.create_image(0, 0, anchor='nw', image=self.board_photo)
        self._draw_arrow()
        if self.board.is_checkmate():
            self.footer.config(text='Checkmate' + (' — you win' if
                                                  self.board.turn != self.user_color
                                                  else ' — you lose'))
        elif self.board.is_game_over():
            self.footer.config(text='Game over')
        else:
            self.footer.config(
                text=f'{side_name(self.board.turn)} to move  ·  '
                     + ('your turn' if self.board.turn == self.user_color
                        else 'waiting'))

    def _draw_arrow(self):
        if self.move is None or self.rec is None:
            return
        cell = BOARD / 8.0

        def rect(square):
            i = self.rec.names.index(chess.square_name(square))
            r, c = divmod(i, 8)
            return c * cell, r * cell, (c + 1) * cell, (r + 1) * cell

        x0, y0, x1, y1 = rect(self.move.from_square)
        self.canvas.create_rectangle(x0 + 2, y0 + 2, x1 - 2, y1 - 2,
                                     fill=ACCENT, stipple='gray12', outline='')
        self.canvas.create_rectangle(x0 + 2, y0 + 2, x1 - 2, y1 - 2,
                                     outline=ACCENT, width=2)
        x0, y0, x1, y1 = rect(self.move.to_square)
        self.canvas.create_rectangle(x0 + 2, y0 + 2, x1 - 2, y1 - 2,
                                     fill=ACCENT, stipple='gray12', outline='')
        self.canvas.create_rectangle(x0 + 2, y0 + 2, x1 - 2, y1 - 2,
                                     outline=ACCENT, width=2)
        fx, fy = rect(self.move.from_square)[0] + cell / 2, rect(self.move.from_square)[1] + cell / 2
        tx, ty = rect(self.move.to_square)[0] + cell / 2, rect(self.move.to_square)[1] + cell / 2
        self.canvas.create_line(fx + 2, fy + 3, tx + 2, ty + 3, fill='#05070a',
                                width=9, arrow='last', arrowshape=(22, 26, 10))
        self.canvas.create_line(fx, fy, tx, ty, fill=ACCENT, width=5,
                                arrow='last', arrowshape=(22, 26, 10))

    # ------------------------------------------------------------- close

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.mode = 'idle'
        try:
            if self.worker is not None:
                self.requests.put(None)
                self.worker.join(timeout=4)
        except Exception:
            pass
        try:
            if self.engine is not None:
                self.engine.close()
        except Exception:
            pass
        try:
            self.cap.close()
        except Exception:
            pass
        self.closed = True
        try:
            self.root.destroy()
        except Exception:
            pass
