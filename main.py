"""
KeyShow - デスクトップキー入力表示アプリ（Windows専用）

デスクトップ上に常時表示する2段構成バー:
  上段: 修飾キーインジケーター（Ctrl / Shift / Alt / Win）
  下段: 一般キー履歴（右寄せ、フェードアウト付き）
バーはドラッグで移動、辺/隅ドラッグでズーム。
トレイメニューから背景色・文字色・透過度を変更可能。
"""

import ctypes
import threading
import time
import tkinter as tk
from collections import OrderedDict
from tkinter import colorchooser
from typing import Callable

from PIL import Image, ImageDraw, ImageFont
from pynput import keyboard
from pystray import Icon, Menu, MenuItem

# ─── Win32 ───────────────────────────────────────────────────
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

_user32 = ctypes.windll.user32

# ─── キー名マッピング ────────────────────────────────────────
SPECIAL_KEY_NAMES: dict[keyboard.Key, str] = {
    keyboard.Key.space: "Space",
    keyboard.Key.enter: "Enter",
    keyboard.Key.tab: "Tab",
    keyboard.Key.backspace: "BS",
    keyboard.Key.delete: "Del",
    keyboard.Key.esc: "Esc",
    keyboard.Key.up: "↑",
    keyboard.Key.down: "↓",
    keyboard.Key.left: "←",
    keyboard.Key.right: "→",
    keyboard.Key.home: "Home",
    keyboard.Key.end: "End",
    keyboard.Key.page_up: "PgUp",
    keyboard.Key.page_down: "PgDn",
    keyboard.Key.insert: "Ins",
    keyboard.Key.caps_lock: "CapsLock",
    keyboard.Key.num_lock: "NumLock",
    keyboard.Key.scroll_lock: "ScrLock",
    keyboard.Key.print_screen: "PrtSc",
    keyboard.Key.pause: "Pause",
    **{getattr(keyboard.Key, f"f{i}"): f"F{i}" for i in range(1, 13)},
    keyboard.Key.media_play_pause: "Play/Pause",
    keyboard.Key.media_next: "Next",
    keyboard.Key.media_previous: "Prev",
    keyboard.Key.media_volume_up: "Vol+",
    keyboard.Key.media_volume_down: "Vol-",
    keyboard.Key.media_volume_mute: "Mute",
}

MODIFIER_KEYS: OrderedDict[keyboard.Key, str] = OrderedDict([
    (keyboard.Key.ctrl, "Ctrl"),   (keyboard.Key.ctrl_l, "Ctrl"),   (keyboard.Key.ctrl_r, "Ctrl"),
    (keyboard.Key.shift, "Shift"), (keyboard.Key.shift_l, "Shift"), (keyboard.Key.shift_r, "Shift"),
    (keyboard.Key.alt, "Alt"),     (keyboard.Key.alt_l, "Alt"),     (keyboard.Key.alt_r, "Alt"),
    (keyboard.Key.alt_gr, "Alt"),
    (keyboard.Key.cmd, "Win"),     (keyboard.Key.cmd_r, "Win"),
])

MODIFIER_ORDER = ["Ctrl", "Shift", "Alt", "Win"]

_NUMPAD_OPS = {106: "*", 107: "+", 109: "-", 110: ".", 111: "/"}

# ─── 設定 ────────────────────────────────────────────────────
MAX_KEY_HISTORY = 12
KEY_CHAIN_TIMEOUT = 2.0  # 秒


# ─── ユーティリティ ──────────────────────────────────────────
def _create_tray_icon_image() -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([4, 4, 60, 60], radius=10, fill=(30, 30, 30, 255))
    try:
        font = ImageFont.truetype("segoeui.ttf", 36)
    except OSError:
        font = ImageFont.load_default()
    draw.text((size / 2, size / 2), "kS", fill=(220, 220, 220, 255), font=font, anchor="mm")
    return img


def _get_key_name(key: keyboard.Key | keyboard.KeyCode) -> str | None:
    """キーの表示名を返す。表示不要なら None。"""
    if isinstance(key, keyboard.Key):
        return SPECIAL_KEY_NAMES.get(key)
    if not isinstance(key, keyboard.KeyCode):
        return None
    # 通常文字
    if key.char is not None and ord(key.char) >= 32:
        return key.char.upper() if key.char.isalpha() else key.char
    # vk コード（Ctrl 同時押し時の制御文字対応）
    if key.vk is not None:
        if 65 <= key.vk <= 90:
            return chr(key.vk)
        if 48 <= key.vk <= 57:
            return chr(key.vk)
        if 96 <= key.vk <= 105:
            return f"Num{key.vk - 96}"
        if key.vk in _NUMPAD_OPS:
            return _NUMPAD_OPS[key.vk]
    return None


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _lerp_color(c1: str, c2: str, t: float) -> str:
    """c1 → c2 を t (0.0〜1.0) で線形補間する。"""
    r1, g1, b1 = _hex_to_rgb(c1)
    r2, g2, b2 = _hex_to_rgb(c2)
    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


# ═════════════════════════════════════════════════════════════
#  OverlayWindow
# ═════════════════════════════════════════════════════════════
class OverlayWindow:
    """2段構成の常時表示バー。辺/隅ドラッグでズーム可能。"""

    # ── 基準サイズ (scale=1.0) ────────────────────────────────
    FONT_FAMILY = "Segoe UI"
    BASE_FONT_MOD = 12
    BASE_FONT_KEY = 18
    BASE_PAD_X = 14
    BASE_PAD_Y = 8
    BASE_ROW_GAP = 4
    BASE_MOD_PAD_X = 8
    BASE_MOD_PAD_Y = 3
    BASE_MOD_RADIUS = 8
    BASE_MOD_GAP = 5
    BASE_EXTRA_H = 6  # 修飾キー行の追加高さ

    # ── 外観（実行中に変更可能） ─────────────────────────────
    bar_bg = "#111111"
    bar_alpha = 0.8
    key_fg = "#ffffff"
    mod_inactive_bg = "#2a2a2a"
    mod_inactive_fg = "#666666"
    mod_active_bg = "#e0e0e0"
    mod_active_fg = "#111111"

    # ── 動作 ─────────────────────────────────────────────────
    BAR_MARGIN_BOTTOM = 80
    KEY_DISPLAY_MS = 2000
    KEY_FADE_STEPS = 8
    KEY_FADE_INTERVAL_MS = 40
    SCALE_MIN = 0.5
    SCALE_MAX = 3.0
    EDGE_ZONE = 10

    # ── リサイズカーソル ─────────────────────────────────────
    _EDGE_CURSORS = {
        "l": "sb_h_double_arrow", "r": "sb_h_double_arrow",
        "t": "sb_v_double_arrow", "b": "sb_v_double_arrow",
        "tl": "size_nw_se", "br": "size_nw_se",
        "tr": "size_ne_sw", "bl": "size_ne_sw",
    }

    def __init__(self) -> None:
        self._scale = 1.0
        self._modifier_active: dict[str, bool] = {m: False for m in MODIFIER_ORDER}
        self._key_history: list[str] = []
        self._last_key_time = 0.0
        self._fade_after_id: str | None = None
        self._hide_after_id: str | None = None
        self._fade_remaining = 0
        self._resizing = False
        self._bar_w = 0
        self._bar_h = 0

        self._setup_root()
        self._build_ui(initial=True)
        self._base_bar_w = self._bar_w
        self._bind_events()
        self._apply_win32_flags()

    # ── ウィンドウ初期化 ─────────────────────────────────────
    def _setup_root(self) -> None:
        self.root = tk.Tk()
        self.root.title("KeyShow")
        self.root.attributes("-topmost", True)
        self.root.overrideredirect(True)
        self.root.configure(bg=self.bar_bg)
        self.root.attributes("-alpha", self.bar_alpha)
        self.canvas = tk.Canvas(self.root, bg=self.bar_bg, highlightthickness=0)

    # ── スケーリング ─────────────────────────────────────────
    def _s(self, base: int) -> int:
        return max(1, int(base * self._scale))

    @property
    def _mod_font(self) -> tuple:
        return (self.FONT_FAMILY, self._s(self.BASE_FONT_MOD), "bold")

    @property
    def _key_font(self) -> tuple:
        return (self.FONT_FAMILY, self._s(self.BASE_FONT_KEY), "bold")

    # ── UI 構築 ──────────────────────────────────────────────
    def _build_ui(self, initial: bool = False) -> None:
        """現在のスケール・色設定でバー全体を (再) 描画する。"""
        self.canvas.delete("all")

        px = self._s(self.BASE_PAD_X)
        py = self._s(self.BASE_PAD_Y)
        rg = self._s(self.BASE_ROW_GAP)
        mpx = self._s(self.BASE_MOD_PAD_X)
        mpy = self._s(self.BASE_MOD_PAD_Y)
        mg = self._s(self.BASE_MOD_GAP)
        mr = self._s(self.BASE_MOD_RADIUS)

        # 修飾キーテキスト幅を計測
        mw: dict[str, int] = {}
        for name in MODIFIER_ORDER:
            tid = self.canvas.create_text(0, 0, text=name, font=self._mod_font, anchor="nw")
            bb = self.canvas.bbox(tid)
            self.canvas.delete(tid)
            mw[name] = (bb[2] - bb[0]) if bb else 30

        mod_h = self._s(self.BASE_FONT_MOD) + mpy * 2 + self._s(self.BASE_EXTRA_H)
        total_mw = sum(w + mpx * 2 for w in mw.values()) + mg * (len(MODIFIER_ORDER) - 1)

        # 下段テキスト高さ
        tid = self.canvas.create_text(0, 0, text="X", font=self._key_font, anchor="nw")
        bb = self.canvas.bbox(tid)
        self.canvas.delete(tid)
        key_h = (bb[3] - bb[1]) if bb else 24

        self._bar_w = int(total_mw + px * 2)
        self._bar_h = int(py + mod_h + rg + key_h + py)

        # ウィンドウ配置
        if initial:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            wx = (sw - self._bar_w) // 2
            wy = sh - self.BAR_MARGIN_BOTTOM - self._bar_h
        else:
            wx = self.root.winfo_x()
            wy = self.root.winfo_y()

        self.root.geometry(f"{self._bar_w}x{self._bar_h}+{wx}+{wy}")
        self.canvas.configure(width=self._bar_w, height=self._bar_h)
        self.canvas.pack(fill="both", expand=True)

        # 上段: 修飾キーインジケーター
        self._mod_items: dict[str, dict] = {}
        mod_cy = py + mod_h // 2
        x = (self._bar_w - total_mw) // 2

        for name in MODIFIER_ORDER:
            pw = mw[name] + mpx * 2
            x1, y1 = x, mod_cy - mod_h // 2
            x2, y2 = x + pw, mod_cy + mod_h // 2
            active = self._modifier_active.get(name, False)
            bg = self.mod_active_bg if active else self.mod_inactive_bg
            fg = self.mod_active_fg if active else self.mod_inactive_fg

            bg_id = self._rounded_rect(x1, y1, x2, y2, mr, bg, bg)
            text_id = self.canvas.create_text(
                (x1 + x2) // 2, mod_cy, text=name,
                font=self._mod_font, fill=fg, anchor="center",
            )
            self._mod_items[name] = {
                "bg_id": bg_id, "text_id": text_id,
                "x1": x1, "y1": y1, "x2": x2, "y2": y2, "r": mr,
            }
            x = x2 + mg

        # 下段: キー表示（右寄せ）
        key_cy = py + mod_h + rg + key_h // 2
        self._key_text_id = self.canvas.create_text(
            self._bar_w - px, key_cy, text="",
            font=self._key_font, fill=self.key_fg, anchor="e",
        )
        if self._key_history:
            self.canvas.itemconfig(self._key_text_id, text="  ".join(self._key_history))

    # ── イベント ─────────────────────────────────────────────
    def _bind_events(self) -> None:
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_b1_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)

    def _get_edge(self, x: int, y: int) -> str:
        e = self.EDGE_ZONE
        l, r, t, b = x < e, x >= self._bar_w - e, y < e, y >= self._bar_h - e
        if t and l: return "tl"
        if t and r: return "tr"
        if b and l: return "bl"
        if b and r: return "br"
        if l: return "l"
        if r: return "r"
        if t: return "t"
        if b: return "b"
        return ""

    def _on_motion(self, event: tk.Event) -> None:
        self.canvas.configure(cursor=self._EDGE_CURSORS.get(self._get_edge(event.x, event.y), ""))

    def _on_press(self, event: tk.Event) -> None:
        edge = self._get_edge(event.x, event.y)
        if edge:
            self._resizing = True
            self._resize_edge = edge
            self._resize_start_x = event.x_root
            self._resize_start_y = event.y_root
            self._resize_start_scale = self._scale
        else:
            self._resizing = False
            self._drag_x = event.x
            self._drag_y = event.y

    def _on_b1_motion(self, event: tk.Event) -> None:
        if self._resizing:
            dx = event.x_root - self._resize_start_x
            dy = event.y_root - self._resize_start_y
            edge = self._resize_edge
            if edge in ("r", "br", "tr"):    delta = dx
            elif edge in ("l", "bl", "tl"):  delta = -dx
            elif edge == "b":                delta = dy
            else:                            delta = -dy  # "t"
            new = self._resize_start_scale * (1.0 + delta / self._base_bar_w)
            new = max(self.SCALE_MIN, min(self.SCALE_MAX, round(new, 2)))
            if new != self._scale:
                self._scale = new
                self._build_ui()
        else:
            self.root.geometry(
                f"+{self.root.winfo_x() + event.x - self._drag_x}"
                f"+{self.root.winfo_y() + event.y - self._drag_y}"
            )

    def _on_release(self, _: tk.Event) -> None:
        self._resizing = False

    # ── 修飾キー更新 ─────────────────────────────────────────
    def update_modifier(self, name: str, active: bool) -> None:
        self.root.after(0, self._set_modifier, name, active)

    def _set_modifier(self, name: str, active: bool) -> None:
        if name not in self._mod_items:
            return
        self._modifier_active[name] = active
        info = self._mod_items[name]
        self.canvas.delete(info["bg_id"])
        bg = self.mod_active_bg if active else self.mod_inactive_bg
        fg = self.mod_active_fg if active else self.mod_inactive_fg
        info["bg_id"] = self._rounded_rect(
            info["x1"], info["y1"], info["x2"], info["y2"], info["r"], bg, bg,
        )
        self.canvas.itemconfig(info["text_id"], fill=fg)
        self.canvas.tag_raise(info["text_id"])

    # ── キー表示 ─────────────────────────────────────────────
    def show_key(self, text: str) -> None:
        self.root.after(0, self._display_key, text)

    def _display_key(self, text: str) -> None:
        self._cancel_timers()
        now = time.monotonic()
        if now - self._last_key_time > KEY_CHAIN_TIMEOUT:
            self._key_history.clear()
        self._last_key_time = now
        self._key_history.append(text)
        if len(self._key_history) > MAX_KEY_HISTORY:
            self._key_history = self._key_history[-MAX_KEY_HISTORY:]
        self.canvas.itemconfig(
            self._key_text_id,
            text="  ".join(self._key_history), fill=self.key_fg, font=self._key_font,
        )
        self._hide_after_id = self.root.after(self.KEY_DISPLAY_MS, self._start_fade)

    def _start_fade(self) -> None:
        self._fade_remaining = self.KEY_FADE_STEPS
        self._fade_step()

    def _fade_step(self) -> None:
        if self._fade_remaining <= 0:
            self.canvas.itemconfig(self._key_text_id, text="")
            self._key_history.clear()
            return
        t = self._fade_remaining / self.KEY_FADE_STEPS
        color = _lerp_color(self.bar_bg, self.key_fg, t)
        self.canvas.itemconfig(self._key_text_id, fill=color)
        self._fade_remaining -= 1
        self._fade_after_id = self.root.after(self.KEY_FADE_INTERVAL_MS, self._fade_step)

    def _cancel_timers(self) -> None:
        for aid in ("_fade_after_id", "_hide_after_id"):
            v = getattr(self, aid)
            if v:
                self.root.after_cancel(v)
                setattr(self, aid, None)

    # ── 設定変更（トレイメニューから呼ばれる） ────────────────
    def pick_bar_bg(self) -> None:
        self.root.after(0, self._pick_color, "bar_bg", "背景色")

    def pick_key_fg(self) -> None:
        self.root.after(0, self._pick_color, "key_fg", "文字色")

    def _pick_color(self, attr: str, title: str) -> None:
        color = colorchooser.askcolor(color=getattr(self, attr), title=title)[1]
        if color:
            setattr(self, attr, color)
            self.root.configure(bg=self.bar_bg)
            self.canvas.configure(bg=self.bar_bg)
            self._build_ui()

    def set_alpha(self, alpha: float) -> None:
        self.root.after(0, self._apply_alpha, alpha)

    def _apply_alpha(self, alpha: float) -> None:
        self.bar_alpha = alpha
        self.root.attributes("-alpha", alpha)

    # ── Win32 ────────────────────────────────────────────────
    def _apply_win32_flags(self) -> None:
        self.root.update_idletasks()
        hwnd = _user32.GetParent(self.root.winfo_id())
        style = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)

    # ── Canvas ───────────────────────────────────────────────
    def _rounded_rect(self, x1: int, y1: int, x2: int, y2: int,
                      r: int, fill: str, outline: str) -> int:
        pts = [
            x1+r, y1, x2-r, y1, x2, y1, x2, y1+r,
            x2, y2-r, x2, y2, x2-r, y2, x1+r, y2,
            x1, y2, x1, y2-r, x1, y1+r, x1, y1,
        ]
        return self.canvas.create_polygon(pts, fill=fill, outline=outline, smooth=True, width=1)

    def quit(self) -> None:
        self.root.quit()


# ═════════════════════════════════════════════════════════════
#  KeyListener
# ═════════════════════════════════════════════════════════════
class KeyListener:
    """pynput グローバルキーリスナー。修飾キープレフィックス付きで通知。"""

    def __init__(self, on_key: Callable[[str], None], on_mod: Callable[[str, bool], None]) -> None:
        self._on_key = on_key
        self._on_mod = on_mod
        self._mods: set[str] = set()
        self._listener: keyboard.Listener | None = None

    def start(self) -> None:
        self._listener = keyboard.Listener(on_press=self._press, on_release=self._release)
        self._listener.daemon = True
        self._listener.start()

    def stop(self) -> None:
        if self._listener:
            self._listener.stop()

    def _press(self, key: keyboard.Key | keyboard.KeyCode) -> None:
        if key in MODIFIER_KEYS:
            name = MODIFIER_KEYS[key]
            self._mods.add(name)
            self._on_mod(name, True)
            return
        kn = _get_key_name(key)
        if kn is None:
            return
        active = [m for m in MODIFIER_ORDER if m in self._mods]
        self._on_key("+".join(active + [kn]) if active else kn)

    def _release(self, key: keyboard.Key | keyboard.KeyCode) -> None:
        if key in MODIFIER_KEYS:
            name = MODIFIER_KEYS[key]
            self._mods.discard(name)
            self._on_mod(name, False)


# ═════════════════════════════════════════════════════════════
#  SystemTray
# ═════════════════════════════════════════════════════════════
class SystemTray:
    """pystray トレイアイコン。背景色・文字色・透過度メニュー付き。"""

    _ALPHA_PRESETS = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]

    def __init__(self, overlay: OverlayWindow) -> None:
        self._ov = overlay
        self._icon: Icon | None = None

    def start(self) -> None:
        alpha_menu = Menu(*(
            MenuItem(
                f"{int(a * 100)}%",
                self._alpha_action(a),
                radio=True,
                checked=self._alpha_check(a),
            ) for a in self._ALPHA_PRESETS
        ))
        menu = Menu(
            MenuItem("背景色...", lambda *_: self._ov.pick_bar_bg()),
            MenuItem("文字色...", lambda *_: self._ov.pick_key_fg()),
            MenuItem("透過度", alpha_menu),
            Menu.SEPARATOR,
            MenuItem("終了", self._quit),
        )
        self._icon = Icon("KeyShow", _create_tray_icon_image(), "KeyShow", menu=menu)
        threading.Thread(target=self._icon.run, daemon=True).start()

    def _alpha_action(self, a: float) -> Callable:
        return lambda *_: self._ov.set_alpha(a)

    def _alpha_check(self, a: float) -> Callable:
        return lambda _: abs(self._ov.bar_alpha - a) < 0.05

    def _quit(self, icon: Icon, _: MenuItem) -> None:
        icon.stop()
        self._ov.quit()

    def stop(self) -> None:
        if self._icon:
            self._icon.stop()


# ═════════════════════════════════════════════════════════════
#  エントリーポイント
# ═════════════════════════════════════════════════════════════
def main() -> None:
    overlay = OverlayWindow()
    listener = KeyListener(on_key=overlay.show_key, on_mod=overlay.update_modifier)
    listener.start()
    tray = SystemTray(overlay)
    tray.start()
    try:
        overlay.root.mainloop()
    finally:
        listener.stop()
        tray.stop()


if __name__ == "__main__":
    main()
