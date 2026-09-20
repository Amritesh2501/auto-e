"""Auto E - watches a timing bar and presses the letter in the box when the line reaches it.

    py auto_e.py      run the app
    build.bat         make a shareable dist\\AutoE.exe

Developer menu: press and hold the "Auto E" title for 3 seconds.
"""
import atexit
import ctypes
import json
import logging
import os
import queue
import random
import string
import sys
import threading
import time
import traceback
import tkinter as tk
from collections import Counter, deque
from copy import deepcopy
from ctypes import wintypes
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import ttk

import cv2
import mss
import numpy as np
from PIL import Image, ImageTk

import detector as det

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # real pixels, so window coords match screen captures
except OSError:
    pass
sys.setswitchinterval(0.001)  # the detection thread hands the GIL back to the UI quickly -> no UI stutter

APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "AutoE"
CONFIG = APP_DIR / "config.json"
TRIGGERS = ["Line hits box", "Letter appears"]
DEFAULTS = {
    "hotkey": "F8", "trigger": TRIGGERS[0], "margin": 10, "delay": 0, "aim_ms": 20, "hold": 50, "cooldown": 300,
    "retries": 1, "retry_ms": 250, "background": False, "jiggle": True, "jiggle_secs": 120,
    "min_conf": 75, "digits": False, "max_fps": 60,
    "minimize": True, "outline": True, "hide_capture": True, "hide_taskbar": False, "topmost": True,
    "dev": False, "verbose": False, "dry_run": False, "show_mask": False,
    # the full-auto cycle, run top to bottom then round again. key = one key, or text to type (sent with
    # Enter after it). wait = "bar" (until the bar's key is pressed) or a number of minutes. watch = keep
    # reading the region during that wait.
    "auto": [
        {"on": True, "key": "E", "wait": "bar", "watch": True},
        {"on": True, "key": "1", "wait": "5", "watch": True},
        {"on": True, "key": "T", "wait": "0", "watch": False},
        {"on": True, "key": "e yoga", "wait": "10", "watch": False},
        {"on": True, "key": "X", "wait": "0", "watch": True},
        {"on": False, "key": "", "wait": "0", "watch": True},
        {"on": False, "key": "", "wait": "0", "watch": True},
        {"on": False, "key": "", "wait": "0", "watch": True},
    ],
    "skill": {"region": None, "key": "Auto"},
    "custom": {"region": None, "box": det.GYM["box"], "line": det.GYM["line"],
               "box_pixel": None, "line_pixel": None, "tolerance": 60},
}
MODE_OF_PAGE = {"Skill bar": "skill", "Custom": "custom"}
PAGE_OF_MODE = {v: k for k, v in MODE_OF_PAGE.items()}
WDA_EXCLUDEFROMCAPTURE = 0x11
# skill bar key choice: Auto = press the letter read from the bar
KEYS = ["Auto"] + list(string.ascii_uppercase) + list(string.digits) + ["Space"]

# palette
BG, PANEL, CARD, FIELD, BORDER = "#0a0c11", "#0f1219", "#151923", "#1e2331", "#2a3042"
TEXT, MUTED, DIM, ACCENT, ACCENT_HI = "#eceef5", "#8b93a7", "#5b6275", "#7c6cff", "#978bff"
GREEN, YELLOW, RED, AMBER = "#34d399", "#facc15", "#f87171", "#f0a44b"
FONT = "Segoe UI"
# trigger zone / outline: yellow while watching, green right after a press, red right after a miss
ZONE = {"idle": YELLOW, "press": GREEN, "miss": RED}
ZONE_BGR = {"idle": (21, 204, 250), "press": (153, 211, 52), "miss": (113, 113, 248)}
LINE_BGR = (255, 108, 124)  # accent violet


def load_config():
    cfg = deepcopy(DEFAULTS)
    try:
        for k, v in json.loads(CONFIG.read_text()).items():
            if isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            elif k in cfg:
                cfg[k] = v
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2))


# ---------- logging (shown in the developer menu, also written to %APPDATA%\AutoE\autoe.log) ----------
class Ring(logging.Handler):
    """Keeps recent records in memory for the developer menu."""

    def __init__(self):
        super().__init__()
        self.records, self.counts, self.serial = deque(maxlen=3000), Counter(), 0

    def emit(self, record):
        self.serial += 1
        record.serial = self.serial
        self.records.append(record)
        self.counts[record.levelname] += 1


log = logging.getLogger("autoe")
ring = Ring()
_last_said = {}


def say_once(key, level, msg, every=30):
    """Log at most once per `every` seconds per key, so a repeating problem doesn't flood the log."""
    if time.time() - _last_said.get(key, 0) >= every:
        _last_said[key] = time.time()
        log.log(level, msg)


def setup_logging():
    if log.handlers:
        return
    APP_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S")
    handlers = [ring]
    try:
        handlers.append(RotatingFileHandler(APP_DIR / "autoe.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"))
    except OSError:
        pass
    for h in handlers:
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)
    sys.excepthook = lambda *exc: log.critical("Unhandled error", exc_info=exc)
    threading.excepthook = lambda a: log.critical(f"Thread '{a.thread.name}' crashed",
                                                  exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


def write_image(path, img):
    """cv2.imwrite can't handle non-ASCII paths (e.g. a user name with accents) on Windows."""
    Path(path).write_bytes(cv2.imencode(".png", img)[1].tobytes())


# ---------- windows ----------
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetForegroundWindow.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = (wintypes.POINT,)
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
user32.GetAncestor.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.SystemParametersInfoW.argtypes = (wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT)
user32.BringWindowToTop.argtypes = user32.IsIconic.argtypes = (wintypes.HWND,)
user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
user32.SetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_long)
user32.SetWindowDisplayAffinity.argtypes = (wintypes.HWND, wintypes.DWORD)


def hwnd_of(win):
    win.update_idletasks()
    return int(win.wm_frame(), 16)


def hide_from_capture(win, hidden=True):
    """Exclude a window from screenshots, recordings, OBS/Discord/Teams shares. Needs Windows 10 2004+."""
    ok = bool(user32.SetWindowDisplayAffinity(hwnd_of(win), WDA_EXCLUDEFROMCAPTURE if hidden else 0))
    if not ok:
        say_once("affinity", logging.WARNING, "Could not hide a window from capture (needs Windows 10 2004+)", 300)
    return ok


def click_through(win):
    """Mouse passes through, never takes focus, no taskbar/Alt+Tab entry."""
    hwnd = hwnd_of(win)
    # WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    user32.SetWindowLongW(hwnd, -20, user32.GetWindowLongW(hwnd, -20) | 0x80000 | 0x20 | 0x80 | 0x8000000)


def is_ours(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value == os.getpid()


def window_under(region):
    """Top-level window showing the region (the game / browser), skipping this app's own windows."""
    l, t, w, h = region["left"], region["top"], region["width"], region["height"]
    for x, y in ((l + w // 2, t + h // 2), (l + 2, t + 2), (l + w - 3, t + h - 3), (l + 2, t + h - 3)):
        hwnd = user32.GetAncestor(user32.WindowFromPoint(wintypes.POINT(x, y)), 2)  # 2 = GA_ROOT
        if hwnd and not is_ours(hwnd):
            return hwnd
    return None


def title_of(hwnd):
    buf = ctypes.create_unicode_buffer(120)
    user32.GetWindowTextW(hwnd, buf, 120)
    return buf.value or "?"


# ---------- keyboard input ----------
# keys with no printable character; everything else comes from the current keyboard layout
VKS = {"Space": 0x20, "Enter": 0x0D, "Backspace": 0x08, "Tab": 0x09, "Esc": 0x1B}


def vk_of(ch):
    return VKS.get(ch) or user32.VkKeyScanW(ord(ch.lower())) & 0xFF


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("pad", ctypes.c_byte * 32)]  # pad = biggest member
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def cursor_at():
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def cursor_in(hwnd):
    """True when the pointer is inside that window. A nudge must never drag a pointer you are using somewhere
    else, on a second monitor for instance."""
    if not hwnd:
        return True
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return True
    x, y = cursor_at()
    return rect.left <= x < rect.right and rect.top <= y < rect.bottom


def nudge_mouse():
    """A few pixels and straight back: enough for an AFK check to see mouse input, and the same distance each
    way so a game's camera lands where it started."""
    dx, dy = random.choice(((7, 0), (-7, 0), (0, 6), (0, -6), (6, 5), (-6, -5)))
    home = cursor_at()
    move_mouse(dx, dy)
    time.sleep(0.12)
    move_mouse(-dx, -dy)
    # "enhance pointer precision" scales a move by how fast it is, and the two moves are not equally fast, so
    # they don't cancel to the pixel. Put the pointer back exactly: SetCursorPos is not input, so it neither
    # counts against the AFK timer nor reaches the game.
    user32.SetCursorPos(*home)
    return dx, dy


def move_mouse(dx, dy):
    """A relative mouse move, as real input. SetCursorPos would warp the pointer without counting as input at
    all, which is exactly what an AFK check looks for."""
    inp = INPUT(type=0, mi=MOUSEINPUT(dx, dy, 0, 0x0001, 0, 0))  # INPUT_MOUSE, MOUSEEVENTF_MOVE
    if not user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)):
        say_once("sendinput_mouse", logging.ERROR, f"Mouse SendInput failed (error {ctypes.get_last_error()})", 10)


def send_key(ch, hold):
    """Real key down, hold, key up, as a hardware scan code. Games reading DirectInput / raw input (GTA/FiveM,
    most engines) only see scan codes; Windows also turns it into normal key messages for browsers and apps.
    Holding matters: games that check keys once per frame miss 0ms taps."""
    vk = vk_of(ch)
    scan = user32.MapVirtualKeyW(vk, 0)  # MAPVK_VK_TO_VSC, for the current keyboard layout
    for flags in (0x8, 0x8 | 0x2):  # KEYEVENTF_SCANCODE, then | KEYEVENTF_KEYUP
        inp = INPUT(type=1, ki=KEYBDINPUT(0, scan, flags, 0, 0))
        if not user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)):
            say_once("sendinput", logging.ERROR, f"SendInput failed (error {ctypes.get_last_error()})", 10)
        if not flags & 0x2:  # hold between key down and key up
            time.sleep(hold)


_fg_timeout = None


def unlock_foreground():
    """Windows holds the foreground for the window you last used and refuses SetForegroundWindow to everyone
    else, which is why presses stop landing the moment you alt-tab away from the game. A zero lock timeout
    lifts that. Windows only accepts the change from the foreground window, so this runs when you press Start,
    while the Auto E window still has focus. Nothing is written to the registry and the old value goes back
    when the app closes."""
    global _fg_timeout
    val = ctypes.c_uint()
    user32.SystemParametersInfoW(0x2000, 0, ctypes.byref(val), 0)  # SPI_GETFOREGROUNDLOCKTIMEOUT
    if not val.value:
        return True
    if _fg_timeout is None:
        _fg_timeout = val.value
    ok = bool(user32.SystemParametersInfoW(0x2001, 0, ctypes.c_void_p(0), 2))  # 0 ms, SPIF_SENDCHANGE
    log.log(logging.INFO if ok else logging.WARNING, f"Foreground lock timeout {val.value} ms -> 0: "
            + ("done, keys now reach the game while you're in another window"
               if ok else "refused by Windows (click the Auto E window, then Start)"))
    return ok


@atexit.register
def restore_foreground():
    global _fg_timeout
    if _fg_timeout:
        user32.SystemParametersInfoW(0x2001, 0, ctypes.c_void_p(_fg_timeout), 2)
    _fg_timeout = None


def focus_window(hwnd):
    """Bring the game forward so a key actually lands in it. SetForegroundWindow is refused to background apps;
    lifting the foreground lock, an empty input and sharing the foreground thread's input state all help, and
    the shell's own SwitchToThisWindow is the last resort when the documented route is still refused."""
    if user32.GetForegroundWindow() == hwnd:
        return True
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    for attempt in range(3):
        # a key *up* for an unused key: enough to count as input from us, and nothing stays held down
        user32.SendInput(1, ctypes.byref(INPUT(type=1, ki=KEYBDINPUT(0, 0, 0x2, 0, 0))), ctypes.sizeof(INPUT))
        fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        me = ctypes.windll.kernel32.GetCurrentThreadId()
        attached = fg_thread and fg_thread != me and user32.AttachThreadInput(me, fg_thread, True)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        if attempt:
            user32.SwitchToThisWindow(hwnd, True)  # undocumented, but it is what Alt+Tab itself uses
        if attached:
            user32.AttachThreadInput(me, fg_thread, False)
        for _ in range(10):
            if user32.GetForegroundWindow() == hwnd:
                return True
            time.sleep(0.01)
    return False


user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.FindWindowExW.argtypes = (wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)
user32.FindWindowExW.restype = wintypes.HWND


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND), ("rcCaret", wintypes.RECT)]


def message_target(hwnd):
    """Where a posted key has to go. A top-level window usually hands its keyboard input to a child, so a
    message posted to the frame is dropped: ask the window's own UI thread which child holds the focus, and
    fall back to a browser's render widget, then to the frame itself."""
    info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    tid = user32.GetWindowThreadProcessId(hwnd, None)
    if tid and user32.GetGUIThreadInfo(tid, ctypes.byref(info)) and info.hwndFocus:
        return info.hwndFocus
    # Chrome / Edge / Electron take keyboard messages on their render widget child, not the top-level window
    return user32.FindWindowExW(hwnd, None, "Chrome_RenderWidgetHostHWND", None) or hwnd


def post_key(hwnd, ch, hold):
    """Key down/up posted straight into a window's message queue, so it lands while another window has focus
    and even while this one is minimized. Anything reading window messages takes it -- browsers, most UI
    toolkits, chat boxes. A game that reads only raw input or DirectInput (GTA V / FiveM) will not."""
    vk = vk_of(ch)
    scan = user32.MapVirtualKeyW(vk, 0)
    target = message_target(hwnd)
    for msg, bits in ((0x100, 0), (0x101, 0xC0000000)):  # WM_KEYDOWN, WM_KEYUP (+ previous-state/transition bits)
        lparam = wintypes.LPARAM(1 | scan << 16 | bits)
        if not user32.PostMessageW(target, msg, vk, lparam):
            say_once("postmessage", logging.ERROR, f"PostMessage failed (error {ctypes.get_last_error()})", 10)
        if msg == 0x100:
            if len(ch) == 1 and ch.isprintable():
                user32.PostMessageW(target, 0x102, ord(ch), lparam)  # WM_CHAR: a text box needs the character
            time.sleep(hold)


def reach(cfg, region, target=None):
    """Make the game window ready to take a key, *before* the press is timed. Focusing costs up to a few
    hundred ms, so doing it at press time (as this used to) made every press that needed it land late."""
    if not (target and user32.IsWindow(target)):
        target = window_under(region)
    if target and not cfg["background"] and user32.GetForegroundWindow() != target:
        if focus_window(target):
            time.sleep(0.15)  # a just-activated window queues input; a press sent now arrives squashed to ~0ms
    return target


def press(letter, cfg, region, target=None):
    """Send the key to the game. A real key always goes to the foreground window, so if the game isn't in
    front the key is brought to it rather than typed into whatever you alt-tabbed to -- that is how presses
    ended up in this app's own window and in Discord. Returns where the key went, or why it didn't."""
    if not (target and user32.IsWindow(target)):
        target = window_under(region)
    if target and user32.GetForegroundWindow() != target:
        if cfg["background"]:
            if user32.IsIconic(target):
                say_once("iconic", logging.WARNING, "The game window is minimized. Windows stops it drawing, so "
                         "the bar can't be read at all -- use windowed borderless and leave it on screen.", 60)
            post_key(target, letter, cfg["hold"] / 1000)
            return f"{title_of(target)} (background)"
        if not focus_window(target) and user32.GetForegroundWindow() != target:
            # refused (an elevated window has the foreground?): posting is better than sending it elsewhere,
            # though a game reading only raw input won't take it
            post_key(target, letter, cfg["hold"] / 1000)
            return f"{title_of(target)} (background, couldn't focus it)"
    send_key(letter, cfg["hold"] / 1000)
    return title_of(user32.GetForegroundWindow())


# ---------- detection ----------
class Watcher(threading.Thread):
    """Captures the region, finds box/line/letter, queues key presses. The GUI only reads its attributes."""

    def __init__(self, cfg):
        super().__init__(daemon=True, name="watcher")
        self.cfg, self.mode, self.armed = cfg, "skill", False
        self.target = None  # game window locked in at start: alt-tabbing can't redirect keys to another window
        self.frame = self.raw = self.mask = self.last_line = None
        self.mask_at, self.line_seen, self.last_speed = (0, 0), 0.0, 0
        self.vel, self.frame_dt, self.captured = 0.0, 1 / 60, 0.0
        self.steady = self.still = self.odd = 0  # line tracking: consistent moves, sub-pixel checks, glitches
        self.last_box, self.box_seen = None, 0.0
        self.frame_id, self.hit, self.box, self.line = 0, False, None, None
        self.status, self.letter, self.score, self.top = "Loading letters...", None, 0.0, []
        self.presses, self.log = Counter(), deque(maxlen=200)
        self.perf = {"fps": 0.0, "grab": 0.0, "detect": 0.0}
        self.templates, self.digits = None, None
        self.last_press, self.last_miss, self.misses, self.last_sent = (0.0, None), 0.0, 0, 0.0
        self.jobs = queue.Queue()
        self.last_jiggle = time.time()
        self.reset_round()
        threading.Thread(target=self.press_loop, daemon=True, name="presser").start()
        threading.Thread(target=self.jiggle_loop, daemon=True, name="jiggler").start()

    def reset_round(self):
        self.box_x = self.done = self.pending = self.sig = self.clean_box = self.pressed_x = None
        self.unsure_warned, self.box_since, self.box_frames, self.was_hit = False, 0.0, 0, False

    def look(self):
        """'miss' / 'press' / 'idle': which color the zone and outline show right now."""
        now = time.time()
        return "miss" if now - self.last_miss < 0.6 else "press" if now - self.last_sent < 0.4 else "idle"

    def missed(self, text):
        self.last_miss, self.misses = time.time(), self.misses + 1
        self.log.appendleft(f"{time.strftime('%H:%M:%S')}  MISSED  {text}")
        log.warning(f"Missed: {text}")

    def queue_press(self, letter, region, why, wait=0.0):
        """Presses run on their own thread so holding a key never pauses detection. The key is sent `wait` s from
        now: the exact moment the line reaches the target, finer than one check."""
        self.last_press = (time.time(), letter)  # for the cooldown; counted as pressed only once really sent
        self.jobs.put((letter, dict(region), why, time.perf_counter() + wait))

    def press_loop(self):
        while True:
            letter, region, why, at = self.jobs.get()
            cfg = self.cfg
            if why in ("pressed", "retry") and not self.armed:
                continue  # stopped watching between aiming and sending: never fire into a chat box
            try:
                if why == "pressed":
                    at += cfg["delay"] / 1000
                target = reach(cfg, region, self.target)  # before the wait: focusing is slow, aiming is not
                while (left := at - time.perf_counter()) > 0:
                    time.sleep(left)  # Python 3.11+ sleeps to ~1 ms on Windows
                where = ("(dry run, no key sent)" if cfg["dev"] and cfg["dry_run"]
                         else press(letter, cfg, region, target))
            except Exception:
                log.exception(f"Pressing {letter} failed")
                self.missed(f"{why} {letter} -> failed, see the log")
                continue
            text = f"{why} {letter} -> {where}"
            if where.startswith("NOT PRESSED"):
                self.missed(text)
            else:
                self.last_sent = time.time()
                if why != "TEST":
                    self.presses[letter] += 1
                self.log.appendleft(f"{time.strftime('%H:%M:%S')}  {text}")
                log.info(text)

    def jiggle_loop(self):
        """Nudge the mouse every so often so an AFK kick doesn't see it sitting still. Guarded three ways: only
        while running, only while the game is the window in front (otherwise it would drag the pointer around
        whatever you are doing), and never while a bar is on screen, since in a game a mouse move turns the
        camera and that changes the world behind a see-through bar mid-round."""
        while True:
            time.sleep(1)
            cfg = self.cfg
            if not (self.armed and cfg["jiggle"]):
                self.last_jiggle = time.time()  # starting up shouldn't fire one straight away
                continue
            if time.time() - self.last_jiggle < max(10, cfg["jiggle_secs"]):
                continue
            if self.box_x is not None or not cursor_in(self.target):
                continue
            if self.target and not cfg["background"] and user32.GetForegroundWindow() != self.target:
                continue  # focus mode: the game isn't in front, so a nudge would go to whatever is
            self.last_jiggle = time.time()
            log.debug(f"Mouse nudge {nudge_mouse()} and back")

    def run(self):
        frames, t_fps = 0, time.perf_counter()
        with mss.mss() as sct:
            while True:
                started = time.perf_counter()
                try:
                    self.step(sct)
                except Exception as e:
                    say_once(f"err:{e!r}", logging.ERROR, "Watcher error:\n" + "".join(
                        traceback.format_exception(e)).rstrip(), 10)
                    time.sleep(0.2)
                frames += 1
                now = time.perf_counter()
                if now - t_fps >= 1:
                    self.perf["fps"] = frames / (now - t_fps)
                    frames, t_fps = 0, now
                    if self.armed and self.perf["fps"] < min(25, 0.6 * self.cfg["max_fps"]):
                        say_once("slow", logging.WARNING, f"Detection is slow ({self.perf['fps']:.0f} fps, capture "
                                 f"{self.perf['grab']:.1f} ms). Select a smaller region.", 60)
                # running: cap checks per second so a game on the same PC keeps its CPU
                frame_time = 1 / max(10, self.cfg["max_fps"]) if self.armed else 0.03
                time.sleep(max(0.0, frame_time - (time.perf_counter() - started)))

    def step(self, sct):
        cfg = self.cfg
        if self.digits != cfg["digits"]:
            self.status = "Loading letters..."
            t = time.perf_counter()
            self.templates, self.digits = det.build_templates(cfg["digits"]), cfg["digits"]
            if len(self.templates[0]):
                log.info(f"Loaded {len(self.templates[0])} letter shapes in {1000 * (time.perf_counter() - t):.0f} ms")
            else:
                log.error("No Windows fonts found, letters can't be read")
        region = cfg[self.mode]["region"]
        if not region:
            self.frame = self.raw = None
            self.status = "Select a region first"
            time.sleep(0.1)
            return

        t0 = time.perf_counter()
        try:
            img = np.array(sct.grab(region))[:, :, :3]
        except mss.exception.ScreenShotError as e:
            say_once("grab", logging.WARNING, f"Screen capture failed: {e}", 10)
            time.sleep(0.5)
            return
        t1 = time.perf_counter()
        now = time.time() - (t1 - t0) / 2  # when the pixels were captured: line speed and aim are measured from here
        self.frame_dt = 0.8 * self.frame_dt + 0.2 * min(0.2, now - self.captured)
        self.captured = now

        skill = self.mode == "skill"
        if skill:  # no coloured target: the letter itself marks the spot
            line, lbox, lmask = det.find_marks(img)
            box = ((lbox[0], lbox[1]), (lbox[2], lbox[3])) if lbox else None
        else:
            specs = cfg["custom"]
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            box = det.find_box(hsv, specs["box"])
            line = det.find_line(hsv, specs["line"], box[1]) if box else None
        view = img.copy()
        hit, land, wait = False, None, None
        recent = self.last_line is not None and now - self.line_seen < 0.2
        if line is not None and recent and self.steady:
            # far from where a steadily moving line should be: a glitch (a letter stroke, something bright behind
            # the bar). Skip it, unless it keeps happening: then the line really jumped (restarted).
            expect = self.last_line + self.vel * (now - self.line_seen)
            if abs(line - expect) > 2.5 * abs(self.vel) * self.frame_dt + 6 and self.odd < 2:
                self.odd, line = self.odd + 1, None
            else:
                self.odd = 0
        if line is not None:
            jump = line - self.last_line if recent else 0
            if abs(jump) > img.shape[1] / 4:
                jump = None  # a jump over a quarter of the bar is the line restarting, not speed
            self.last_speed = abs(jump or 0)  # px per check
            if jump is None or not recent:
                self.vel, self.steady, self.still = 0.0, 0, 0
            elif jump == 0:  # a slow line moves under 1px some checks: keep the speed for a few
                self.still += 1
                if self.still > 3:
                    self.vel, self.steady = 0.0, 0
            else:
                v, self.still = jump / (now - self.line_seen), 0
                if self.vel and (v > 0) == (self.vel > 0):
                    self.vel, self.steady = (self.vel + v) / 2, self.steady + 1  # px/s, smoothed against jitter
                else:
                    # one sighting-to-sighting jump is already movement. Waiting for a second agreeing sample
                    # cost three checks before any press could be scheduled, and a fast line is past a letter
                    # near the start of the bar by then.
                    self.vel, self.steady = v, 1  # started or turned
            self.last_line, self.line_seen = line, now
        # skill bar: a white line touching the white letter becomes one blob, so the line "vanishes" and the
        # letter's shape is polluted. While that lasts, keep the last clean letter box and don't re-read.
        seen = box is not None  # really on screen this check (not carried over below)
        merged = False
        if skill:
            near = self.clean_box is not None and self.last_line is not None and \
                self.clean_box[0][0] - 12 - 2 * self.last_speed <= self.last_line <= self.clean_box[0][1] + 12 + 2 * self.last_speed
            merged = near and (line is not None or now - self.line_seen < 0.25)
            if merged:
                box = self.clean_box
            elif box and line is not None:  # only trust a letter seen together with the line (= the bar is up)
                self.clean_box = box
        if box:
            self.last_box, self.box_seen = box, now
        elif self.box_x is not None and now - self.box_seen < 0.12:
            box = self.last_box  # a detection dropout for a check or two is not the bar closing
        if box:
            (x0, x1), (y0, y1) = box
            by_line = cfg["trigger"] == TRIGGERS[0]
            self.box_frames += 1
            if self.box_x is None:
                self.box_since, self.box_frames = now, 1
                log.debug(f"Box appeared at x {x0}-{x1}")
            elif abs(x0 - self.box_x) > 5:
                self.box_since, self.box_frames = now, 1  # a different box/letter
                if not by_line:
                    self.done = None  # box moved = new round
            self.box_x = x0
            inside = merged or (line is not None and x0 - 5 <= line <= x1 + 5)
            gx0, gx1, gy0, gy1 = (x0, x1, y0, y1) if skill else det.inner(box)
            if not inside and seen:  # the line would cover the letter
                sig = img[gy0:gy1:2, gx0:gx1:2]
                if self.sig is None or sig.shape != self.sig.shape or not np.array_equal(sig, self.sig):
                    self.sig = sig.copy()  # only re-read when the pixels in the box actually changed
                    self.mask = lmask if skill else det.letter_mask(img, box)
                    self.mask_at = (gx0, gy0)
                    top = det.rank(self.mask, self.templates) if self.mask is not None else []
                    if top != self.top:
                        log.debug(f"Letter read: {' '.join(f'{c}={s:.0%}' for c, s in top) or 'nothing'}")
                    self.top = top
            best, score = self.top[0] if self.top else (None, 0.0)
            letter = best if score >= cfg["min_conf"] / 100 else None
            read, key = letter, cfg[self.mode].get("key", "Auto")
            if key != "Auto":  # the user picked the key: the letter only marks where to press, not what
                letter = key
            if best and not letter and not self.unsure_warned and now - self.box_since > 1:
                self.unsure_warned = True
                say_once("unsure", logging.WARNING,
                         f"Unsure letter read: best {best} at {score:.0%}, below the {cfg['min_conf']}% minimum", 15)
            if by_line and not skill and now - max(self.box_since, self.line_seen) > 4:
                say_once("noline", logging.WARNING, "Box visible for 4s but no line detected. Pick the line color "
                         "in Custom, or set Press when to 'Letter appears'.", 30)

            m = int((x1 - x0) * cfg["margin"] / 100)
            # the line must be moving steadily (a still white object behind a see-through bar is not it, nor is a
            # one-check glitch) and, on the skill bar, the letter must have been seen before this check
            moving = abs(self.vel) > 20 and self.steady >= 1 and (self.box_frames >= 2 or not skill)
            # where the line is: seen, or hidden for a moment (inside the white letter, a glitch), carried on at its speed
            est = line if line is not None else (
                self.last_line + self.vel * (now - self.line_seen) if moving and now - self.line_seen < 0.2 else None)
            if est is not None and moving:
                # where the line would be when a key sent right now lands: detection time so far + game input lag
                land = est + self.vel * (time.time() - now + cfg["aim_ms"] / 1000)
                # a zone only a few px wide is crossed between two checks, so waiting for a closer look
                # means no press at all. Once the speed has held over several checks it is good enough to book
                # the press from further out -- 60 ms of drift at a few % speed error is well under a pixel.
                horizon = 0.06 if self.steady >= 2 else None
                wait = det.press_wait(land, self.vel, x0 + m, x1 - m, self.frame_dt, horizon)
                hit = wait is not None
            elif skill:
                # no usable speed yet (the line just appeared, or restarted right on the letter): press on plain
                # overlap. `inside` also covers the line and the letter merged into one white blob.
                hit = inside
            else:  # a line that isn't moving: plain overlap
                hit = line is not None and x0 + m <= line <= x1 - m
            # the line went past the zone (or is gone / restarted): the next pass may press again
            if est is None:
                gone = True
            elif moving:
                gone = (est > x1 + 5) if self.vel > 0 else (est < x0 - 5)
            else:
                gone = not inside
            if by_line and not gone and (hit or (inside and moving)):  # the line reached the letter this pass
                self.was_hit = True
            if by_line and gone and not hit:
                # a box that already got its key is never also counted as missed (the line passing it again)
                pressed = self.pressed_x is not None and abs(x0 - self.pressed_x) <= 5
                if self.was_hit and self.done is None and self.armed and not pressed:
                    self.missed(f"line passed {letter or 'an unread letter'} without a press")
                self.was_hit, self.done = False, None
            # one result per pass: a sent key = pressed, the line crossing without one = missed. (Judging "not
            # taken" by the letter still showing doesn't work: games keep the bar up for a moment after a good press.)
            ready = (hit or not by_line) and letter and self.armed
            if ready and letter != self.done and now - self.last_press[0] > cfg["cooldown"] / 1000:
                self.queue_press(letter, region, "pressed", wait=wait or 0.0)
                self.done, self.pending, self.pressed_x = letter, [letter, now + (wait or 0.0), 0], x0
                if wait is not None:
                    log.info(f"Aim {letter}: line at {est:.0f}px, key in {1000 * wait:.0f} ms lands at "
                             f"{land + self.vel * wait:.0f}px, zone {x0 + m}-{x1 - m}px (center {(x0 + x1) / 2:.0f}), "
                             f"{self.vel:.0f} px/s{'' if line is not None else ', line hidden'}")
            elif (not by_line and ready and self.pending and self.pending[0] == letter
                  and self.pending[2] < cfg["retries"] and now - self.pending[1] > cfg["retry_ms"] / 1000):
                self.pending[1:3] = [now, self.pending[2] + 1]
                log.info(f"{letter} still showing {cfg['retry_ms']} ms after the press, pressing again "
                         f"({self.pending[2]}/{cfg['retries']})")
                self.queue_press(letter, region, "retry")
            if self.pending and letter and letter != self.pending[0]:
                self.pending = None

            cv2.rectangle(view, (x0 + m, y0), (x1 - m, y1 - 1), ZONE_BGR[self.look()], 2)
            if cfg["dev"] and cfg["show_mask"] and self.mask is not None:
                mx, my = self.mask_at
                if self.mask.shape == view[my:my + self.mask.shape[0], mx:mx + self.mask.shape[1]].shape[:2]:
                    view[my:my + self.mask.shape[0], mx:mx + self.mask.shape[1]][self.mask] = (255, 0, 255)
            self.letter, self.score = letter, score
            self.status = (f"Letter: {read or '?'} ({score:.0%})" + (f"   ·   Key: {key}" if key != "Auto" else "")
                           + f"   ·   Line: {'seen' if line is not None else 'not seen'}")
        else:
            if self.box_x is not None:
                log.debug("Box gone")
            if self.was_hit and self.done is None and self.pressed_x is None and self.armed:
                self.missed("the bar closed before a press")
            self.reset_round()
            self.letter, self.score, self.top, self.mask = None, 0.0, [], None
            self.status = "Waiting for the bar..."
        if line is not None:
            cv2.line(view, (line, 0), (line, view.shape[0] - 1), LINE_BGR, 2)
        t2 = time.perf_counter()
        self.perf["grab"] = 0.9 * self.perf["grab"] + 100 * (t1 - t0)  # ms, smoothed
        self.perf["detect"] = 0.9 * self.perf["detect"] + 100 * (t2 - t1)
        self.box, self.line, self.hit = box, line, hit
        self.raw, self.frame = img, view
        self.frame_id += 1


# ---------- full auto ----------
class FullAuto:
    """Runs cfg["auto"] -- a list of steps -- on its own thread, so nothing here can stall detection. Each
    step sends a key (or types a line) and then waits, either for the watcher to press the bar's key or for
    a number of minutes. At the end of the list it starts over."""

    def __init__(self, cfg, watcher):
        self.cfg, self.w, self.on, self.stage, self.until = cfg, watcher, False, "", 0.0

    def start(self):
        region = self.cfg[self.w.mode]["region"]
        if not region:
            log.warning("Full auto: select a region first")
            return False
        self.w.target = window_under(region)
        self.w.armed = self.on = True
        log.info(f"Full auto started on '{title_of(self.w.target)}'" if self.w.target else "Full auto started")
        threading.Thread(target=self.run, daemon=True, name="fullauto").start()
        return True

    def stop(self):
        if self.on:
            log.info("Full auto stopped")
        self.on = self.w.armed = False
        self.stage, self.w.target = "", None

    def tap(self, *keys, gap=0.12):
        """Scripted keys go through the same path as a bar press, so one rule covers every key this app sends:
        with "press without focusing" on they are posted to the game and focus is never taken, otherwise the
        game is brought to the front first."""
        region = self.cfg[self.w.mode]["region"]
        for k in keys:
            if not self.on:
                return False
            log.info(f"Full auto: {k} -> {press(k, self.cfg, region, self.w.target)}")
            time.sleep(gap)
        return self.on

    def hold(self, minutes, stage):
        """Wait, but check often enough that Stop feels instant."""
        self.stage, self.until = stage, time.time() + 60 * minutes
        while self.on and time.time() < self.until:
            time.sleep(0.2)
        return self.on

    def await_bar(self, timeout=120):
        """Wait until the watcher really presses the bar's key; the rest of the cycle is timed from there."""
        self.stage, self.until = "waiting for the bar", time.time() + timeout
        mark = self.w.last_sent
        while self.on and time.time() < self.until:
            if self.w.last_sent != mark:
                return True
            time.sleep(0.05)
        if self.on:
            log.warning(f"Full auto: no key pressed within {timeout}s, carrying on anyway")
        return False

    def send(self, key):
        """One named key (E, 1, Enter, Backspace, Space...) or, for anything longer, a line typed out and sent
        with Enter -- which is what a chat command like "e yoga" needs."""
        if key in VKS or len(key) == 1:
            return self.tap(key)
        time.sleep(0.4)  # a chat box needs a moment to open before it takes typing
        return self.tap(*key, gap=0.05) and self.tap("Enter")

    def wait_for(self, spec, key):
        """"bar" waits for the watcher to press the bar's key, a number waits that many minutes, 0 doesn't wait."""
        if str(spec).strip().lower() in ("bar", "key"):
            self.stage = f"{key} -> waiting for the bar"
            self.await_bar()
            return self.on
        try:
            minutes = float(str(spec).strip() or 0)
        except ValueError:
            log.warning(f"Full auto: '{spec}' is not a wait, treating it as no wait")
            minutes = 0.0
        return self.hold(minutes, f"{key} -> waiting {minutes:g} min") if minutes > 0 else self.on

    def run(self):
        while self.on:
            steps = [s for s in self.cfg["auto"] if s.get("on") and str(s.get("key", "")).strip()]
            if not steps:
                log.warning("Full auto: no steps are switched on")
                break
            for step in steps:
                if not self.on:
                    break
                key = str(step["key"]).strip()
                # watching drives the outline too, so a step that runs while nothing is on screen (an emote)
                # takes the region off as well
                self.w.armed = bool(step.get("watch", True))
                self.stage = key
                if not self.send(key) or not self.wait_for(step.get("wait", "0"), key):
                    break
        self.stage = ""


# ---------- UI ----------
def dark_titlebar(win):
    """Windows 10/11 dark title bar tinted to the app background (0x00BBGGRR)."""
    try:
        for attr, value in ((20, 1), (35, int(BG[5:7] + BG[3:5] + BG[1:3], 16))):  # DARK_MODE, CAPTION_COLOR
            v = ctypes.c_int(value)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd_of(win), attr, ctypes.byref(v), ctypes.sizeof(v))
    except OSError:
        pass


class Btn(tk.Label):
    """Flat button with hover color (tk.Button can't show hover colors on Windows)."""
    KINDS = {"primary": (ACCENT, ACCENT_HI, "#ffffff"), "ghost": (FIELD, BORDER, TEXT)}

    def __init__(self, parent, text, command, kind="ghost", **kw):
        bg, hover, fg = self.KINDS[kind]
        opts = dict(padx=14, pady=7, font=(FONT, 10, "bold" if kind == "primary" else "normal"))
        opts.update(kw)
        super().__init__(parent, text=text, bg=bg, fg=fg, cursor="hand2", **opts)
        self.bind("<Enter>", lambda _: self.configure(bg=hover))
        self.bind("<Leave>", lambda _: self.configure(bg=bg))
        self.bind("<ButtonRelease-1>", lambda e: self.winfo_containing(e.x_root, e.y_root) is self and command())


class Toggle(tk.Canvas):
    """Pill on/off switch bound to a BooleanVar."""

    def __init__(self, parent, var):
        s = parent.winfo_fpixels("1i") / 96
        super().__init__(parent, width=int(40 * s), height=int(22 * s), bg=parent["bg"], highlightthickness=0,
                         cursor="hand2")
        self.s, self.var = s, var
        self.bind("<Button-1>", lambda _: var.set(not var.get()))
        var.trace_add("write", self.draw)
        self.draw()

    def draw(self, *_):
        s, on = self.s, self.var.get()
        c = ACCENT if on else BORDER
        self.delete("all")
        self.create_oval(0, 0, 22 * s, 22 * s, fill=c, width=0)
        self.create_oval(18 * s, 0, 40 * s, 22 * s, fill=c, width=0)
        self.create_rectangle(11 * s, 0, 29 * s, 22 * s, fill=c, width=0)
        x = (21 if on else 3) * s
        self.create_oval(x, 3 * s, x + 16 * s, 19 * s, fill="#ffffff", width=0)


class Overlay(tk.Toplevel):
    """Click-through outline around the watched area while running. Excluded from capture, so neither the
    watcher nor a stream ever sees it."""
    KEY, PAD, LABEL = "#010203", 4, 24

    def __init__(self, master):
        super().__init__(master)
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-transparentcolor", self.KEY)
        self.canvas = tk.Canvas(self, bg=self.KEY, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.rect = self.canvas.create_rectangle(0, 0, 0, 0, width=3)
        self.text = self.canvas.create_text(0, 0, anchor="w", font=(FONT, 12, "bold"))
        self.region, self.look = None, None
        click_through(self)

    def show_on(self, region):
        p, lab = self.PAD, self.LABEL
        above = region["top"] - p - lab >= user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
        w, h = region["width"] + 2 * p, region["height"] + 2 * p + lab
        self.geometry(f"{w}x{h}+{region['left'] - p}+{region['top'] - p - (lab if above else 0)}")
        top = lab if above else 0
        self.canvas.coords(self.rect, 1, top + 1, w - 2, top + h - lab - 2)
        self.canvas.coords(self.text, 2, lab // 2 if above else h - lab // 2)
        self.region, self.look = dict(region), None
        before = user32.GetForegroundWindow()
        self.deiconify()
        click_through(self)  # Tk may rebuild the window frame when mapping it, so (re)apply styles now
        hide_from_capture(self)
        if before and user32.GetForegroundWindow() == hwnd_of(self):
            focus_window(before)  # showing the outline must never take the keyboard from the game

    def set_state(self, color, text):
        if (color, text) != self.look:
            self.look = (color, text)
            self.canvas.itemconfigure(self.rect, outline=color)
            self.canvas.itemconfigure(self.text, fill=color, text=text)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        setup_logging()
        self.cfg = load_config()
        log.setLevel(logging.DEBUG if self.cfg["verbose"] else logging.INFO)
        self.title("Auto E")
        self.s = s = self.winfo_fpixels("1i") / 96
        self.geometry(f"{int(1060 * s)}x{int(720 * s)}")
        self.minsize(int(960 * s), int(660 * s))
        self.configure(bg=BG)
        self.setup_style()
        self.watcher = Watcher(self.cfg)
        self.watcher.start()
        self.auto = FullAuto(self.cfg, self.watcher)
        self.overlay = Overlay(self)
        self.views, self.vars, self.nav, self.pages, self._shown = {}, {}, {}, {}, {}
        self.picking, self.hotkey_down, self.scale, self.page = None, False, 1.0, None
        self.auto_hidden, self.unlock_job, self.ui_ms, self.slow = False, None, 0.0, 0
        self.log_serial, self.log_filter_shown = 0, None

        side = tk.Frame(self, bg=PANEL, padx=14, pady=22, width=int(230 * s))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)  # fixed width: changing status text never reflows the window
        brand = tk.Frame(side, bg=PANEL)
        brand.pack(anchor="w", padx=6)
        badge = tk.Label(brand, text="E", bg=ACCENT, fg="#ffffff", font=(FONT, 14, "bold"), width=2)
        badge.pack(side="left")
        logo = tk.Label(brand, text="Auto E", bg=PANEL, fg=TEXT, font=(FONT, 18, "bold"))
        logo.pack(side="left", padx=(10, 0))
        for w in (badge, logo):
            w.bind("<ButtonPress-1>", self.logo_down)
            w.bind("<ButtonRelease-1>", self.logo_up)
        self.subtitle = tk.Label(side, text="timing bar helper", bg=PANEL, fg=MUTED, font=(FONT, 9))
        self.subtitle.pack(anchor="w", padx=6, pady=(4, 26))
        tk.Label(side, text="MENU", bg=PANEL, fg=DIM, font=(FONT, 8, "bold")).pack(anchor="w", padx=8, pady=(0, 6))
        tk.Frame(self, bg=BORDER, width=1).pack(side="left", fill="y")
        body = tk.Frame(self, bg=BG, padx=32, pady=26)
        body.pack(side="left", fill="both", expand=True)

        for name, build in [("Skill bar", self.page_skill), ("Custom", self.page_custom),
                            ("Full auto", self.page_auto), ("Settings", self.page_settings),
                            ("Stats", self.page_stats), ("Help", self.page_help), ("Developer", self.page_dev)]:
            page = tk.Frame(body, bg=BG)
            page.place(relwidth=1, relheight=1)
            build(page)
            self.pages[name] = page
            item = tk.Frame(side, bg=PANEL, cursor="hand2")
            bar = tk.Frame(item, bg=PANEL, width=3)
            bar.pack(side="left", fill="y")
            text = tk.Label(item, text=name, bg=PANEL, fg=MUTED, font=(FONT, 11), anchor="w", padx=14, pady=8)
            text.pack(side="left", fill="x", expand=True)
            for w in (item, text):
                w.bind("<Button-1>", lambda _, n=name: self.show(n))
            text.bind("<Enter>", lambda _, n=name: self.paint_nav(n, True))
            text.bind("<Leave>", lambda _, n=name: self.paint_nav(n))
            self.nav[name] = (item, bar, text)
            if name != "Developer" or self.cfg["dev"]:
                item.pack(fill="x", pady=2)

        self.state_lbl = tk.Label(side, bg=PANEL, font=(FONT, 10, "bold"), anchor="w")
        self.state_lbl.pack(side="bottom", fill="x", padx=6)
        self.big_btn = Btn(side, "", self.toggle, "primary", pady=11)
        self.big_btn.pack(side="bottom", fill="x", pady=10)

        self.show("Skill bar")
        self.after(100, self.apply_window_settings)
        self.after(400, unlock_foreground)  # our window has focus now, which is when Windows accepts it
        log.info(f"Auto E started (admin: {bool(ctypes.windll.shell32.IsUserAnAdmin())})")
        self.tick()

    def setup_style(self):
        """Only the combobox stays ttk; clam is a plain drawn theme, cheap to redraw."""
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", background=CARD, foreground=TEXT, fieldbackground=FIELD, bordercolor=BORDER,
                     lightcolor=FIELD, darkcolor=BORDER, troughcolor=CARD, selectbackground=FIELD,
                     selectforeground=TEXT, font=(FONT, 10))
        st.map("TCombobox", fieldbackground=[("readonly", FIELD)], selectbackground=[("readonly", FIELD)],
               selectforeground=[("readonly", TEXT)])
        for k, v in {"background": FIELD, "foreground": TEXT, "selectBackground": ACCENT,
                     "selectForeground": "#ffffff", "borderWidth": 0, "font": f"{{{FONT}}} 10"}.items():
            self.option_add(f"*TCombobox*Listbox.{k}", v)

    def report_callback_exception(self, *exc):
        log.error("UI error", exc_info=exc)

    # ---------- building blocks ----------
    def header(self, page, title, text):
        tk.Label(page, text=title, bg=BG, fg=TEXT, font=(FONT, 22, "bold")).pack(anchor="w")
        tk.Label(page, text=text, bg=BG, fg=MUTED, font=(FONT, 10), wraplength=int(720 * self.s),
                 justify="left").pack(anchor="w", pady=(2, 18))

    def card(self, parent, **pack):
        f = tk.Frame(parent, bg=CARD, padx=18, pady=14, highlightthickness=1, highlightbackground=BORDER,
                     highlightcolor=BORDER)
        if pack:
            f.pack(**pack)
        return f

    def tabs(self, parent, names, expand=False):
        bar = tk.Frame(parent, bg=BG)
        bar.pack(fill="x", pady=(0, 10))
        stack = tk.Frame(parent, bg=BG)
        stack.pack(fill="both", expand=expand)
        frames, btns = {n: self.card(stack) for n in names}, {}

        def pick(name):
            for n, f in frames.items():
                f.pack(fill="both", expand=True) if n == name else f.pack_forget()
                btns[n].configure(bg=FIELD if n == name else BG, fg=TEXT if n == name else MUTED)

        for n in names:
            btns[n] = tk.Label(bar, text=n, font=(FONT, 10, "bold"), padx=16, pady=6, cursor="hand2")
            btns[n].pack(side="left", padx=(0, 6))
            btns[n].bind("<Button-1>", lambda _, n=n: pick(n))
        pick(names[0])
        return frames

    # ---------- pages ----------
    def mode_page(self, page, mode, title, text, extras=None):
        self.header(page, title, text)
        row = self.card(page, fill="x")
        Btn(row, "Select region", lambda: self.select_region(mode)).pack(side="left")
        region = tk.Label(row, bg=CARD, fg=MUTED, font=(FONT, 10))
        region.pack(side="left", padx=14)
        if mode == "skill":
            tk.Label(row, text="Key", bg=CARD, fg=MUTED, font=(FONT, 10)).pack(side="left", padx=(6, 8))
            self.key_var = tk.StringVar(value=self.cfg["skill"]["key"])
            self.key_var.trace_add("write", lambda *_: (self.cfg["skill"].update(key=self.key_var.get()),
                                                        save_config(self.cfg)))
            ttk.Combobox(row, textvariable=self.key_var, state="readonly", width=7, values=KEYS).pack(side="left")
        start = Btn(row, "", self.toggle, "primary", width=14)
        start.pack(side="right")
        Btn(row, "Test key", lambda: self.test_key(mode)).pack(side="right", padx=8)
        if extras:
            extras(page)
        live = self.card(page, fill="x", pady=14)
        top = tk.Frame(live, bg=CARD)
        top.pack(fill="x", pady=(0, 10))
        tk.Label(top, text="LIVE VIEW", bg=CARD, fg=MUTED, font=(FONT, 8, "bold")).pack(side="left")
        for label, color in (("Line", ACCENT), ("Missed", RED), ("Pressed", GREEN), ("Zone", YELLOW)):
            tk.Label(top, text=label, bg=CARD, fg=MUTED, font=(FONT, 9)).pack(side="right", padx=(0, 14))
            tk.Label(top, text="●", bg=CARD, fg=color, font=(FONT, 9)).pack(side="right", padx=(0, 4))
        holder = tk.Frame(live, bg="#07090d", height=int(260 * self.s))
        holder.pack(fill="x")
        holder.pack_propagate(False)  # fixed box: a new frame size never makes the page jump
        preview = tk.Label(holder, text="No region selected", bg="#07090d", fg=DIM, font=(FONT, 10))
        preview.pack(fill="both", expand=True)
        preview.bind("<Button-1>", self.on_preview_click)
        status = tk.Label(page, bg=BG, fg=TEXT, font=(FONT, 13, "bold"), anchor="w")
        status.pack(fill="x")
        self.views[mode] = {"region": region, "start": start, "preview": preview, "status": status, "photo": None}

    def page_skill(self, page):
        self.mode_page(page, "skill", "Skill bar",
                       "For plain see-through bars with no coloured target: a white letter marks the spot and a "
                       "white line slides along the bar. Select a region tightly around the bar. The letter is "
                       "pressed as the line reaches it; if that's early or late, adjust Press delay in Settings.")

    def page_custom(self, page):
        def extras(p):
            f = self.card(p, fill="x", pady=(14, 0))
            self.swatches = {}
            for part in ("box", "line"):
                Btn(f, f"Pick {part} color", lambda x=part: self.start_pick(x)).pack(side="left")
                self.swatches[part] = tk.Label(f, width=3, bd=0, highlightthickness=1, highlightbackground=BORDER)
                self.swatches[part].pack(side="left", padx=(8, 20), ipady=6)
            tk.Label(f, text="Tolerance", bg=CARD, fg=MUTED, font=(FONT, 10)).pack(side="left")
            self.tol_var = tk.IntVar(value=self.cfg["custom"]["tolerance"])
            tk.Scale(f, from_=20, to=120, orient="horizontal", variable=self.tol_var, length=int(150 * self.s),
                     showvalue=False, bg=ACCENT, activebackground=ACCENT_HI, troughcolor=FIELD,
                     highlightthickness=0, bd=0, sliderrelief="flat", width=int(8 * self.s),
                     sliderlength=int(16 * self.s),
                     command=lambda v: self.set_tolerance(int(float(v)))).pack(side="left", padx=8)
            Btn(f, "Reset", self.reset_custom_colors).pack(side="right")
            self.update_swatches()

        self.mode_page(page, "custom", "Custom",
                       "For other bars. Select the region, then use Pick box color / Pick line color and "
                       "click that part in the live view.", extras)

    def page_auto(self, page):
        self.header(page, "Full auto", "Runs a cycle of key presses on its own, top to bottom then round "
                                       "again, using the region of whichever mode you picked (Skill bar or "
                                       "Custom). Start it with the game already open.")
        row = self.card(page, fill="x")
        Btn(row, "Select region", lambda: self.select_region(self.watcher.mode)).pack(side="left")
        self.auto_region = tk.Label(row, bg=CARD, fg=MUTED, font=(FONT, 10))
        self.auto_region.pack(side="left", padx=14)
        self.auto_btn = Btn(row, "Start full auto", self.toggle_auto, "primary", width=16)
        self.auto_btn.pack(side="right")

        table = self.card(page, fill="x", pady=14)
        for i, (name, width) in enumerate((("ON", 4), ("KEY OR TEXT TO TYPE", 26), ("THEN WAIT", 12),
                                           ("WATCH REGION", 12))):
            tk.Label(table, text=name, bg=CARD, fg=DIM, font=(FONT, 8, "bold"), width=width, anchor="w").grid(
                row=0, column=i, sticky="w", padx=(0, 16), pady=(0, 6))
        self.step_vars = []
        for i, step in enumerate(self.cfg["auto"]):
            on = tk.BooleanVar(value=bool(step.get("on")))
            key = tk.StringVar(value=str(step.get("key", "")))
            wait = tk.StringVar(value=str(step.get("wait", "0")))
            watch = tk.BooleanVar(value=bool(step.get("watch", True)))
            Toggle(table, on).grid(row=i + 1, column=0, sticky="w", pady=2)
            for col, (var, width) in enumerate(((key, 24), (wait, 8)), start=1):
                tk.Entry(table, textvariable=var, width=width, bg=FIELD, fg=TEXT, insertbackground=TEXT,
                         relief="flat", bd=0, highlightthickness=1, highlightbackground=BORDER,
                         highlightcolor=ACCENT, font=(FONT, 10)).grid(row=i + 1, column=col, sticky="w",
                                                                      padx=(0, 16), ipady=4)
            Toggle(table, watch).grid(row=i + 1, column=3, sticky="w", pady=2)
            for v in (on, key, wait, watch):
                v.trace_add("write", self.save_steps)
            self.step_vars.append((on, key, wait, watch))

        tk.Label(page, bg=BG, fg=MUTED, justify="left", font=(FONT, 9), wraplength=int(720 * self.s), text=(
            "Key or text:  one key (E, 1, X, Space, Enter, Backspace, Esc, Tab), or a whole line like "
            "e yoga, which is typed out and sent with Enter.\n"
            "Then wait:  bar waits until the bar's key has been pressed, a number waits that many minutes, "
            "0 goes straight on to the next step.\n"
            "Watch region:  off for a step that runs while no bar is on screen, such as an emote -- nothing "
            "is pressed and the outline goes away until a step with it back on.\n"
            "Switch a step off to skip it: turning off the emote's rows leaves the cycle at E, the bar's key "
            "and 1.")).pack(anchor="w")
        self.auto_lbl = tk.Label(page, bg=BG, fg=MUTED, font=(FONT, 13, "bold"), anchor="w")
        self.auto_lbl.pack(fill="x", pady=(12, 0))

    def save_steps(self, *_):
        try:
            self.cfg["auto"] = [{"on": o.get(), "key": k.get(), "wait": w.get(), "watch": c.get()}
                                for o, k, w, c in self.step_vars]
        except tk.TclError:
            return  # half-typed value
        save_config(self.cfg)

    def toggle_auto(self):
        if self.auto.on:
            self.auto.stop()
            self.overlay.withdraw()
            if self.auto_hidden:
                self.auto_hidden = False
                self.deiconify()
            return
        unlock_foreground()
        if not self.auto.start():
            self.show(PAGE_OF_MODE[self.watcher.mode])
            return
        if self.cfg["outline"]:
            self.overlay.show_on(self.cfg[self.watcher.mode]["region"])
        if self.cfg["minimize"]:
            self.auto_hidden = True
            self.hide_main()
            self.update()
        if self.auto.w.target:
            focus_window(self.auto.w.target)  # minimizing hands focus to "the next window", possibly our outline

    def setting_rows(self, parent, rows):
        for i, (key, label, kind, hint) in enumerate(rows):
            tk.Label(parent, text=label, bg=CARD, fg=TEXT, font=(FONT, 10)).grid(
                row=i, column=0, sticky="w", pady=7, padx=(0, 24))
            if isinstance(kind, list):
                w = ttk.Combobox(parent, textvariable=self.var(key, tk.StringVar), state="readonly", width=14,
                                 values=kind)
            elif kind == "switch":
                w = Toggle(parent, self.var(key, tk.BooleanVar))
            else:
                w = tk.Spinbox(parent, from_=kind[0], to=kind[1], textvariable=self.var(key, tk.IntVar), width=9,
                               bg=FIELD, fg=TEXT, buttonbackground=FIELD, insertbackground=TEXT, relief="flat",
                               bd=0, highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
                               selectbackground=ACCENT, font=(FONT, 10))
            w.grid(row=i, column=1, sticky="w")
            tk.Label(parent, text=hint, bg=CARD, fg=MUTED, font=(FONT, 9), wraplength=int(380 * self.s),
                     justify="left").grid(row=i, column=2, sticky="w", padx=24)

    def page_settings(self, page):
        self.header(page, "Settings", "Saved automatically.")
        groups = {
            "Pressing": [
                ("hotkey", "Start / stop hotkey", [f"F{n}" for n in range(1, 13)], "Works while the game is focused."),
                ("trigger", "Press when", TRIGGERS, "Letter appears = press as soon as a letter is read."),
                ("margin", "Trigger zone margin %", (0, 45),
                 "Presses aim at the center of the letter/box. Higher = never press near its edges."),
                ("aim_ms", "Key lag compensation (ms)", (0, 150),
                 "Presses this much earlier, for the time until the game reads the key. Presses late: raise it."),
                ("delay", "Press delay (ms)", (0, 1000), "Presses early: raise this (or lower lag compensation)."),
                ("hold", "Key hold (ms)", (10, 500), "Raise if the game misses presses (browser games: 50+)."),
                ("cooldown", "Cooldown (ms)", (50, 3000), "Minimum time between presses."),
                ("retries", "Re-press if not taken", (0, 5),
                 "Press when = Letter appears: presses again if the letter is still showing after the wait below. 0 = off."),
                ("retry_ms", "Re-press wait (ms)", (80, 2000),
                 "Press when = Letter appears: how long before pressing again."),
                ("jiggle", "Move the mouse now and then", "switch",
                 "Servers that kick you for being AFK watch the mouse. Nudges it a few pixels and straight "
                 "back, only while running, only while the game is in front and never while a bar is up."),
                ("jiggle_secs", "Seconds between mouse nudges", (10, 600),
                 "Keep it well under the server's AFK timeout."),
                ("background", "Press without focusing the game", "switch",
                 "On: keys are posted to the game window and focus is never taken, so your mouse stays free "
                 "on another screen. Only works if the game reads window messages -- GTA V / FiveM read raw "
                 "input and ignore them. Off: the game is brought to the front for each key, which works "
                 "everywhere but grabs the pointer."),
            ],
            "Reading": [
                ("min_conf", "Min letter match %", (50, 99), "Won't press if the letter read is less sure than this."),
                ("digits", "Also read digits 0-9", "switch", "Only if the bar can show numbers."),
                ("max_fps", "Checks per second", (10, 240),
                 "Higher reacts faster but uses more CPU. Lower it if your game's FPS drops."),
            ],
            "Window": [
                ("minimize", "Minimize when started", "switch",
                 "Only the outline stays on screen. The hotkey stops and brings the app back."),
                ("outline", "Outline the watched area", "switch", "Click-through border, hidden from streams too."),
                ("hide_capture", "Hide from screen share / stream", "switch",
                 "Invisible to OBS, Discord, Teams, screenshots. Not to capture cards."),
                ("hide_taskbar", "Hide taskbar button", "switch", "Use the hotkey or Alt+Tab to find the window."),
                ("topmost", "Always on top", "switch", ""),
            ],
        }
        frames = self.tabs(page, list(groups))
        for tab, rows in groups.items():
            self.setting_rows(frames[tab], rows)
        self.capture_note = tk.Label(page, bg=BG, fg=AMBER, font=(FONT, 10))
        self.capture_note.pack(anchor="w", pady=6)
        Btn(page, "Reset to defaults", self.reset_settings).pack(anchor="w")

    def page_stats(self, page):
        self.header(page, "Stats", "Key presses this session.")
        row = tk.Frame(page, bg=BG)
        row.pack(fill="x")
        self.stat_lbls = {}
        for name, color in (("Pressed", GREEN), ("Missed", RED)):
            c = self.card(row, side="left", fill="x", expand=True, padx=(0, 12) if name == "Pressed" else 0)
            tk.Label(c, text=name.upper(), bg=CARD, fg=MUTED, font=(FONT, 8, "bold")).pack(anchor="w")
            self.stat_lbls[name] = tk.Label(c, bg=CARD, fg=color, font=(FONT, 28, "bold"))
            self.stat_lbls[name].pack(anchor="w")
        self.letters_lbl = tk.Label(page, bg=BG, fg=MUTED, font=(FONT, 11))
        self.letters_lbl.pack(anchor="w", pady=(12, 8))
        Btn(page, "Clear", self.clear_stats).pack(side="bottom", anchor="w", pady=(12, 0))
        box = self.card(page, fill="both", expand=True)
        self.log_box = tk.Listbox(box, bg=CARD, fg=TEXT, bd=0, highlightthickness=0, font=("Consolas", 10),
                                  activestyle="none", selectbackground=FIELD, selectforeground=TEXT)
        self.log_box.pack(fill="both", expand=True)

    def page_help(self, page):
        self.header(page, "Help", "How to use Auto E.")
        c = self.card(page, fill="x")
        tk.Label(c, bg=CARD, fg=TEXT, justify="left", wraplength=int(700 * self.s), font=(FONT, 10), text=(
            "1.  Open Skill bar (plain see-through bar with a letter and a line) or Custom.\n"
            "2.  Select region: drag a box tightly around the whole bar.\n"
            "3.  Press Start or the hotkey (F8 by default). The app minimizes and outlines the area.\n"
            "4.  When the line reaches the letter, it is pressed in the game.\n"
            "5.  Press the hotkey again to stop; the app comes back.\n\n"
            "Colors:  yellow = watching the zone,  green = key pressed,  red = missed.\n\n"
            "Game not focused?  Keep Press without focusing the game on: keys go straight to the game window "
            "while you use other windows. The bar must stay visible on screen (not minimized or covered). "
            "If a game ignores background keys, turn it off so the game is brought to the front instead.\n"
            "Full auto  runs a cycle of key presses for you, over and over: by default E, the bar\'s key, 1, "
            "five minutes, T, \"e yoga\", Enter, ten minutes, X, and round again. Every step is yours to "
            "change on that page -- which key, how long to wait after it, whether the region is watched during "
            "that wait, and whether the step runs at all. The hotkey stops it.\n"
            "Kicked for being AFK?  Move the mouse now and then, in Settings, nudges the pointer a few pixels "
            "and back every couple of minutes so the server sees mouse input. It only does it while running, "
            "while the game is the window in front, and never while a bar is up.\n"
            "Two screens, and you want your mouse free?  Run the game windowed borderless and turn on Press "
            "without focusing the game. Keys are then posted to the game window, focus is never taken and the "
            "pointer is never grabbed. The catch is that GTA V / FiveM read raw input and ignore posted keys, so "
            "for those the switch has to stay off and the game is pulled to the front for each key. Moving the "
            "mouse to the other screen does not cost you focus by itself -- only clicking there does.\n"
            "Minimized game?  Windows stops a minimized window drawing, so there is nothing on screen to read "
            "and no bar to time against. Exclusive fullscreen minimizes the game whenever you alt-tab, which is "
            "why windowed borderless is the one to use; plain windowed works just as well.\n"
            "Too early / too late?  Change Trigger zone margin or Press delay in Settings.\n"
            "Keys not reaching the game?  Click Test key: it focuses the game and presses the letter, and "
            "Stats shows which window got it. Click inside the game once first (browser games need the "
            "page focused). Still nothing: raise Key hold, or run AutoE.exe as administrator.\n"
            "Status says 'Line: not seen'?  Use Custom and pick the line color, or set Press when to "
            "'Letter appears'.\n"
            "Wrong letter?  Raise Min letter match % so it skips unsure reads, or pick the Key next to the "
            "region on Skill bar: that key is always pressed, the letter only shows where.\n\n"
            "Hidden from screen share: this window, the outline and the region picker are excluded from OBS, "
            "Discord, Teams, Windows screenshots and recordings. A hardware capture card or a phone camera "
            "pointed at the monitor still sees them.\n\n"
            "Some online games' anti-cheat flags simulated key presses. Use at your own risk.")).pack(anchor="w")
        self.credit(page, c)

    def credit(self, page, card):
        """Developer credit, kept only as cipher text (XOR 0x5A: not in the source or the exe as plain letters).
        Double-click it: a light traces around the whole help section, decoding a letter per stretch it covers.
        The name stays on screen for a few seconds while the trace is lit, then it's encrypted again."""
        code = (0x1B, 0x37, 0x28, 0x33, 0x2E, 0x3F, 0x29, 0x32)
        cipher = " ".join(f"{b:02X}" for b in code)
        label = tk.Label(page, text=f"Developed by  {cipher}", bg=BG, fg=DIM, font=("Consolas", 9), cursor="hand2")
        label.pack(anchor="w", pady=(14, 0))
        bars = [tk.Frame(page, bg=ACCENT_HI) for _ in range(4)]  # top, right, bottom, left edge of the card
        busy = []

        def done():
            for b in bars:
                b.place_forget()
            label.configure(text=f"Developed by  {cipher}", fg=DIM)
            busy.clear()

        def step(t0):
            p = min(1.0, (time.perf_counter() - t0) / 1.6)
            x, y, w, h = card.winfo_x(), card.winfo_y(), card.winfo_width(), card.winfo_height()
            t, run = max(2, int(2 * self.s)), p * 2 * (w + h)  # clockwise, what the light passed stays lit
            lit = (min(w, run), min(h, max(0, run - w)), min(w, max(0, run - w - h)), min(h, max(0, run - 2 * w - h)))
            spots = ((x, y, lit[0], t), (x + w - t, y, t, lit[1]),
                     (x + w - lit[2], y + h - t, lit[2], t), (x, y + h - lit[3], t, lit[3]))
            for bar, n, (bx, by, bw, bh) in zip(bars, lit, spots):
                bar.place(x=bx, y=by, width=bw, height=bh) if n >= 1 else bar.place_forget()
            # letter i decodes once the light has covered its share of the loop; the rest keep scrambling
            text = "".join(chr(b ^ 0x5A) if p >= (i + 1) / len(code) else random.choice("#%&@$*+=?")
                           for i, b in enumerate(code)) if p < 1 else bytes(b ^ 0x5A for b in code).decode()
            label.configure(text=f"Developed by  {text}", fg=ACCENT_HI if p >= 1 else MUTED)
            self.after(16, lambda: step(t0)) if p < 1 else self.after(3500, done)

        def start(_):
            if not busy:
                busy.append(1)
                step(time.perf_counter())

        label.bind("<Double-Button-1>", start)

    def page_dev(self, page):
        self.header(page, "Developer", "Logs, live internals and debug tools.")
        frames = self.tabs(page, ["Logs", "Live", "Tools"], expand=True)

        logs = frames["Logs"]
        bar = tk.Frame(logs, bg=CARD)
        bar.pack(fill="x")
        self.log_filter = tk.StringVar(value="All")
        ttk.Combobox(bar, textvariable=self.log_filter, state="readonly", width=12,
                     values=["All", "Info+", "Warnings+", "Errors"]).pack(side="left")
        self.counts_lbl = tk.Label(bar, bg=CARD, fg=MUTED, font=(FONT, 9))
        self.counts_lbl.pack(side="left", padx=14)
        for text, cmd in [("Clear", self.clear_logs), ("Copy", self.copy_logs),
                          ("Open folder", lambda: os.startfile(APP_DIR))]:
            Btn(bar, text, cmd, pady=4).pack(side="right", padx=3)
        holder = tk.Frame(logs, bg=CARD)
        holder.pack(fill="both", expand=True, pady=(10, 0))
        self.log_text = tk.Text(holder, bg="#0c0f15", fg="#d0d4de", bd=0, highlightthickness=0, wrap="none",
                                font=("Consolas", 10), insertbackground=TEXT, padx=8, pady=6)
        self.log_text.pack(side="left", fill="both", expand=True)
        for level, color in [("DEBUG", DIM), ("INFO", "#d0d4de"), ("WARNING", YELLOW),
                             ("ERROR", RED), ("CRITICAL", "#ff4d4d")]:
            self.log_text.tag_configure(level, foreground=color)

        live = frames["Live"]
        self.dev_vals = {}
        names = ["Detection fps", "Capture", "Detect", "UI refresh", "Mode / running", "Box / line", "Top matches",
                 "Pending re-press", "Window under area", "Foreground window", "Running as admin", "Letter shapes",
                 "Config"]
        for i, name in enumerate(names):
            tk.Label(live, text=name, bg=CARD, fg=MUTED, font=(FONT, 10)).grid(
                row=i, column=0, sticky="w", pady=3, padx=(0, 24))
            self.dev_vals[name] = tk.Label(live, bg=CARD, fg=TEXT, font=("Consolas", 10))
            self.dev_vals[name].grid(row=i, column=1, sticky="w")

        tools = frames["Tools"]
        opts = tk.Frame(tools, bg=CARD)
        opts.pack(anchor="w", fill="x")
        self.setting_rows(opts, [
            ("verbose", "Verbose logging", "switch", "Logs every letter read and box change."),
            ("dry_run", "Dry run", "switch", "Detect and log, but never send keys."),
            ("show_mask", "Show letter mask", "switch", "Paints the pixels read as the letter pink in the live view."),
        ])
        btns = tk.Frame(tools, bg=CARD)
        btns.pack(anchor="w", pady=16)
        for i, (text, cmd) in enumerate([
            ("Save debug snapshot", self.snapshot), ("Test key", lambda: self.test_key(self.watcher.mode)),
            ("Reload letter shapes", lambda: setattr(self.watcher, "digits", None)),
            ("Log a test error", lambda: log.error("Test error from the developer menu")),
            ("Reset ALL settings", self.reset_all), ("Turn off developer mode", self.lock_dev),
        ]):
            Btn(btns, text, cmd, width=22).grid(row=i // 2, column=i % 2, padx=4, pady=4, sticky="w")
        tk.Label(tools, bg=CARD, fg=MUTED, wraplength=int(640 * self.s), justify="left", font=(FONT, 9), text=(
            f"Snapshots and autoe.log are saved in {APP_DIR}. A snapshot saves the captured image, the annotated "
            "view, the letter mask and the detection state, handy for fixing a bar that isn't detected.")).pack(anchor="w")

    # ---------- developer mode ----------
    def logo_down(self, _):
        self.unlock_job = self.after(3000, self.unlock_dev)
        self.after(1000, lambda: self.unlock_job and self.subtitle.configure(text="keep holding..."))

    def logo_up(self, _):
        if self.unlock_job:
            self.after_cancel(self.unlock_job)
            self.unlock_job = None
        self.subtitle.configure(text="timing bar helper")

    def unlock_dev(self):
        self.unlock_job = None
        self.subtitle.configure(text="timing bar helper")
        if not self.cfg["dev"]:
            self.cfg["dev"] = True
            save_config(self.cfg)
            self.nav["Developer"][0].pack(fill="x", pady=2)
            log.info("Developer mode turned on")
        self.show("Developer")

    def lock_dev(self):
        for key in ("verbose", "dry_run", "show_mask"):
            self.vars[key].set(False)
        self.cfg["dev"] = False
        save_config(self.cfg)
        self.nav["Developer"][0].pack_forget()
        log.info("Developer mode turned off")
        self.show("Skill bar")

    def copy_logs(self):
        self.clipboard_clear()
        self.clipboard_append(self.log_text.get("1.0", "end"))

    def clear_logs(self):
        ring.records.clear()
        ring.counts.clear()
        self.log_text.delete("1.0", "end")

    def snapshot(self):
        w = self.watcher
        if w.raw is None:
            log.warning("Snapshot: nothing captured yet, select a region first")
            return
        d = APP_DIR / "snapshots" / time.strftime("%Y%m%d-%H%M%S")
        d.mkdir(parents=True, exist_ok=True)
        write_image(d / "captured.png", w.raw)
        write_image(d / "annotated.png", w.frame)
        if w.mask is not None:
            write_image(d / "letter_mask.png", w.mask.astype(np.uint8) * 255)
        info = {"mode": w.mode, "armed": w.armed, "status": w.status, "box": w.box, "line": w.line,
                "top_matches": w.top, "perf": w.perf, "config": self.cfg}
        (d / "info.json").write_text(json.dumps(info, indent=2, default=int))
        log.info(f"Snapshot saved to {d}")

    def reset_all(self):
        keep_dev = self.cfg["dev"]
        self.cfg.clear()
        self.cfg.update(deepcopy(DEFAULTS), dev=keep_dev)
        for key, v in self.vars.items():
            v.set(self.cfg[key])
        self.tol_var.set(self.cfg["custom"]["tolerance"])
        self.key_var.set(self.cfg["skill"]["key"])
        self.update_swatches()
        save_config(self.cfg)
        log.info("All settings reset")

    # ---------- actions ----------
    def var(self, key, kind):
        v = kind(value=self.cfg[key])

        def changed(*_):
            try:
                self.cfg[key] = v.get()
            except (tk.TclError, ValueError):
                return  # half-typed number
            save_config(self.cfg)
            if key in ("hide_capture", "hide_taskbar", "topmost"):
                self.apply_window_settings(refresh_taskbar=key == "hide_taskbar")
            if key == "verbose":
                log.setLevel(logging.DEBUG if v.get() else logging.INFO)

        v.trace_add("write", changed)
        self.vars[key] = v
        return v

    def reset_settings(self):
        for key, v in self.vars.items():
            if key not in ("verbose", "dry_run", "show_mask"):
                v.set(DEFAULTS[key])

    def paint_nav(self, name, hover=False):
        item, bar, text = self.nav[name]
        active = name == self.page
        bg = FIELD if active else CARD if hover else PANEL
        item.configure(bg=bg)
        text.configure(bg=bg, fg=TEXT if active or hover else MUTED)
        bar.configure(bg=ACCENT if active else bg)

    def show(self, name):
        self.pages[name].tkraise()
        self.page = name
        for n in self.nav:
            self.paint_nav(n)
        if name in MODE_OF_PAGE:
            self.watcher.mode = MODE_OF_PAGE[name]
        if name == "Developer":
            self.log_filter_shown = None  # force a full log redraw

    def hide_main(self):
        self.withdraw() if self.cfg["hide_taskbar"] else self.iconify()

    def toggle(self):
        if self.auto.on:
            return self.toggle_auto()
        w = self.watcher
        region = self.cfg[w.mode]["region"]
        if not region:
            w.armed = False
            self.show(PAGE_OF_MODE[w.mode])
            log.warning("Can't start: no region selected")
            return
        w.armed = not w.armed
        if w.armed:
            unlock_foreground()
        w.target = window_under(region) if w.armed else None
        log.info(f"{'Started' if w.armed else 'Stopped'} ({w.mode})"
                 + (f" on '{title_of(w.target)}'" if w.target else ""))
        if not w.armed:
            self.overlay.withdraw()
            if self.auto_hidden:
                self.auto_hidden = False
                self.deiconify()
            return
        if self.cfg["outline"]:
            self.overlay.show_on(region)
        # hand the keyboard to the game: easiest while this app is still the foreground window
        # (background mode never needs focus, so it leaves whatever window you're using alone)
        focus = not self.cfg["background"]
        target = window_under(region) if focus and is_ours(user32.GetForegroundWindow()) else None
        if target and not focus_window(target):
            log.warning(f"Couldn't focus '{title_of(target)}', click the game once")
        if self.cfg["minimize"]:
            self.auto_hidden = True
            self.hide_main()
            self.update()
            # Windows activates "the next window" after a minimize, which can be our own outline
            if focus and is_ours(user32.GetForegroundWindow()) and (target := window_under(region)):
                focus_window(target)

    def test_key(self, mode):
        region = self.cfg[mode]["region"]
        if not region:
            log.warning("Test key: no region selected")
            return
        key = self.cfg[mode].get("key", "Auto")
        self.watcher.queue_press(key if key != "Auto" else self.watcher.letter or "E", region, "TEST")
        self.show("Stats")

    def apply_window_settings(self, refresh_taskbar=False):
        self.attributes("-topmost", self.cfg["topmost"])
        self.attributes("-toolwindow", self.cfg["hide_taskbar"])
        if refresh_taskbar and self.state() != "withdrawn":
            self.withdraw()
            self.deiconify()
        dark_titlebar(self)
        ok = hide_from_capture(self, self.cfg["hide_capture"])
        self.capture_note.configure(text="" if ok else "Could not hide from capture (needs Windows 10 2004 or newer).")

    def select_region(self, mode):
        self.withdraw()
        self.update()
        time.sleep(0.25)
        with mss.mss() as sct:
            mon = sct.monitors[0]
            shot = sct.grab(mon)
        top = tk.Toplevel(self)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.geometry(f"{mon['width']}x{mon['height']}+{mon['left']}+{mon['top']}")
        if self.cfg["hide_capture"]:
            hide_from_capture(top)
        canvas = tk.Canvas(top, highlightthickness=0, cursor="cross")
        canvas.pack(fill="both", expand=True)
        canvas.photo = ImageTk.PhotoImage(Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX"))
        canvas.create_image(0, 0, image=canvas.photo, anchor="nw")
        canvas.create_text(mon["width"] // 2, 40, text="Drag a box tightly around the bar   ·   Esc to cancel",
                           fill="white", font=(FONT, 18, "bold"))
        rect = canvas.create_rectangle(0, 0, 0, 0, outline=YELLOW, width=3)
        start = {}

        def close(_=None):
            top.destroy()
            self.deiconify()
            self.apply_window_settings()

        def up(e):
            if not start:
                return
            x0, x1 = sorted((start["x"], e.x))
            y0, y1 = sorted((start["y"], e.y))
            if x1 - x0 > 10 and y1 - y0 > 5:
                self.cfg[mode]["region"] = {"left": mon["left"] + x0, "top": mon["top"] + y0,
                                            "width": x1 - x0, "height": y1 - y0}
                save_config(self.cfg)
                log.info(f"{mode} region set to {self.cfg[mode]['region']}")
            close()

        canvas.bind("<ButtonPress-1>", lambda e: start.update(x=e.x, y=e.y))
        canvas.bind("<B1-Motion>", lambda e: start and canvas.coords(rect, start["x"], start["y"], e.x, e.y))
        canvas.bind("<ButtonRelease-1>", up)
        top.bind("<Escape>", close)
        top.focus_force()

    def start_pick(self, part):
        self.picking = part

    def on_preview_click(self, e):
        raw, photo = self.watcher.raw, self.views[self.watcher.mode]["photo"]
        if not self.picking or raw is None or photo is None or self.watcher.mode != "custom":
            return
        # the image is centered in the preview label
        x = (e.x - (e.widget.winfo_width() - photo.width()) / 2) / self.scale
        y = (e.y - (e.widget.winfo_height() - photo.height()) / 2) / self.scale
        x, y = min(raw.shape[1] - 1, max(0, int(x))), min(raw.shape[0] - 1, max(0, int(y)))
        c = self.cfg["custom"]
        c[self.picking + "_pixel"] = [int(v) for v in raw[y, x]]
        c[self.picking] = det.spec_from_pixel(c[self.picking + "_pixel"], c["tolerance"])
        log.info(f"Custom {self.picking} color picked: BGR {c[self.picking + '_pixel']}")
        self.picking = None
        self.update_swatches()
        save_config(self.cfg)

    def set_tolerance(self, tol):
        c = self.cfg["custom"]
        c["tolerance"] = tol
        for part in ("box", "line"):
            if c[part + "_pixel"]:
                c[part] = det.spec_from_pixel(c[part + "_pixel"], tol)
        save_config(self.cfg)

    def reset_custom_colors(self):
        self.cfg["custom"].update(box=det.GYM["box"], line=det.GYM["line"], box_pixel=None, line_pixel=None)
        self.update_swatches()
        save_config(self.cfg)

    def update_swatches(self):
        for part, default in (("box", "#5a3c8c"), ("line", "#ffffff")):
            p = self.cfg["custom"][part + "_pixel"]
            self.swatches[part].configure(bg=f"#{p[2]:02x}{p[1]:02x}{p[0]:02x}" if p else default)

    def clear_stats(self):
        self.watcher.presses.clear()
        self.watcher.log.clear()
        self.watcher.misses = 0

    # ---------- refresh loop ----------
    def set(self, widget, **kw):
        """configure() only what changed: re-setting identical text still costs a Tk redraw."""
        cache = self._shown.setdefault(str(widget), {})
        changed = {k: v for k, v in kw.items() if cache.get(k) != v}
        if changed:
            widget.configure(**changed)
            cache.update(changed)

    def tick(self):
        t0 = time.perf_counter()
        try:
            self.refresh()
        except Exception:
            say_once("tick", logging.ERROR, "UI refresh error:\n" + traceback.format_exc().rstrip(), 10)
        self.ui_ms = 0.9 * self.ui_ms + 100 * (time.perf_counter() - t0)
        # poll faster than the watcher produces frames, so each new frame is shown as soon as it exists
        # (polling at the same ~30 fps as the watcher beats against it and drops/doubles frames = stutter)
        self.after(15, self.tick)

    def refresh(self):
        vk = 0x6F + int(self.cfg["hotkey"][1:])  # F1 = 0x70
        down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
        if down and not self.hotkey_down:
            self.toggle()
        self.hotkey_down = down

        w = self.watcher
        region = self.cfg[w.mode]["region"]
        if w.armed and self.cfg["outline"] and region:
            if self.overlay.region != region:
                self.overlay.show_on(region)
            look = w.look()
            dry = "  DRY RUN" if self.cfg["dev"] and self.cfg["dry_run"] else ""
            text = {"miss": "missed", "press": f"{w.last_press[1]} pressed"}.get(look, w.letter or "...")
            self.overlay.set_state(ZONE[look], f"Auto E  ·  {text}{dry}")
        elif self.overlay.region:
            self.overlay.withdraw()
            self.overlay.region = None

        if self.state() in ("iconic", "withdrawn"):
            return  # hidden: only hotkey and outline need updating ("zoomed" = maximized, still visible)
        label = f"{'Stop' if w.armed else 'Start'}  ({self.cfg['hotkey']})"
        self.set(self.big_btn, text=label)
        self.set(self.state_lbl, text=f"●  {'RUNNING' if w.armed else 'STOPPED'}  ·  {PAGE_OF_MODE[w.mode]}",
                 foreground=GREEN if w.armed else MUTED)

        if self.page in MODE_OF_PAGE:
            view = self.views[w.mode]
            self.set(view["start"], text=label)
            self.set(view["region"], text=f"{region['width']}×{region['height']} at ({region['left']}, "
                                          f"{region['top']})" if region else "No region selected")
            self.set(view["status"], text=f"Click the {self.picking} in the live view"
                     if self.picking and w.mode == "custom" else w.status)
            frame, fid = w.frame, w.frame_id
            if frame is None:
                self.set(view["preview"], image="", text="No region selected")
                view["photo"] = None
            elif fid != view.get("fid"):
                view["fid"] = fid
                pv = view["preview"]
                self.scale = min(max(80, pv.winfo_width() - 8) / frame.shape[1],
                                 max(40, pv.winfo_height() - 8) / frame.shape[0], 3)
                size = (max(1, int(frame.shape[1] * self.scale)), max(1, int(frame.shape[0] * self.scale)))
                im = Image.fromarray(cv2.cvtColor(cv2.resize(frame, size, interpolation=cv2.INTER_AREA),
                                                  cv2.COLOR_BGR2RGB))
                photo = view["photo"]
                if photo is None or (photo.width(), photo.height()) != size:
                    view["photo"] = photo = ImageTk.PhotoImage(im)
                    pv.configure(image=photo, text="")
                    self._shown.pop(str(pv), None)
                else:
                    photo.paste(im)  # reuse the Tk image instead of allocating a new one every frame

        elif self.page == "Full auto":
            a = self.auto
            left = max(0.0, a.until - time.time()) if a.on else 0.0
            self.set(self.auto_btn, text="Stop full auto" if a.on else "Start full auto")
            self.set(self.auto_region, text=f"{region['width']}×{region['height']} at ({region['left']}, "
                                            f"{region['top']})" if region else "No region selected")
            self.set(self.auto_lbl, text=f"{'RUNNING' if a.on else 'Idle'}  ·  {a.stage or '—'}"
                     + (f"  ·  {int(left) // 60}:{int(left) % 60:02d} left" if left else ""),
                     foreground=GREEN if a.on else MUTED)

        elif self.page == "Stats":
            self.set(self.stat_lbls["Pressed"], text=str(sum(w.presses.values())))
            self.set(self.stat_lbls["Missed"], text=str(w.misses))
            self.set(self.letters_lbl, text="   ".join(f"{k} × {n}" for k, n in w.presses.most_common()) or "—")
            log_lines = list(w.log)
            if self.log_box.size() != len(log_lines) or (log_lines and self.log_box.get(0) != log_lines[0]):
                self.log_box.delete(0, "end")
                self.log_box.insert("end", *log_lines)
                for i, line in enumerate(log_lines):
                    if "MISSED" in line:
                        self.log_box.itemconfigure(i, fg=RED)

        elif self.page == "Developer":
            self.refresh_dev()

    def refresh_dev(self):
        w = self.watcher
        c = ring.counts
        self.set(self.counts_lbl, text=f"Errors {c['ERROR'] + c['CRITICAL']}   ·   Warnings {c['WARNING']}   ·   "
                                       f"Info {c['INFO']}   ·   Debug {c['DEBUG']}")
        min_level = {"All": 0, "Info+": logging.INFO, "Warnings+": logging.WARNING, "Errors": logging.ERROR}[
            self.log_filter.get()]
        if self.log_filter_shown != self.log_filter.get():
            self.log_filter_shown, self.log_serial = self.log_filter.get(), 0
            self.log_text.delete("1.0", "end")
        new = [r for r in list(ring.records) if r.serial > self.log_serial]
        if new:
            at_bottom = self.log_text.yview()[1] > 0.999
            for r in new:
                if r.levelno >= min_level:
                    self.log_text.insert("end", ring.format(r) + "\n", r.levelname)
            self.log_serial = new[-1].serial
            if int(self.log_text.index("end-1c").split(".")[0]) > 3000:
                self.log_text.delete("1.0", "1000.0")
            if at_bottom:
                self.log_text.see("end")

        region = self.cfg[w.mode]["region"]
        under = window_under(region) if region else None
        fg = user32.GetForegroundWindow()
        vals = {
            "Detection fps": f"{w.perf['fps']:.0f}",
            "Capture": f"{w.perf['grab']:.1f} ms",
            "Detect": f"{w.perf['detect']:.1f} ms",
            "UI refresh": f"{self.ui_ms:.1f} ms",
            "Mode / running": f"{w.mode} / {w.armed}",
            "Box / line": f"{w.box[0] if w.box else None} / {w.line}",
            "Top matches": "  ".join(f"{ch} {s:.0%}" for ch, s in w.top) or "-",
            "Pending re-press": str(w.pending),
            "Window under area": title_of(under) if under else "-",
            "Foreground window": title_of(fg) if fg else "-",
            "Running as admin": str(bool(ctypes.windll.shell32.IsUserAnAdmin())),
            "Letter shapes": str(len(w.templates[0]) if w.templates else 0),
            "Config": str(CONFIG),
        }
        for name, value in vals.items():
            self.set(self.dev_vals[name], text=value)


if __name__ == "__main__":
    App().mainloop()
