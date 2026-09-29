"""Castika DeckShow: one file, three processes.

    python3 deckshow.py host                 what the login item runs: the microphone
                                             daemon (the only thing that opens the
                                             mic) and the Companion adapter in one
                                             process, two threads
    python3 deckshow.py daemon               the microphone daemon alone (debugging)
    python3 deckshow.py companion            the Companion adapter alone (debugging)
    python3 deckshow.py plugin <SDK args>    the Stream Deck plugin, started by the
                                             Stream Deck app (bin/plugin passes
                                             -port/-pluginUUID/-registerEvent/-info)

All three share the sections below (paths, idle and display watchers, audio
analysis, rendering, grid layout). Everything is relative to ROOT, the folder
this file sits in: logs/, .runtime/, streamdeck/<plugin>/ (manifest, fonts,
glyphs).
"""
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import argparse
import asyncio
import base64
import colorsys
import json
import logging
import os
import random
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import numpy as np
import sounddevice as sd
import websockets
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("deckshow")
RENDER_FPS = 10  # show frame rate (Stream Deck plugin and Companion adapter alike)


def _setup_logging(filename: str, tag: str):
    """Each process writes its own file under ROOT/logs; the terminal also gets a
    copy only when run by hand (the macOS applet and the Windows launcher only
    catch what escapes to stdout)."""
    path = ROOT / "logs" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s " + (tag + " " if tag else "") + "%(levelname)s %(message)s"
    handlers = [logging.FileHandler(path, encoding="utf-8")] + ([logging.StreamHandler()] if sys.stdout.isatty() else [])
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)


class _ThreadIs(logging.Filter):
    """Routes a record by the name of the thread that logged it (host mode: the
    daemon thread and the Companion thread keep separate log files)."""

    def __init__(self, name, invert=False):
        super().__init__()
        self.name, self.invert = name, invert

    def filter(self, record):
        return (record.threadName == self.name) != self.invert


def _setup_logging_host():
    (ROOT / "logs").mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(threadName)s %(levelname)s %(message)s"
    # The Companion adapter is the main thread ("COMPANION"); the daemon thread and
    # PortAudio's own callback threads (unnamed) all belong to the audio side.
    companion = logging.FileHandler(ROOT / "logs" / "companion.log", encoding="utf-8")
    companion.addFilter(_ThreadIs("COMPANION"))
    daemon = logging.FileHandler(ROOT / "logs" / "deckshow.log", encoding="utf-8")
    daemon.addFilter(_ThreadIs("COMPANION", invert=True))
    handlers = [daemon, companion] + ([logging.StreamHandler()] if sys.stdout.isatty() else [])
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)


# ============================================================================
# Per-user folders (macOS / Windows)
# ============================================================================
# Per-user folders that differ between macOS and Windows. Everything else in
# the package is relative to the install folder and needs no such switch.
#
# macOS:   ~/Library/Application Support/DeckShow      (fonts/, companion.json)
#          ~/Library/Application Support/companion     (Companion's own store)
# Windows: %APPDATA%\DeckShow
#          %APPDATA%\companion

APP_DIR_NAME = "DeckShow"


def _roaming_base() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    return Path.home() / "Library" / "Application Support"


def user_data_dir() -> Path:
    """Our per-user data (user fonts, Companion adapter settings)."""
    return _roaming_base() / APP_DIR_NAME


def companion_data_dir() -> Path:
    """Where Bitfocus Companion keeps its store (read-only for us)."""
    return _roaming_base() / "companion"


def reveal_in_file_manager(path: Path) -> None:
    """Open a folder in Finder / Explorer (used by the property inspector's
    "open fonts folder" button)."""
    import subprocess
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - opens the folder in Explorer
    else:
        subprocess.Popen(["open", str(path)])


# ============================================================================
# System idle time
# ============================================================================
# System-wide idle time: the signal we settled on for "start the show" is how
# long since any buttonboard/mouse input anywhere on the computer, not Stream
# Deck-specific input (the SDK gives us no way to observe button events on a profile
# we don't own). Host-agnostic: knows nothing about Stream Deck.
#
# macOS:   `ioreg -c IOHIDSystem` (HIDIdleTime), rather than a Quartz/PyObjC
#          binding, to avoid adding a dependency for one number.
# Windows: `GetLastInputInfo` from user32 through ctypes, compared with
#          `GetTickCount` (both 32-bit millisecond counters, so the difference is
#          taken modulo 2^32).

_IDLE_RE = re.compile(rb'"HIDIdleTime"\s*=\s*(\d+)')


_cg_idle = None  # CoreGraphics probe, loaded on first use


def _idle_seconds_macos() -> float:
    """CoreGraphics `CGEventSourceSecondsSinceLastEventType` (combined session
    state, any input event): a direct call costing microseconds and needing no
    permission. Falls back to `ioreg -c IOHIDSystem` (HIDIdleTime) if the
    library cannot be loaded; spawning ioreg every poll was measurable CPU while
    idle (2026-09-22), which is why it is no longer the first choice."""
    global _cg_idle
    if _cg_idle is None:
        try:
            import ctypes
            import ctypes.util
            cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics"))
            cg.CGEventSourceSecondsSinceLastEventType.argtypes = [ctypes.c_int, ctypes.c_uint32]
            cg.CGEventSourceSecondsSinceLastEventType.restype = ctypes.c_double
            kCGEventSourceStateCombinedSessionState = 0
            kCGAnyInputEventType = 0xFFFFFFFF  # ~0 in the SDK header
            _cg_idle = lambda: float(cg.CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateCombinedSessionState, kCGAnyInputEventType))
        except (OSError, AttributeError):
            _cg_idle = False
    if _cg_idle:
        return _cg_idle()
    out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, check=True).stdout
    m = _IDLE_RE.search(out)
    if not m:
        raise RuntimeError("HIDIdleTime not found in ioreg output")
    return int(m.group(1)) / 1_000_000_000  # nanoseconds -> seconds


def _idle_seconds_windows() -> float:
    import ctypes
    from ctypes import wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        raise RuntimeError("GetLastInputInfo failed")
    now = ctypes.windll.kernel32.GetTickCount()
    return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0


def get_idle_seconds() -> float:
    """Seconds since the last system-wide keyboard/mouse event. Raises
    RuntimeError when the OS gives no answer (unexpected on a real machine, but
    callers should treat a failure as "assume active" rather than crash the
    render/idle loop over it)."""
    if sys.platform == "win32":
        return _idle_seconds_windows()
    return _idle_seconds_macos()


def user_active_since(started_at) -> bool:
    """Has the keyboard or mouse been used since `started_at` (time.monotonic())?
    Any input resets the system's idle timer, so a timer that is younger than the
    show itself means someone touched the machine. An unreadable timer answers
    False: the safe failure mode is a show that keeps running, not one that stops
    for no reason."""
    if not started_at:
        return False
    try:
        idle_s = get_idle_seconds()
    except (RuntimeError, subprocess.CalledProcessError, OSError, AttributeError):
        return False
    return idle_s + ACTIVITY_MARGIN_S < time.monotonic() - started_at


class IdleWatcher:
    """Polls `get_idle_seconds()` on a slow cadence and fires callbacks on the
    OFF->ON / ON->OFF edges of "idle for >= threshold_s", not on every poll --
    callers (e.g. the Elgato adapter) drive one-shot actions (switch to the show
    profile, switch back) from those edges, not from a level they'd otherwise
    have to de-dupe themselves.
    """

    def __init__(self, threshold_s: float):
        self.threshold_s = threshold_s
        self.is_idle = False

    def poll(self) -> "str | None":
        """Call this periodically (e.g. every few seconds). Returns "became_idle",
        "became_active", or None if nothing changed since the last poll. Idle
        time can only be read as "assume active" on error, since the safe
        failure mode is a show that fails to start, not one that fails to stop."""
        try:
            idle_s = get_idle_seconds()
        except (RuntimeError, subprocess.CalledProcessError, OSError, AttributeError):
            idle_s = 0.0

        now_idle = idle_s >= self.threshold_s
        if now_idle and not self.is_idle:
            self.is_idle = True
            return "became_idle"
        if not now_idle and self.is_idle:
            self.is_idle = False
            return "became_active"
        return None


# ============================================================================
# Display sleep / wake
# ============================================================================
# Is the computer's display asleep? Asked of the OS directly, so an adapter can
# log display off/on and repaint after a wake. The Stream Deck app tells its
# plugin this by itself (deviceDidDisconnect/Connect around display sleep);
# Companion tells nobody, so the Companion adapter polls this.
#
# macOS: CoreGraphics `CGDisplayIsAsleep(CGMainDisplayID())` through ctypes --
# no permission, no subprocess, microseconds. Verified with `pmset
# displaysleepnow`: 1 while the display is off, 0 once it is back. (`ioreg`
# framebuffer power state stays 1 during display sleep, `pmset -g log` costs
# 0.25 s per read, and `log stream` shows nothing to a normal user -- none of
# those are used.)
#
# Windows: there is no "is the display off" query; the OS pushes the state.
# A background thread owns a hidden message-only window, registers for
# GUID_CONSOLE_DISPLAY_STATE with `PowerSettingRegisterNotification`, and keeps
# the last WM_POWERBROADCAST / PBT_POWERSETTINGCHANGE value (0 = off, 1 = on,
# 2 = dimmed). Until the first notification arrives the state is unknown (None)
# and the watcher stays quiet; the same if anything in the setup fails.

class DisplayWatcher:
    def __init__(self):
        self._probe = None
        self.state = None  # True = asleep, False = awake, None = unknown
        if sys.platform == "darwin":
            self._setup_macos()
        elif sys.platform == "win32":
            self._setup_windows()

    # -- macOS ----------------------------------------------------------------
    def _setup_macos(self):
        try:
            import ctypes
            import ctypes.util
            cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics"))
            cg.CGMainDisplayID.restype = ctypes.c_uint32
            cg.CGDisplayIsAsleep.argtypes = [ctypes.c_uint32]
            cg.CGDisplayIsAsleep.restype = ctypes.c_uint32
            self._probe = lambda: bool(cg.CGDisplayIsAsleep(cg.CGMainDisplayID()))
        except (OSError, AttributeError) as e:
            log.warning("CoreGraphics display state unavailable: %s", e)

    # -- Windows --------------------------------------------------------------
    def _setup_windows(self):
        self._win_state = None  # last GUID_CONSOLE_DISPLAY_STATE value
        try:
            t = threading.Thread(target=self._windows_loop, name="display-watch", daemon=True)
            t.start()
            self._probe = lambda: None if self._win_state is None else (self._win_state == 0)
        except Exception as e:  # noqa: BLE001
            log.warning("Windows display state unavailable: %s", e)

    def _windows_loop(self):
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        # ctypes assumes int arguments and int results. On 64-bit Windows that
        # truncates handles and overflows on an LPARAM that carries a pointer,
        # so every call used here says what it takes and returns.
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.GetMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

        WM_POWERBROADCAST = 0x0218
        PBT_POWERSETTINGCHANGE = 0x8013
        DEVICE_NOTIFY_WINDOW_HANDLE = 0
        HWND_MESSAGE = wintypes.HWND(-3)

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]

        # GUID_CONSOLE_DISPLAY_STATE {6FE69556-704A-47A0-8F24-C28D936FDA47}
        console_display = GUID(0x6FE69556, 0x704A, 0x47A0,
                               (ctypes.c_ubyte * 8)(0x8F, 0x24, 0xC2, 0x8D, 0x93, 0x6F, 0xDA, 0x47))

        class POWERBROADCAST_SETTING(ctypes.Structure):
            _fields_ = [("PowerSetting", GUID), ("DataLength", wintypes.DWORD), ("Data", wintypes.DWORD)]

        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_POWERBROADCAST and wparam == PBT_POWERSETTINGCHANGE and lparam:
                setting = ctypes.cast(lparam, ctypes.POINTER(POWERBROADCAST_SETTING)).contents
                if bytes(setting.PowerSetting) == bytes(console_display):
                    self._win_state = int(setting.Data)
                return 1  # TRUE
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        proc = WNDPROC(wndproc)  # keep a reference for the lifetime of the window

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = proc
        wc.hInstance = hinst
        wc.lpszClassName = "DeckShowDisplayWatch"
        if not user32.RegisterClassW(ctypes.byref(wc)):
            log.warning("display watch: RegisterClassW failed (%s)", ctypes.get_last_error())
            return
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "DeckShow display watch", 0, 0, 0, 0, 0,
                                      HWND_MESSAGE, None, hinst, None)
        if not hwnd:
            log.warning("display watch: CreateWindowExW failed (%s)", ctypes.get_last_error())
            return
        # RegisterPowerSettingNotification, not PowerSettingRegisterNotification:
        # the latter lives in powrprof.dll and takes a callback, this one takes
        # the window handle we just made (verified on Windows 10 22H2).
        user32.RegisterPowerSettingNotification.restype = wintypes.HANDLE
        user32.RegisterPowerSettingNotification.argtypes = [wintypes.HANDLE, ctypes.POINTER(GUID), wintypes.DWORD]
        if not user32.RegisterPowerSettingNotification(hwnd, ctypes.byref(console_display), DEVICE_NOTIFY_WINDOW_HANDLE):
            log.warning("display watch: RegisterPowerSettingNotification failed (%s)", ctypes.get_last_error())
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    # -- common ---------------------------------------------------------------
    def asleep(self):
        return self._probe() if self._probe else None

    def poll(self):
        """'slept' / 'woke' on a transition, else None."""
        now = self.asleep()
        if now is None or now == self.state:
            return None
        was, self.state = self.state, now
        if was is None:
            return None  # first reading: nothing to report
        return "slept" if now else "woke"


# ============================================================================
# Rendering: bar meters and icon/text show
# ============================================================================
# Theme A ("바 모드") rendering -- host-agnostic: takes
# analyzer numbers in, gives back per-button images; nothing here knows about Stream
# Deck, WebSocket, or setImage.
#
# The model (사용자 확정, 2026-09-19):
#   - Bars are VERTICAL and fill bottom-to-top. A column of stacked buttons (a 3x2
#     device's 1/4, 2/5, 3/6 pairs) is ONE meter: the bottom button fills first, and
#     once it is full the excess overflows into the button above it.
#   - "Bars" (1..MAX_BARS_PER_KEY) is how many vertical bars sit side by side in
#     one button, each its own frequency band; bar width follows from the count with
#     1px gaps (bar_layout()).
#   - Style: SEGMENT_BAR draws each bar as a stack of horizontal slats (an LED
#     ladder), SEGMENT_SOLID as one continuous bar. Color: COLOR_SCHEME_TONE gives
#     a smooth low->mid->high gradient up the meter's height; COLOR_SCHEME_TIER
#     hard zones by height (0-50% low, 51-90% mid, 91%+ high).
#
# Grid convention: `rows` x `cols`, cell (0, 0) is TOP-LEFT; column = one band's
# meter, row 0 = top, row (rows-1) = bottom (fills first).

# Default 3-color palette (low/mid/high), user-configurable via ui/inspector.html
# (사용자 지적, 2026-09-19: "Color는 3개를 선택할 수 있게 해").
DEFAULT_PALETTE = ((0, 230, 90), (255, 220, 0), (255, 40, 40))  # vivid green, yellow, red
# (사용자 지적, 2026-09-19: "색이 왜 이리 탁하지" -- the earlier muted values topped out at 200/255)
_DECAY_PER_FRAME = 1  # bars fall at most 1 row per frame
# The peak marker: a thin line left behind at the highest point the meter
# reached, sliding down one button per second while the bar itself drops fast
# (사용자 요청 2026-09-29: "가장 높은 라인은 천천히 내려오다가"). A new peak
# takes it straight back up, and a rising bar that catches the line carries
# it up with it.
_PEAK_FALL_PER_FRAME = 1.0 / RENDER_FPS  # one key per second
_PEAK_MIN_GAP = 0.04  # closer than this to the bar's top and the line is not drawn
_PEAK_FLOOR = 0.10    # and it disappears rather than resting on the bottom edge
_PNG_COMPRESS = 1  # zlib level for key images: 1 is ~4x cheaper to encode than the default 6 and the
# images are a few hundred bytes either way (encoding was the plugin's largest remaining cost, 2026-09-22)
_BG = (0, 0, 0)  # margin background: pure black so the LCD reads as OFF, like native keys (사용자 지적, 2026-09-19)

COLOR_SCHEME_TONE = "tone"  # A-2: smooth low->mid->high gradient up the meter's height
COLOR_SCHEME_TIER = "tier"  # A-1: fixed by height, classic VU-meter zones
SEGMENT_SOLID = "solid"
SEGMENT_BAR = "bar"
SEGMENT_ICONS = "icons"  # 테마 B: a glyph per key, re-rolled on every beat
SEGMENT_CYCLE = "cycle"  # Style "Random": Text/Icon -> random characters -> Stripe -> Icons, repeat

# Classic VU-meter zone thresholds along the meter's height (사용자 지적,
# 2026-09-19: "green zone은 50%, yellow zone은 51-90, red는 91 이상").
TIER_YELLOW_AT = 0.5
TIER_RED_AT = 0.9

# SEGMENT_BAR slats: 2 device pixels of color, 1 pixel of gap, as many as fit the
# button (사용자 지적, 2026-09-19: "바 타입인 경우 2픽셀 색, 1픽셀 공간으로 해봐").
_LADDER_SLAT_PX = 2
_LADDER_GAP_PX = 1
_LADDER_MIN_OUTER_PX = 9  # top/bottom inset, same curved-edge reason as _SIDE_MARGIN_PCT
_SOLID_STRIPS = 24  # SEGMENT_SOLID is drawn as gapless strips so TIER zones can still band it
_SIDE_MARGIN_PCT = 10  # left/right inset for the whole row of bars (8px at 80px) -- the keycap's
# curved edges/rounded corners distort anything closer (사용자 지적, 2026-09-19)


def _lerp(a, b, t):
    return a + (b - a) * t


def _dim(color, factor):
    factor = min(1.0, max(0.0, factor))
    return tuple(round(c * factor) for c in color)


# Gradient anchors up the meter: pure `low` until GRADIENT_LOW_HOLD, blend to
# `mid` by GRADIENT_MID_AT, then to `high` at the top (사용자 지적, 2026-09-19:
# "gradient solid에서 최하단색 범위를 좀 더 올려" -- was a straight 0/0.5/1 ramp).
GRADIENT_LOW_HOLD = 0.25
GRADIENT_MID_AT = 0.6


def _color_for_level(position, palette):
    """A-2 (TONE): smooth low->mid->high by position (0..1) up the meter."""
    low, mid, high = palette
    position = min(1.0, max(0.0, position))
    if position <= GRADIENT_LOW_HOLD:
        return tuple(low)
    if position < GRADIENT_MID_AT:
        t = (position - GRADIENT_LOW_HOLD) / (GRADIENT_MID_AT - GRADIENT_LOW_HOLD)
        return tuple(round(_lerp(low[i], mid[i], t)) for i in range(3))
    t = (position - GRADIENT_MID_AT) / (1.0 - GRADIENT_MID_AT)
    return tuple(round(_lerp(mid[i], high[i], t)) for i in range(3))


def _zone_color(position, palette):
    """A-1 (TIER): color by position (0..1) along the meter's height."""
    low, mid, high = palette
    if position < TIER_YELLOW_AT:
        return low
    if position < TIER_RED_AT:
        return mid
    return high


MAX_BARS_PER_KEY = 14  # at 80px with 1px gaps and 5px side margins a bar is still >= 4px wide,
# the minimum for a capsule end to read as round (사용자 요청, 2026-09-19: "최대 몇개까지 가능할까?")
_BAR_GAP_PX = 1  # 사용자 지정: "여백 1픽셀"


def bar_layout(size, n):
    """Left x-offsets and width (device pixels) for `n` bars side by side in a
    key of `size` pixels: 1px gaps, whole-pixel bars, leftover pixels split into
    the side margins so the row stays centered."""
    n = max(1, int(n))
    side = round(size * _SIDE_MARGIN_PCT / 100.0)
    usable = size - 2 * side
    bar_w = max(1, (usable - (n - 1) * _BAR_GAP_PX) // n)
    total = n * bar_w + (n - 1) * _BAR_GAP_PX
    left = side + (usable - total) // 2
    return [left + i * (bar_w + _BAR_GAP_PX) for i in range(n)], bar_w


class ShowRenderer:
    """Owns the per-meter bar-height state (for the decay-limited fall) and turns
    analyzer levels into per-meter cell lists each frame. Each meter has its own
    height in keys (`rows_per_meter`): the buttons actually present in that column,
    stacked bottom-up -- so any placement of the action keys works, including a
    single row, a single column, or gaps (사용자 지적, 2026-09-19)."""

    def __init__(self, cols, rows_per_meter):
        self.cols = cols
        self.rows_per_meter = list(rows_per_meter)
        self._bar = [0.0] * cols  # current fill height per meter, in keys (float for partial fill)
        self._peak = [0.0] * cols  # highest point the meter reached, sliding down slowly

    def render(self, levels, peak_hold=True):
        """`levels`: `cols` floats in [0, 1], one per meter. Returns, per meter, a
        list indexed by row_from_bottom (0 = bottom key) of
        (level, fraction, row_from_bottom, meter_rows): `fraction` is how full
        that button's share of the meter is (1.0 lit, 0.0 off, in between for the
        key the bar top currently sits in). Values are rounded so unchanged
        frames dedup cleanly upstream."""
        assert len(levels) == self.cols
        out = []
        for col in range(self.cols):
            rows = self.rows_per_meter[col]
            level = min(1.0, max(0.0, float(levels[col])))
            target = level * rows
            if target < self._bar[col]:
                self._bar[col] = max(target, self._bar[col] - _DECAY_PER_FRAME)
            else:
                self._bar[col] = target
            bar = self._bar[col]
            full_rows = int(bar)
            partial = bar - full_rows
            level_r = round(level, 2)

            # The peak line: straight up to a new high, otherwise down a button per
            # second, and never below the bar itself (a rising bar carries it).
            if peak_hold:
                self._peak[col] = max(bar, self._peak[col] - _PEAK_FALL_PER_FRAME)
                peak = self._peak[col]
                # No line when it would sit on the bar's own top (it would just
                # thicken the bar) and none once it has sunk to the floor, where
                # it would hang under a silent meter (사용자 지적 2026-09-29:
                # "맨 하단의 아래까지도 막대가 내려가고 있어").
                if peak <= bar + _PEAK_MIN_GAP or peak < _PEAK_FLOOR:
                    peak_row, peak_in_row = -1, 0.0
                else:
                    peak_row = min(rows - 1, int(peak))
                    peak_in_row = round(peak - peak_row, 2)
            else:
                self._peak[col] = 0.0
                peak_row, peak_in_row = -1, 0.0

            cells = []
            for row_from_bottom in range(rows):
                if row_from_bottom < full_rows:
                    fraction = 1.0
                elif row_from_bottom == full_rows and partial > 0:
                    fraction = round(partial, 2)
                else:
                    fraction = 0.0
                # only the button the line sits in carries it
                peak_here = peak_in_row if row_from_bottom == peak_row else 0.0
                cells.append((level_r, fraction, row_from_bottom, rows, peak_here))
            out.append(cells)
        return out


_KEY_PNG_CACHE = {}  # (cells, scheme, palette, segment, rotation, size) -> PNG bytes
_KEY_PNG_CACHE_MAX = 8000  # a few hundred bytes each; cleared wholesale when full


def key_image_bytes(cells, *, color_scheme, palette, segment, rotation=0, size=72):
    """PNG for one button. `cells`: this button's bars left-to-right, each a
    (level, fraction, row_from_bottom, meter_rows) tuple from ShowRenderer.render() -- one
    per band packed into the key (bands_per_key()). Drawn upright, then rotated
    counter-clockwise by `rotation` so it reads upright once the device itself
    has been turned that many degrees clockwise (same convention as the key
    coordinate mapping in the adapter).

    The image is a pure function of its arguments (levels arrive rounded), so
    finished PNGs are cached: a steady or repeating meter costs nothing to draw
    and encode again."""
    key = (tuple(cells), color_scheme, tuple(map(tuple, palette)), segment, rotation, size)
    png = _KEY_PNG_CACHE.get(key)
    if png is not None:
        return png
    png = _key_image_bytes_uncached(cells, color_scheme=color_scheme, palette=palette, segment=segment, rotation=rotation, size=size)
    if len(_KEY_PNG_CACHE) >= _KEY_PNG_CACHE_MAX:
        _KEY_PNG_CACHE.clear()
    _KEY_PNG_CACHE[key] = png
    return png


def _draw_peak(draw, x0, x1, peak, row_from_bottom, rows, color_scheme, palette, size, ladder):
    """The thin line the meter leaves at its highest point. `peak` is where it
    sits inside this key (0 = it is not in this key, 1.0 = at its top).

    It is lit as one rung of the ladder, so it stands on the same grid as the
    bar below it; drawn on a scale of its own it sat below the lowest slat and
    above the highest (사용자 지적 2026-09-29: "가장 아래선 위치와 비교해봐").
    Only the ladder style has it: on the smooth capsule a detached line looks
    out of place (사용자 지적 2026-09-29: "Solid 에 피크 홀드는 안 어울리네")."""
    if not peak:
        return
    peak = min(1.0, max(0.0, float(peak)))
    position = (row_from_bottom + peak) / rows
    color = _zone_color(position, palette) if color_scheme == COLOR_SCHEME_TIER else _color_for_level(position, palette)
    strips, strip_h, gap, outer = ladder
    i = min(strips - 1, int(peak * strips))
    y1 = size - 1 - outer - i * (strip_h + gap)
    draw.rectangle((x0, y1 - strip_h + 1, x1, y1), fill=color)  # exactly one rung


def _key_image_bytes_uncached(cells, *, color_scheme, palette, segment, rotation=0, size=72):
    import io
    k = len(cells)
    img = Image.new("RGB", (size, size), _BG)
    draw = ImageDraw.Draw(img)
    lefts, bar_w = bar_layout(size, k)
    if segment == SEGMENT_BAR:
        # Whole-pixel slat layout: fractional heights rasterize as a mix of 1px
        # and 2px slats (사용자 지적, 2026-09-19: "중간에 높이가 다른 놈이 있는데?").
        strip_h, gap = _LADDER_SLAT_PX, _LADDER_GAP_PX
        strips = (size - 2 * _LADDER_MIN_OUTER_PX + gap) // (strip_h + gap)
        outer = (size - (strips * strip_h + (strips - 1) * gap)) // 2
    else:
        strips, gap, outer = _SOLID_STRIPS, 0, 0
        strip_h = size / strips

    for j, cell in enumerate(cells):
        level, fraction, row_from_bottom, rows = cell[0], cell[1], cell[2], cell[3]
        peak = cell[4] if len(cell) > 4 else 0.0  # where the peak line sits in this key, 0 = not here
        x0 = lefts[j]
        x1 = x0 + bar_w - 1
        if segment != SEGMENT_BAR:
            _draw_capsule(img, x0, x1, fraction, row_from_bottom, rows, color_scheme, palette, size)
            continue
        lit = min(1.0, max(0.0, float(fraction))) * strips
        full = int(lit)
        partial = lit - full
        for i in range(strips):  # i=0 is the bottom strip
            # PIL's rectangle() fills both end pixels inclusively, so a strip of
            # strip_h pixels spans y0 .. y0 + strip_h - 1 (measured: 2 came out 3).
            y1 = size - 1 - outer - i * (strip_h + gap)
            y0 = y1 - strip_h + 1
            # Both schemes color by POSITION along the meter's height (across all
            # stacked buttons), not by the current level: TIER in hard zones, TONE as
            # a smooth bottom-to-top gradient (사용자 지적, 2026-09-19: "gradient는
            # 색 전체에 적용하는게 아니라, 아래에서 위로 그라디언트로 변경된다는 뜻").
            position = (row_from_bottom + (i + 0.5) / strips) / rows
            if color_scheme == COLOR_SCHEME_TIER:
                base = _zone_color(position, palette)
            else:
                base = _color_for_level(position, palette)
            # Unlit strips are left as background -- no faint "ghost" of the color
            # where the bar has not reached (사용자 지적, 2026-09-19: "색을 도달하지
            # 않은 구역에 미리 깔아두지 말것").
            if i < full:
                draw.rectangle((x0, y0, x1, y1), fill=base)
            elif i == full and partial > 0:
                draw.rectangle((x0, y0, x1, y1), fill=_dim(base, partial))
        _draw_peak(draw, x0, x1, peak, row_from_bottom, rows, color_scheme, palette, size,
                   (strips, strip_h, gap, outer))

    if rotation:
        img = img.rotate(rotation)  # PIL rotates counter-clockwise, like np.rot90
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=_PNG_COMPRESS)
    return buf.getvalue()


_CAPSULE_EDGE_MARGIN_PCT = 11  # gap between a meter's real end and the key edge (Solid Type)
_CAPSULE_SUPERSAMPLE = 4  # draw the capsule 4x larger and shrink: aliased small
# circles look pointed/hexagonal (사용자 지적, 2026-09-19: "끝이 동그랗지 않고 뾰족한데?")
_CAPSULE_LAYER_CACHE = {}  # (bar position, palette, size) -> shrunk color layer
_CAPSULE_MASK_CACHE = {}  # (bar position, lit height, ends) -> shrunk mask


def _draw_capsule(img, x0, x1, fraction, row_from_bottom, rows, color_scheme, palette, size):
    """SEGMENT_SOLID bar with rounded ends (사용자 지적, 2026-09-19, 온도계 모양
    참고 이미지): a capsule that starts as a full circle at the bottom as soon as
    there is any level at all, then grows upward. Only the ends where the bar
    really starts/stops are rounded -- where it continues into the key above or
    below, that end stays flat so the two buttons read as one bar."""
    fraction = min(1.0, max(0.0, float(fraction)))
    if fraction <= 0:
        return
    S = _CAPSULE_SUPERSAMPLE
    big = size * S
    bx0, bx1 = x0 * S, (x1 + 1) * S - 1
    bar_w = bx1 - bx0 + 1
    radius = bar_w / 2.0
    at_meter_bottom = row_from_bottom == 0
    at_meter_top = row_from_bottom == rows - 1
    # Breathing room at the meter's own two ends (not at button-to-button continuations)
    # -- 사용자 지적, 2026-09-19: "아래 시작 지점을 좀 위로 올리자".
    margin = big * _CAPSULE_EDGE_MARGIN_PCT / 100.0
    bottom_margin = margin if at_meter_bottom else 0.0
    top_margin = margin if at_meter_top else 0.0
    avail = big - bottom_margin - top_margin
    lit_h = fraction * avail
    if at_meter_bottom:
        lit_h = max(lit_h, bar_w)  # never less than a full circle
    lit_h = int(round(min(avail, max(1.0, lit_h))))  # whole pixels, >= 1 (crashed on 0.8px, 2026-09-19)
    y_bot = int(round(big - 1 - bottom_margin))
    y_top = y_bot - lit_h + 1
    radius = min(radius, lit_h / 2.0)
    ends_here = fraction < 1.0 or at_meter_top
    starts_here = at_meter_bottom

    # Color layer: the whole column colored by position (gradient or zones),
    # shown through a capsule-shaped mask. Both are cached: the layer never
    # changes for a given bar position/palette, and the mask only depends on the
    # lit height (whole supersampled pixels) and which ends are rounded. Without
    # the cache this drew and LANCZOS-shrank two 4x images per bar per frame and
    # was the plugin's whole CPU cost (2026-09-22: 25-35% on a Mini).
    layer_key = (bx0, bx1, row_from_bottom, rows, color_scheme, tuple(map(tuple, palette)), size)
    layer = _CAPSULE_LAYER_CACHE.get(layer_key)
    if layer is None:
        layer = Image.new("RGB", (big, big), _BG)
        ld = ImageDraw.Draw(layer)
        for i in range(_SOLID_STRIPS):
            sy1 = big - 1 - i * (big / _SOLID_STRIPS)
            sy0 = sy1 - big / _SOLID_STRIPS + 1
            position = (row_from_bottom + (i + 0.5) / _SOLID_STRIPS) / rows
            base = _zone_color(position, palette) if color_scheme == COLOR_SCHEME_TIER else _color_for_level(position, palette)
            ld.rectangle((bx0, sy0, bx1, sy1), fill=base)
        layer = layer.reduce(S)  # box average: the layer is axis-aligned strips, no need for LANCZOS
        _CAPSULE_LAYER_CACHE[layer_key] = layer

    mask_key = (bx0, bx1, lit_h, y_bot, int(radius * 4), ends_here, starts_here, size)
    mask = _CAPSULE_MASK_CACHE.get(mask_key)
    if mask is None:
        # Mask built from plain ellipses + a rectangle rather than rounded_rectangle():
        # Pillow's rounded_rectangle raises on the thin slivers a continuation button
        # gets right after the bar crosses into it (2026-09-19 crash, "y1 must be
        # greater than or equal to y0").
        mask = Image.new("L", (big, big), 0)
        md = ImageDraw.Draw(mask)
        body_top, body_bot = y_top, y_bot
        if ends_here:
            cap_h = min(2 * radius, lit_h)
            md.ellipse((bx0, y_top, bx1, y_top + cap_h), fill=255)
            body_top = y_top + cap_h / 2.0
        if starts_here:
            md.ellipse((bx0, y_bot - 2 * radius, bx1, y_bot), fill=255)
            body_bot = y_bot - radius
        if body_bot >= body_top:
            md.rectangle((bx0, body_top, bx1, body_bot), fill=255)
        mask = mask.resize((size, size), Image.LANCZOS)  # the round ends are what needs the smooth shrink
        if len(_CAPSULE_MASK_CACHE) > 4000:
            _CAPSULE_MASK_CACHE.clear()
        _CAPSULE_MASK_CACHE[mask_key] = mask
    img.paste(layer, (0, 0), mask)


def solid_color_png_bytes(color, size=72):
    """Plain full-square fill, used for the blank-on-stop screen."""
    import io
    buf = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buf, format="PNG", compress_level=_PNG_COMPRESS)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Theme B: icons (테마 B, 사용자 정의 2026-09-19: "버튼 하나에 아이콘을 다양한
# 색, 다양한 배경으로 마구 뿌리는 것... 비트에 맞추는 것"). Every button shows one
# glyph; on each onset of its band (a sudden rise = a beat) the glyph, its color
# and the background are re-rolled at random; between beats the glyph pulses
# with the level. Glyphs come from the bundled Bootstrap Icons font (MIT) or a
# font file + character list the user points at.



_FONTS_DIR = str(ROOT / "streamdeck" / "com.castika.deckshow.sdPlugin" / "fonts")
BUNDLED_ICON_FONT = os.path.join(_FONTS_DIR, "bootstrap-icons.woff")
# Default text font (사용자 확정, 2026-09-19: "기본 폰트를 esamanru를 기본값으로", SIL OFL,
# see fonts/LICENSE-esamanru.txt) -- used whenever no font is chosen and there is text to show.
BUNDLED_TEXT_FONT = os.path.join(_FONTS_DIR, "esamanru Bold.ttf")
_BUNDLED_CODEPOINTS = os.path.splitext(BUNDLED_ICON_FONT)[0] + ".codepoints.json"
# The plugin icon's dot palette (deckshow_icon.png), used as-is for the icon show.
ICON_DOT_COLORS = [(240, 110, 220), (210, 240, 90), (80, 220, 200), (255, 90, 90), (110, 230, 130),
                   (170, 120, 255), (255, 150, 60), (120, 140, 255), (90, 190, 255), (255, 210, 70)]
ICON_DARK_BASE = (20, 20, 24)


def _saturate(color):
    """Same hue as the icon's dot, but fully saturated and at full value -- the
    dots themselves are pastel (사용자 지적, 2026-09-19: "완전히 파스텔이네")."""
    h, _s, _v = colorsys.rgb_to_hsv(*(c / 255.0 for c in color))
    return tuple(round(c * 255) for c in colorsys.hsv_to_rgb(h, 1.0, 1.0))


ICON_BG_COLORS = [_saturate(c) for c in ICON_DOT_COLORS if c not in ((210, 240, 90), (80, 220, 200))]  # lime, mint dropped (사용자 지적)
_ICON_BLACK_BG_SHARE = 0.5  # share of keys drawn on pure black (colored glyph) in the text show
_ONSET_JUMP = 0.06  # level must rise this much over its running average to count as a beat
_ONSET_MIN_LEVEL = 0.1
_ONSET_REFRACTORY_S = 0.15  # up to ~6 beats/s (사용자 지적 2026-09-19: "글자 움직임 확 줄었어" -- was 0.3)
_EMA = 0.7  # running average weight for the onset baseline
_PULSE_DECAY = 0.8  # per frame (10 fps): a zoom shrinks back over ~0.5 s


class IconShow:
    """Per-button beat-reactive glyph state + renderer."""

    # Fonts to try, in order, when the user typed glyphs but named no font file:
    # the bundled icon font only has its own icons, so typed characters would be
    # blank in it (사용자 질문, 2026-09-19: "글리프는 키보드로 넣은 것이 나오는 것인가?").
    SYSTEM_FONT_CANDIDATES = [
        "/System/Library/Fonts/Apple Symbols.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]

    TEXT_PASSES = 2  # built-in text: after 2 full passes, the same number of random characters, repeat
    # (사용자 확정, 2026-09-19: "그 숫자만큼 랜덤" -- a count, not a time limit)

    def __init__(self, font_path=None, glyphs=None, sequential=False, alternate_random=False):
        # Sequence mode (사용자 요청, 2026-09-19: "이 순서로 글자가 나오게 해줘, ()는
        # 제외"): the typed text is shown one character at a time, in order, one
        # step per beat, walking across the buttons in reading order. Parenthesised
        # parts and whitespace are dropped.
        self.sequential = bool(sequential)
        text = re.sub(r"\([^)]*\)", "", glyphs or "")
        text = re.sub(r"[,.]+", "", text)  # commas and "..." dropped too (사용자 지적)
        glyphs = [ch for ch in text if not ch.isspace()]
        if font_path:
            self.font_path = font_path
        elif glyphs:
            self.font_path = BUNDLED_TEXT_FONT if os.path.exists(BUNDLED_TEXT_FONT) else next(
                (f for f in self.SYSTEM_FONT_CANDIDATES if os.path.exists(f)), BUNDLED_ICON_FONT)
        else:
            self.font_path = BUNDLED_ICON_FONT
        # An icon font (glyphs in the Private Use Area, e.g. the bundled Bootstrap
        # Icons) cannot show typed text: use all of its icons, at random
        # (사용자 확정, 2026-09-19: "부트스트랩은 무작위").
        icon_pool = self._all_glyphs(self.font_path) if font_path else []
        if icon_pool and self._is_icon_font(self.font_path):
            self.sequential = False
            self.pool = icon_pool
            glyphs = []
        if glyphs:
            self.pool = self._filter_to_font(glyphs, self.font_path) if not self.sequential else glyphs
            if not self.pool:
                log.warning("none of the %d typed glyphs exist in %s -- using the bundled icons", len(glyphs), self.font_path)
        elif font_path and not self.pool:
            # A user text font with no glyph list: every letter/symbol it has.
            self.pool = icon_pool
        elif not font_path:
            self.pool = []
        if not self.pool:
            self.font_path = BUNDLED_ICON_FONT
            with open(_BUNDLED_CODEPOINTS, encoding="utf-8") as f:
                self.pool = [chr(cp) for cp in json.load(f)]
        self._fonts = {}
        self._state = {}  # key -> dict(glyph, fg, bg, det)
        self._seq_index = 0  # next character to show (sequence mode)
        self._seq_key = 0  # next key (in reading order) to show it on
        # Built-in text only (사용자 확정, 2026-09-19): show the text TEXT_PASSES times,
        # then RANDOM_BREAK_S of random characters from the whole font, then the
        # text again, and so on.
        self.alternate_random = bool(alternate_random) and self.sequential
        self._random_pool = self._all_glyphs(self.font_path) if self.alternate_random else []
        self._random_left = 0  # random characters still to show in the current break (0 = in text)
        self.cycles_done = 0  # +1 each time text (TEXT_PASSES) + random block have both finished
        self.change_count = 0  # every character change (for callers pacing other phases)

    @staticmethod
    def _is_icon_font(font_path):
        try:
            from fontTools.ttLib import TTFont
            cps = (TTFont(font_path, fontNumber=0).getBestCmap() or {}).keys()
            return any(0xE000 <= cp <= 0xF8FF or 0xF0000 <= cp <= 0x10FFFD for cp in cps)
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _all_glyphs(font_path, limit=4000):
        try:
            from fontTools.ttLib import TTFont
            cps = sorted((TTFont(font_path, fontNumber=0).getBestCmap() or {}).keys())
        except Exception as e:  # noqa: BLE001
            log.warning("cannot read glyph list of %s (%s)", font_path, e)
            return []
        pua = [cp for cp in cps if 0xE000 <= cp <= 0xF8FF or 0xF0000 <= cp <= 0x10FFFD]
        chosen = pua or [cp for cp in cps if cp > 0x20 and not (0xD800 <= cp <= 0xDFFF)]
        log.info("%s: %d glyphs available (%s)", os.path.basename(font_path), len(chosen), "icon font" if pua else "text font")
        return [chr(cp) for cp in chosen[:limit]]

    @staticmethod
    def _filter_to_font(glyphs, font_path):
        """Keep only characters the font actually has (a missing glyph renders as
        nothing or a box). Needs fontTools; without it, trust the user's list."""
        try:
            from fontTools.ttLib import TTFont
            font = TTFont(font_path, fontNumber=0)
            cmap = font.getBestCmap() or {}
            return [ch for ch in glyphs if ord(ch) in cmap]
        except Exception as e:  # noqa: BLE001 -- any font parsing problem: don't filter
            log.info("glyph filtering skipped (%s)", e)
            return list(glyphs)

    def _font(self, px):
        px = max(8, int(px))
        if px not in self._fonts:
            self._fonts[px] = ImageFont.truetype(self.font_path, px)
        return self._fonts[px]

    @staticmethod
    def _roll():
        """Background = one of the plugin icon's own dot colors, at full strength
        (사용자 지적, 2026-09-19: "배경색을 바꾸어야 눈에 강하게 보여. 우리가 이 쇼
        아이콘으로 쓴 점들의 색상이 눈에 잘 띄네"); glyph = the most contrasting
        other dot color, or the icon's dark base if none contrasts enough."""
        # Half of the buttons get a pure black background with a vivid glyph, the
        # other half a vivid background with a white glyph (사용자 지적,
        # 2026-09-19: "배경에 완전 블랙도 선택"; 2026-09-22: "검은 배경이 절반은
        # 되어야 할 것 같아. 검은배경이라고 해도 글자색은 있으니까").
        if random.random() < _ICON_BLACK_BG_SHARE:
            return (0, 0, 0), random.choice(ICON_BG_COLORS)
        return random.choice(ICON_BG_COLORS), (255, 255, 255)

    # Two beat detectors on two bass ranges; each button follows one of them and,
    # on that detector's beat, re-rolls only with some probability -- so buttons
    # change at different moments instead of all at once (사용자 지적,
    # 2026-09-19: "한꺼번에 바뀌니까 좀 그러네... 랜덤하게").
    NUM_DETECTORS = 2
    REROLL_PROBABILITY = 0.6

    def beat(self, signals):
        """`signals`: one level per detector (e.g. 80-120 Hz, 120-250 Hz), fed
        every frame. Returns the number of buttons re-rolled this frame."""
        if not hasattr(self, "_det"):
            self._det = [{"ema": 0.0, "last": -1e9} for _ in range(self.NUM_DETECTORS)]  # last=-inf: first beat allowed at once
        now = time.monotonic()
        rerolled = 0
        if self.sequential:
            # One shared detector: any bass beat advances the text by one character.
            d = self._det[0]
            signal = max(signals[:self.NUM_DETECTORS]) if signals else 0.0
            hit = (signal > _ONSET_MIN_LEVEL and signal - d["ema"] > _ONSET_JUMP
                   and now - d["last"] > _ONSET_REFRACTORY_S)
            d["ema"] = _EMA * d["ema"] + (1 - _EMA) * signal
            if hit and self._state:
                d["last"] = now
                keys = sorted(self._state)
                st = self._state[keys[self._seq_key % len(keys)]]
                st["glyph"] = self.pool[self._seq_index % len(self.pool)]
                st["bg"], st["fg"] = self._roll()
                st["pulse"] = 1.0
                self._seq_index += 1
                self._seq_key += 1
                rerolled = 1
            return rerolled
        for i, signal in enumerate(signals[:self.NUM_DETECTORS]):
            d = self._det[i]
            hit = (signal > _ONSET_MIN_LEVEL and signal - d["ema"] > _ONSET_JUMP
                   and now - d["last"] > _ONSET_REFRACTORY_S)
            d["ema"] = _EMA * d["ema"] + (1 - _EMA) * signal
            if not hit:
                continue
            d["last"] = now
            followers = [st for st in self._state.values() if st["det"] == i]
            if not followers:
                continue
            chosen = [st for st in followers if random.random() < self.REROLL_PROBABILITY] or [random.choice(followers)]
            for st in chosen:
                st["glyph"] = random.choice(self.pool)
                st["bg"], st["fg"] = self._roll()
                st["pulse"] = 1.0
                rerolled += 1
        return rerolled

    def update(self, key, level):
        """Make sure this button has a glyph/colors (first sight rolls one and
        assigns it a beat detector at random)."""
        if key not in self._state:
            bg, fg = self._roll()
            glyph = self.pool[0] if self.sequential else random.choice(self.pool)
            self._state[key] = {"glyph": glyph, "bg": bg, "fg": fg, "pulse": 0.0, "prev": 0.0,
                                "det": random.randrange(self.NUM_DETECTORS)}

    ZOOM_RISE = 0.05  # a glyph growing by this much (of full scale) between frames = one zoom

    def zoom_step(self, key, level):
        """The zoom itself is the trigger (사용자 확정, 2026-09-19: "줌 움직임이 있을
        때 글자 교체"): whenever this button's level rises from the previous frame
        (its glyph is about to grow), swap the character -- next in the text for
        Sequence, random otherwise -- and re-roll the background. Returns True
        when it changed. Keys are fed in reading order, so Sequence text flows
        across buttons."""
        st = self._state[key]
        rose = level - st["prev"] > self.ZOOM_RISE
        st["prev"] = level
        if not rose:
            return False
        if self.alternate_random and self._random_pool:
            span = self.TEXT_PASSES * len(self.pool)
            if self._random_left == 0 and self._seq_index >= span:
                self._random_left = span  # text done twice -> as many random characters
                self._seq_index = 0
                log.info("built-in text shown %d times -> %d random characters", self.TEXT_PASSES, span)
            if self._random_left > 0:
                self._random_left -= 1
                if self._random_left == 0:
                    self.cycles_done += 1
                    log.info("random break over -> built-in text again (cycle %d done)", self.cycles_done)
                st["glyph"] = random.choice(self._random_pool)
                st["bg"], st["fg"] = self._roll()
                self.change_count += 1
                return True
        if self.sequential:
            st["glyph"] = self.pool[self._seq_index % len(self.pool)]
            self._seq_index += 1
        else:
            st["glyph"] = random.choice(self.pool)
        st["bg"], st["fg"] = self._roll()
        self.change_count += 1
        return True

    def image_bytes(self, key, level, size=72, rotation=0):
        import io
        st = self._state[key]
        level = min(1.0, max(0.0, float(level)))
        # Background stays exactly the color the last beat chose -- dimming it with
        # the level made it flicker at frame rate (사용자 지적, 2026-09-19: "배경색도
        # 깜빡이는 거 같은데"). Only the glyph size follows the level.
        bg = st["bg"]
        fg = st["fg"]
        img = Image.new("RGB", (size, size), bg)
        draw = ImageDraw.Draw(img)
        px = size * (0.6 + 0.45 * level)  # size follows the level every frame; a rise = a zoom
        try:
            font = self._font(px)
            draw.text((size / 2, size / 2), st["glyph"], font=font, fill=fg, anchor="mm")
        except OSError:
            draw.rectangle((size * 0.3, size * 0.3, size * 0.7, size * 0.7), fill=fg)  # font unreadable
        if rotation:
            img = img.rotate(rotation)
        buf = io.BytesIO()
        img.save(buf, format="PNG", compress_level=_PNG_COMPRESS)
        return buf.getvalue()


# ============================================================================
# GridShow: one show on a set of buttons
# ============================================================================
# Host-agnostic show for one rectangular button grid: turns audio levels into
# per-button PNGs, reporting only the buttons whose picture changed since the last
# frame. This is the same show the plugin section draws (its `_render_frame`
# / `_render_frame_icons`, per device), lifted out for hosts where the buttons are a
# plain full grid we address by (row, col): the Companion adapter draws on a page
# we own, where every cell is ours. The Elgato adapter keeps its own copy for now
# because its buttons can be an arbitrary subset of the grid (user-placed buttons).

CYCLE_PHASE_S = 5 * 60  # Style Random: Stripe and Icons phases last this long each


@dataclass
class ShowSettings:
    """The same knobs ui/inspector.html exposes, with the same defaults as
    the plugin section."""
    segment: str = SEGMENT_ICONS
    bars_per_key: int = 4
    color_scheme: str = COLOR_SCHEME_TIER
    palette: tuple = DEFAULT_PALETTE
    rotation: int = 0
    level_mode: str = "relative"
    sensitivity: float = 1.0
    icon_font_path: str = ""
    icon_glyphs: str = ""
    default_glyphs: str = ""  # the bundled text font's built-in text
    peak_hold: bool = True  # leave a line at the highest point, sliding down a key per second


class GridShow:
    def __init__(self, cols, rows, key_size, settings, keys=None):
        """`keys`: the (row, col) positions that actually exist, default every
        cell of cols x rows. With a subset (user-placed buttons on a page, like the
        Stream Deck plugin's in-place mode) each column's meter is made of the
        buttons present in it, bottom-up, and absent cells are skipped."""
        self.cols, self.rows = int(cols), int(rows)
        self.key_size = int(key_size)
        self.s = settings
        self.keys = sorted(keys) if keys is not None else [(r, c) for r in range(self.rows) for c in range(self.cols)]
        self.renderer = None
        self.icon_show = None
        self.last = {}  # (row, col) -> cells/state tuple last drawn (dedup)
        self.cycle_phase = 0
        self.cycle_phase_started = None
        self._frames = 0

    # -- public ---------------------------------------------------------------
    def reset(self):
        self.renderer = None
        self.icon_show = None
        self.last.clear()

    def frame(self, audio):
        """{(row, col): png_bytes} for every button whose image changed."""
        if self._effective_segment() == SEGMENT_ICONS:
            return self._frame_icons(audio)
        return self._frame_bars(audio)

    # -- bars / stripes -------------------------------------------------------
    def _frame_bars(self, audio):
        k = self.s.bars_per_key
        vrows, vcols = (self.cols, self.rows) if self.s.rotation in (90, 270) else (self.rows, self.cols)
        vmap = self._virtual_index_map(vrows, vcols)
        # Which virtual rows are present in each virtual column: those buttons,
        # bottom-up, form that column's meter (any placement works).
        present = {}
        for (row, col) in self.keys:
            vr, vc = divmod(int(vmap[row][col]), vcols)
            present.setdefault(vc, []).append(vr)
        for vc in present:
            present[vc].sort(reverse=True)  # larger virtual row = lower on the deck = bottom first
        meters = vcols * k
        rows_per_meter = [len(present.get(vc, [])) or 1 for vc in range(vcols) for _ in range(k)]
        if self.renderer is None or self.renderer.cols != meters or self.renderer.rows_per_meter != rows_per_meter:
            self.renderer = ShowRenderer(cols=meters, rows_per_meter=rows_per_meter)
            self.last.clear()
        raw, _ = audio.read(meters, mode=self.s.level_mode)
        levels = [self._shape(x) for x in raw]
        if self.s.segment == SEGMENT_CYCLE and self._cycle_expired():
            self._next_cycle_phase()
            return {}
        meter_cells = self.renderer.render(levels, peak_hold=self.s.peak_hold)
        out = {}
        cache = {}
        for (row, col) in self.keys:
            vr, vc = divmod(int(vmap[row][col]), vcols)
            rank = present[vc].index(vr)  # this button's row_from_bottom within its column's meter
            cells = tuple(meter_cells[vc * k + j][rank] for j in range(k))
            if self.last.get((row, col)) == cells:
                continue
            self.last[(row, col)] = cells
            png = cache.get(cells)
            if png is None:
                png = cache[cells] = key_image_bytes(
                    cells, color_scheme=self.s.color_scheme, palette=self.s.palette,
                    segment=self._effective_segment(), rotation=self.s.rotation, size=self.key_size)
            out[(row, col)] = png
        self._log_levels(raw, levels)
        return out

    # -- icons / text -------------------------------------------------------------
    def _frame_icons(self, audio):
        if self.icon_show is None:
            font = self.s.icon_font_path or BUNDLED_TEXT_FONT
            text = self.s.icon_glyphs.strip() or (self.s.default_glyphs if Path(font) == Path(BUNDLED_TEXT_FONT) else "")
            if self.s.segment == SEGMENT_CYCLE:
                if self.cycle_phase == 0:
                    font, text = BUNDLED_TEXT_FONT, self.s.default_glyphs
                else:
                    font, text = BUNDLED_ICON_FONT, ""
            try:
                self.icon_show = IconShow(font, text or None, sequential=bool(text),
                                          alternate_random=(text == self.s.default_glyphs and bool(text)))
            except (OSError, ValueError) as e:
                log.warning("icon font %r unusable (%s) -- bundled icons instead", font, e)
                self.icon_show = IconShow()
            self.last.clear()
        keys = self.keys
        raw, _ = audio.read(len(keys), mode=self.s.level_mode)
        levels = [self._shape(x) for x in raw]
        for key, level in zip(keys, levels):
            self.icon_show.update(key, level)
            self.icon_show.zoom_step(key, level)
        if self.s.segment == SEGMENT_CYCLE:
            if (self.cycle_phase == 0 and self.icon_show.cycles_done >= 1) or (self.cycle_phase == 2 and self._cycle_expired()):
                self._next_cycle_phase()
                return {}
        out = {}
        for key, level in zip(keys, levels):
            st = self.icon_show._state[key]
            cell = (st["glyph"], st["bg"], st["fg"], round(level, 2))
            if self.last.get(key) == cell:
                continue
            self.last[key] = cell
            out[key] = self.icon_show.image_bytes(key, level, size=self.key_size, rotation=self.s.rotation)
        self._log_levels(raw, levels)
        return out

    # -- helpers ----------------------------------------------------------------
    def _effective_segment(self):
        if self.s.segment != SEGMENT_CYCLE:
            return self.s.segment
        return (SEGMENT_ICONS, SEGMENT_BAR, SEGMENT_ICONS)[self.cycle_phase]

    def _cycle_expired(self):
        if self.cycle_phase_started is None:
            self.cycle_phase_started = time.monotonic()
        return time.monotonic() - self.cycle_phase_started >= CYCLE_PHASE_S

    def _next_cycle_phase(self):
        self.cycle_phase = (self.cycle_phase + 1) % 3
        self.cycle_phase_started = time.monotonic()
        self.reset()
        log.info("Style Random -> phase %d", self.cycle_phase)

    def _shape(self, level):
        level = min(1.0, max(0.0, level))
        if self.s.sensitivity != 1.0 and level > 0:
            level = level ** (1.0 / self.s.sensitivity)
        return level

    def _virtual_index_map(self, vrows, vcols):
        idx = np.arange(vrows * vcols).reshape(vrows, vcols)
        return idx if self.s.rotation == 0 else np.rot90(idx, k=self.s.rotation // 90)

    def _log_levels(self, raw, levels):
        self._frames += 1
        if self._frames % 20 == 0:
            log.info("levels raw=%s -> shown=%s", [round(x, 2) for x in raw[:8]], [round(x, 2) for x in levels[:8]])


# ============================================================================
# Audio: spectrum analyzer
# ============================================================================
# Rolling-FFT spectrum analyzer.
#   - Band count is a parameter (`num_bands`): the renderer maps one Stream Deck
#     grid column to one band, and grid width varies by device (Mini: 3).
#   - Each band gets its own rolling min/max envelope (`band_rel[i]`), so a
#     "relative" level exists per band, not per group.
#   - Produces numbers only; drawing is the rendering section's job.
# Octave-spaced band edges, Hann window, dB normalization, instant-attack /
# release-decay smoothing, and a rolling min/max envelope for "relative to the
# recent loudest/quietest" levels.

def block_decay(block_s, tau_s):
    """Per-block factor of an exponential decay with time constant tau_s."""
    return float(np.exp(-block_s / max(1e-3, float(tau_s))))


GATE_PRESENCE_MARGIN = 0.08  # normalized loudness above the recent minimum that counts as "sound present"

DEFAULT_CONFIG = {
    "audio_input_device": "default",
    "fft_size": 2048,
    "blocksize": 2048,  # = fft_size: one FFT per block, ~23 blocks/s at 48 kHz (the show draws at 10 fps)
    "min_db": -60,
    "max_db": 0,
    "level_release": 0.7,
    "bar_max_fall_s": 2.0,
    "bar_min_rise_s": 4.0,
    # Analysis range (사용자 지적, 2026-09-19: "쓰레기 저음을 버리는 것도 처리하자.
    # 주음 대역은 가청 주파수 대역이어야"): rumble/handling noise below low_hz and
    # anything above high_hz are dropped from the bands AND from the broadband
    # loudness the gate and relative meters use. Was 60-8000 Hz.
    "low_hz": 80.0,
    "high_hz": 16000.0,
}


class SpectrumAnalyzer:
    """Rolling FFT over the mic input; exposes `num_bands` band levels (absolute and
    relative-to-recent-range) plus overall loudness (absolute and relative). Not
    started automatically -- call `start()`/`stop()` to open/close the actual stream:
    the microphone is open only while a show is on.
    """

    def __init__(self, num_bands, cfg=None):
        self.num_bands = int(num_bands)
        # Time-based noise gate (사용자 정의, 2026-09-19: "순간으로 튀는 것을 막고
        # 지속적인 음만 잡아내는 것"): sound must stay present for `gate_ms`
        # before anything is shown; shorter bursts are ignored. 0 = off.
        self.gate_ms = 0
        self._present_since = None
        self.gate_open = True
        self._noise_floor = 1.0  # quietest recent loudness, learned only while the gate is closed
        self.cfg = {**DEFAULT_CONFIG, **(cfg or {})}
        self.device_index, self.device_name = self._pick_device(self.cfg["audio_input_device"])
        samplerate = sd.query_devices(self.device_index)["default_samplerate"]
        log.info("analyzer using device idx=%s name=%r samplerate=%s", self.device_index, self.device_name, samplerate)
        self._init_state(samplerate)

    def _init_state(self, samplerate):
        cfg = self.cfg
        self.samplerate = int(samplerate)
        self.n = int(cfg["fft_size"])
        self.buf = np.zeros(self.n, dtype=np.float32)
        self.window = np.hanning(self.n).astype(np.float32)
        self.win_gain = self.window.sum() / 2.0

        self.levels = np.zeros(self.num_bands, dtype=np.float32)
        self.loudness = 0.0
        # level_release is defined per 1024-sample block; keep the same decay per
        # second whatever the block size is.
        self.release = float(cfg["level_release"]) ** (int(cfg["blocksize"]) / 1024.0)

        self.lock = threading.Lock()
        self.stream = None
        self.last_callback = time.monotonic()
        # A real microphone never reads exactly zero: even a silent room carries
        # the converter's own noise. All-zero blocks mean the samples are not
        # coming from a microphone at all (blocked by the OS, or muted).
        self.saw_signal = False
        self.opened_at = 0.0
        self.block_s = int(cfg["blocksize"]) / float(self.samplerate)

        self.loud_hi, self.loud_lo = 0.0, 1.0
        self.loudness_rel = 0.0
        self.band_hi = np.zeros(self.num_bands, dtype=np.float64)
        self.band_lo = np.ones(self.num_bands, dtype=np.float64)
        self.band_rel = np.zeros(self.num_bands, dtype=np.float32)

        self.hi_decay = block_decay(self.block_s, cfg["bar_max_fall_s"])
        self.lo_rate = 1.0 - block_decay(self.block_s, cfg["bar_min_rise_s"])
        self._build_bins()

    def _pick_device(self, wanted):
        devs = sd.query_devices()
        wanted = (wanted or "").strip()
        if wanted.lower() in ("", "default"):
            default = sd.default.device[0]
            if default is None or default < 0:
                raise RuntimeError("no audio input device available")
            return default, devs[default]["name"]
        inputs = [(i, d) for i, d in enumerate(devs) if d["max_input_channels"] > 0]
        for i, d in inputs:
            if d["name"] == wanted:
                return i, d["name"]
        for i, d in inputs:
            if wanted.lower() in d["name"].lower():
                return i, d["name"]
        default = sd.default.device[0]
        return default, devs[default]["name"]

    def _build_bins(self):
        """`num_bands` octave-ish edges spread log-spaced between low_hz/high_hz."""
        low, high = float(self.cfg["low_hz"]), float(self.cfg["high_hz"])
        centers = np.geomspace(low, high, self.num_bands)
        if self.num_bands > 1:
            ratio = np.sqrt(centers[1:] / centers[:-1])
            edges = np.concatenate(([centers[0] / ratio[0]], np.sqrt(centers[1:] * centers[:-1]), [centers[-1] * ratio[-1]]))
        else:
            edges = np.array([low, high])
        freqs = np.fft.rfftfreq(self.n, 1.0 / self.samplerate)
        self.in_range = (freqs >= low) & (freqs <= high)  # bins that count as "the music" at all
        self.band_bins = []
        for lo, hi in zip(edges[:-1], edges[1:]):
            idx = np.where((freqs >= lo) & (freqs < hi))[0]
            if len(idx) == 0:
                idx = np.array([int(np.argmin(np.abs(freqs - (lo + hi) / 2)))])
            self.band_bins.append(idx)
        # Band power as one matrix product instead of a Python loop over bands
        # (the loop was most of the daemon's CPU while a show ran, 2026-09-22).
        self.band_matrix = np.zeros((self.num_bands, len(freqs)), dtype=np.float32)
        for i, idx in enumerate(self.band_bins):
            self.band_matrix[i, idx] = 1.0

    # -- lifecycle ------------------------------------------------------------
    def start(self):
        if self.stream is not None:
            return
        self.stream = sd.InputStream(
            device=self.device_index,
            channels=1,
            samplerate=self.samplerate,
            blocksize=int(self.cfg["blocksize"]),
            callback=self._callback,
        )
        self.saw_signal = False
        self.opened_at = time.monotonic()
        self.stream.start()
        log.info("stream started: active=%s device=%s samplerate=%s", self.stream.active, self.device_index, self.samplerate)

    def stop(self):
        if self.stream is None:
            return
        stream, self.stream = self.stream, None
        stream.stop()
        stream.close()

    # -- audio thread callback -------------------------------------------------
    def _callback(self, indata, frames, time_info, status):
        with self.lock:
            self.last_callback = time.monotonic()
        self._cb_count = getattr(self, "_cb_count", 0) + 1
        if self._cb_count % 100 == 1:
            log.info("audio callback #%d status=%s indata.shape=%s peak=%.5f",
                      self._cb_count, status, indata.shape, float(np.abs(indata).max()))
        mono = indata[:, 0] if indata.ndim > 1 else indata
        if not self.saw_signal and mono.any():
            self.saw_signal = True
        k = len(mono)
        if k >= self.n:
            self.buf[:] = mono[-self.n:]
        else:
            self.buf = np.roll(self.buf, -k)
            self.buf[-k:] = mono

        spec = np.abs(np.fft.rfft(self.buf * self.window)) / self.win_gain
        power = spec * spec
        lo_db, hi_db = float(self.cfg["min_db"]), float(self.cfg["max_db"])

        band_power = self.band_matrix @ power.astype(np.float32)
        band_db = 10.0 * np.log10(band_power + 1e-12)
        new = np.clip((band_db - lo_db) / (hi_db - lo_db), 0.0, 1.0).astype(np.float32)

        total_db = 10.0 * np.log10(float(power[self.in_range].sum()) + 1e-12)
        loud = min(1.0, max(0.0, (total_db - lo_db) / (hi_db - lo_db)))

        with self.lock:
            # "Sound present" adapts to the room: above the noise floor by a margin,
            # so no fixed dB threshold is needed. The floor is learned only while
            # the gate is closed (room noise), never from the sound that opened it
            # -- otherwise a sustained note would slowly become the new "floor"
            # and close the gate on itself.
            if not self.gate_open:
                self._noise_floor = min(loud, self._noise_floor + (loud - self._noise_floor) * self.lo_rate)
            present = loud > self._noise_floor + GATE_PRESENCE_MARGIN
            now = time.monotonic()
            if self.gate_ms <= 0:
                gate_open = True
            elif present:
                if self._present_since is None:
                    self._present_since = now
                gate_open = (now - self._present_since) * 1000.0 >= self.gate_ms
            else:
                self._present_since = None
                gate_open = False
            self.gate_open = gate_open
            if not gate_open:
                new[:] = 0.0  # let the release smoothing take the bars down
                loud = 0.0
            self.levels = np.maximum(new, self.levels * self.release)
            self.loudness = max(loud, self.loudness * self.release)
            if gate_open:
                # Envelopes only learn while the gate is open, so room noise never
                # becomes the new "recent range".
                self.loud_hi, self.loud_lo, self.loudness_rel = self._update_envelope(
                    self.loudness, self.loud_hi, self.loud_lo)
                # Same envelope step as _update_envelope, for all bands at once.
                lv = self.levels.astype(np.float64)
                self.band_hi = np.maximum(lv, self.band_hi * self.hi_decay)
                self.band_lo = np.minimum(lv, self.band_lo + (lv - self.band_lo) * self.lo_rate)
                span = np.maximum(self.band_hi - self.band_lo, 0.1)
                self.band_rel = np.clip((lv - self.band_lo) / span, 0.0, 1.0).astype(np.float32)
            else:
                self.loudness_rel = 0.0
                self.band_rel[:] = 0.0

    def _update_envelope(self, cur, hi, lo):
        """One step of a rolling min/max envelope: `hi` chases the recent loudest moment, `lo`
        the recent quietest, `cur` is reported as its position within [lo, hi]."""
        # hi_decay is a per-block multiplicative factor (exp(-block/tau)), NOT an
        # amount to subtract -- an earlier port wrote `hi - self.hi_decay`, which
        # collapsed hi to cur every block and made "relative" effectively
        # instantaneous (사용자 지적, 2026-09-19: "최근 몇 초를 두고 움직이는 것이지
        # 지금 순간을 체크하는게 아닐텐데?").
        hi = max(cur, hi * self.hi_decay)
        lo = min(cur, lo + (cur - lo) * self.lo_rate)
        span = max(hi - lo, 0.1)  # floor, so a flat signal does not divide by ~0
        rel = min(1.0, max(0.0, (cur - lo) / span))
        return hi, lo, rel

    # -- reads (thread-safe snapshots) -----------------------------------------
    def get_levels(self):
        with self.lock:
            return [float(x) for x in self.levels]

    def get_band_rel(self):
        with self.lock:
            return [float(x) for x in self.band_rel]

    def get_loudness(self):
        with self.lock:
            return self.loudness

    def get_loudness_rel(self):
        with self.lock:
            return self.loudness_rel


# ============================================================================
# Audio: client side (adapters read the daemon's state)
# ============================================================================
# Reader side of the audio daemon's published state (see the daemon section's module
# docstring for why capture lives in a separate process). Downsamples the daemon's
# fixed NUM_BANDS down to however many columns the connected Stream Deck actually
# has -- device-specific band count is a rendering concern, not a capture one.

RUNTIME_DIR = ROOT / ".runtime"
STATE_PATH = RUNTIME_DIR / "audio_state.json"
CONTROL_DIR = RUNTIME_DIR / "audio_control"  # one file per host adapter (see the daemon section)
# Settings from a browser instead of the Stream Deck app's own panel: some
# machines cannot draw that panel at all (an old GPU makes the app's embedded
# browser produce no picture, seen on Windows 2026-09-28), and then the settings
# are out of reach. The host serves the very same page at /pi and relays what it
# says through these two folders, so there is one settings screen, not two.
PI_BRIDGE_DIR = RUNTIME_DIR / "pi_bridge"
PI_TO_PLUGIN = PI_BRIDGE_DIR / "to_plugin"   # what the page said
PI_TO_PAGE = PI_BRIDGE_DIR / "to_page"       # what the plugin answered
PI_BRIDGE_CONTEXT = "browser-settings"       # the page's "context" over this route
STALE_AFTER_S = 2.0  # daemon publishes at 30Hz; this is generous
_STATE_READ_RETRIES = 3  # the file is being replaced right now; it is back within milliseconds


def set_daemon_active(active: bool, gate_ms: int = 0, adapter: str = "elgato"):
    """Tells the daemon section whether THIS adapter wants the mic open right now (design:
    only while a show is actually running) and its time-gate length. Each host
    adapter (elgato, companion, ...) has its own file; the daemon opens the mic
    if ANY of them is active, so two adapters running at once never fight over
    one flag and one dying cannot switch the other's mic off (2026-09-19,
    사용자 지적 "둘 다 쓴다면 충돌은 없을까?"). While active, call this again at
    least every HEARTBEAT_S: the daemon treats an older file as inactive."""
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    # Same writer as the daemon's state file: on Windows the rename is refused
    # while the reader (the daemon, every tick) has the file open, and losing
    # one heartbeat is nothing, while losing the render loop stops the show.
    write_json_atomically(CONTROL_DIR / f"{adapter}.json",
                          {"active": active, "gate_ms": int(gate_ms), "ts": time.time()})


HEARTBEAT_S = 3  # how often an adapter must re-write its control file while active (daemon.CONTROL_STALE_S = 10)


def _group_max(values, num_groups):
    """Split `values` into `num_groups` contiguous groups (as even as possible)
    and take each group's max ("loudest sub-band in the group")."""
    n = len(values)
    if num_groups >= n:
        # more output slots than source bands: repeat the last bands to fill
        return [values[min(i, n - 1)] for i in range(num_groups)]
    out = []
    for g in range(num_groups):
        lo = (g * n) // num_groups
        hi = ((g + 1) * n) // num_groups
        out.append(max(values[lo:hi]))
    return out


class AudioClient:
    """No connection/thread of its own -- just reads a small JSON file each time
    `read(cols)` is called. Cheap enough at ~10fps render rates."""

    def __init__(self, state_path: Path = STATE_PATH):
        self.state_path = state_path
        self._warned_stale = False

    def read(self, cols, mode="absolute"):
        """Returns (levels, loudness_rel) where `levels` has exactly `cols`
        entries in [0, 1]. All-zero (silence) if the daemon isn't running or its
        state is stale, so the show degrades to "quiet" rather than crashing.

        `mode="absolute"` uses the analyzer's dB-normalized level per band
        (loud room stays loud-looking, quiet room stays quiet-looking).
        `mode="relative"` uses `band_rel` instead -- each band relative to its
        own recent min/max range (사용자 지적,
        2026-09-19: "절대 미터가 아니라 상대 미터 개념이 있어") -- a quiet room
        can still fill the whole meter, since it reacts to CHANGE rather than
        absolute loudness."""
        state = self._read_state()
        if state is None:
            return [0.0] * cols, 0.0

        if not state.get("active", False):
            return [0.0] * cols, 0.0  # daemon's mic is intentionally closed right now

        age = time.time() - state.get("ts", 0)
        if age > STALE_AFTER_S:
            self._warn_once("audio daemon state is stale (%.1fs old) -- is the daemon section still running?", age)
            return [0.0] * cols, 0.0

        self._warned_stale = False
        key = "band_rel" if mode == "relative" else "levels"
        levels = _group_max(state[key], cols)
        return levels, state.get("loudness_rel", 0.0)

    def read_bass(self, mode="absolute", upper_hz=200.0):
        """Level of the low band (analyzer's low_hz .. `upper_hz`), for beat
        detection: kicks/bass live there, hi-hats and voices do not (사용자
        질문, 2026-09-19: "어느 주파수가 있을 때 바꾸는 거니?"). Uses the daemon's
        full band list, so it is independent of how many meters are on screen."""
        state = self._read_state()
        if state is None:
            return 0.0
        if not state.get("active", False) or time.time() - state.get("ts", 0) > STALE_AFTER_S:
            return 0.0
        values = state["band_rel" if mode == "relative" else "levels"]
        n = len(values)
        # bands are log-spaced from low_hz to high_hz (analyzer.DEFAULT_CONFIG)
        import math
        low_hz, high_hz = 80.0, 16000.0
        count = max(1, int(round(n * math.log(upper_hz / low_hz) / math.log(high_hz / low_hz))))
        return max(values[:count])

    def _read_state(self):
        """The daemon's latest numbers, or None if there are none yet.

        The daemon replaces this file 20 times a second. On Windows, opening it
        in that instant is refused, and a frame drawn from zeros looks like a
        show that died. So try again briefly, and failing that reuse the last
        frame that did arrive: one missed read must not black out the deck.
        """
        for attempt in range(_STATE_READ_RETRIES):
            try:
                with open(self.state_path, encoding="utf-8") as f:
                    state = json.load(f)
                self._last_state = state
                return state
            except (FileNotFoundError, PermissionError, json.JSONDecodeError, OSError):
                if attempt + 1 < _STATE_READ_RETRIES:
                    time.sleep(0.005)
        last = getattr(self, "_last_state", None)
        if last is not None and time.time() - last.get("ts", 0) <= STALE_AFTER_S:
            return last  # the file was busy; the numbers from a moment ago still describe the room
        self._warn_once("audio daemon state file missing/unreadable at %s -- is the daemon section running?", STATE_PATH)
        return None

    def _warn_once(self, msg, *args):
        if not self._warned_stale:
            log.warning(msg, *args)
            self._warned_stale = True


# ============================================================================
# Audio: the microphone daemon (mode: daemon)
# ============================================================================
# Standalone audio-capture process, run completely independently of Stream Deck
# (wrapped by Castika.DeckShow.app, registered as a launchd login agent by
# installer/daemon_app.sh).
#
# Why this is a separate process at all: mic capture
# inside anything Stream Deck.app spawned consistently read exact-zero audio (no
# error, no TCC prompt, permission shown as granted) no matter whether it was a
# bare shell launcher or a real .app bundle with NSMicrophoneUsageDescription. The
# same analyzer code run as a plain standalone process (this file) picks up real
# audio without any special handling -- so audio capture is kept out of Stream
# Deck's process tree entirely. The Elgato adapter (the plugin section)
# never touches the microphone; it only reads the JSON state file this daemon
# writes, and tells this daemon when to listen via the control file below.
#
# Always analyzes a fixed NUM_BANDS regardless of any particular Stream Deck's grid
# width -- device-specific band count belongs to the renderer (the rendering section
# lives in the adapter process), not to audio capture. Keeps this daemon reusable
# for a different device/grid later without restarting it.
#
# Mic open/close is gated by the per-adapter files in CONTROL_DIR, written by each adapter's show
# start/stop (the mic is open only while a show is on) -- this process stays running at
# all times (that's the whole point of the separate process), but the actual `sd.InputStream`
# is only open while the adapter says a show is active.

OPEN_RETRY_S = 5  # wait between attempts when the input device refuses to open
DIGITAL_SILENCE_S = 8  # all-zero samples for this long mean the OS is not giving us the microphone

_microphone_settings_shown = False


def _offer_microphone_settings() -> None:
    """Open the page that decides microphone access, once per run.

    Windows has no per-app microphone prompt at all: a desktop program that is
    not allowed simply gets nothing. On macOS the prompt comes once and a "no"
    is remembered. Either way the user cannot be asked again from here, so show
    them where the switch is."""
    global _microphone_settings_shown
    if _microphone_settings_shown:
        return
    _microphone_settings_shown = True
    try:
        if sys.platform == "win32":
            log.warning("Windows shows no microphone prompt for a program like this one. "
                        "Opening Settings > Privacy & security > Microphone: turn on "
                        "'Microphone access' and 'Let desktop apps access your microphone'.")
            os.startfile("ms-settings:privacy-microphone")  # noqa: S606 - opens the Settings page
        else:
            log.warning("Opening System Settings > Privacy & Security > Microphone: "
                        "Castika.DeckShow needs to be allowed there.")
            import subprocess
            subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"])
    except Exception as e:  # noqa: BLE001
        log.warning("could not open the microphone settings page (%s)", e)
NUM_BANDS = 64  # generic/fine-grained; the adapter downsamples to however many meters it
# currently needs -- up to MAX_BARS_PER_KEY (14) per physical column, so a 3-column Mini can
# ask for 42 and an 8-column XL for 112; anything past this count would repeat bands
# (사용자 지적 2026-09-19: 14개 설정 시 30개로는 부족해 세 번째 열 대역이 같은 값으로 복제됨)
# RUNTIME_DIR / STATE_PATH / CONTROL_DIR: see the audio client section above.
# An adapter file whose `ts` is older than this counts as inactive: the adapter
# refreshes it every HEARTBEAT_S while a show runs (the audio client section), so a plugin that
# dies without saying stop (Stream Deck app quit mid-show, 2026-09-20 11:22:55,
# left the mic open) closes the mic within seconds, not the hour it used to be.
CONTROL_STALE_S = 10
TICK_HZ = 20  # publish rate while the mic is open (the adapters draw at 10 fps; 20 keeps latency under 50 ms)
IDLE_CHECK_S = 0.25  # control-file check interval while the mic is closed (a start is noticed within 0.25 s)


_REPLACE_RETRIES = 12  # ~120 ms in total, far less than the 50 ms between two writes matters
_replace_denied_reported = False


def write_json_atomically(path: Path, data: dict):
    """Write the file so a reader never sees half of it.

    The rename is atomic everywhere, but on Windows it is also refused while
    another process has the target open for reading (the plugin polls this file
    while the daemon writes it 20 times a second). That is not an error worth
    losing an update over, let alone the loop that produces them: retry briefly,
    then drop this one. The next write is 50 ms away."""
    global _replace_denied_reported
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        for attempt in range(_REPLACE_RETRIES):
            try:
                os.replace(tmp_path, path)  # atomic on the same filesystem
                return
            except PermissionError:  # Windows: a reader has it open right now
                if attempt == _REPLACE_RETRIES - 1:
                    if not _replace_denied_reported:
                        log.info("%s was busy being read; dropped one update (this is normal on Windows "
                                 "and is only reported once)", path.name)
                        _replace_denied_reported = True
                    break
                time.sleep(0.01)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def pi_bridge_write(folder: Path, message: dict) -> None:
    """Hand one message to the other side. One file per message, so a reader that
    is not running yet (the Stream Deck app closed) finds them waiting."""
    folder.mkdir(parents=True, exist_ok=True)
    name = "%013d-%s.json" % (int(time.time() * 1000), uuid.uuid4().hex[:6])
    write_json_atomically(folder / name, message)


def pi_bridge_take(folder: Path, limit: int = 50) -> list:
    """Every message waiting in `folder`, oldest first; each is removed as it is
    read, so nobody acts on it twice."""
    out = []
    try:
        names = sorted(p for p in folder.iterdir() if p.suffix == ".json" and not p.name.startswith(".tmp_"))
    except FileNotFoundError:
        return out
    for path in names[:limit]:
        try:
            with open(path, encoding="utf-8") as f:
                out.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
        try:
            path.unlink()
        except OSError:
            pass
    return out


def read_control():
    """(active, gate_ms) across all host adapters: the mic is wanted if ANY
    adapter's file says so; the gate length comes from the active one (largest
    if several). Defaults to (False, 0) on any read problem -- the safe failure
    mode is a show that stays silent, not a mic stuck open with nobody reading."""
    active, gate_ms = False, 0
    try:
        for path in CONTROL_DIR.glob("*.json"):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            if time.time() - float(data.get("ts", 0) or 0) > CONTROL_STALE_S:
                continue
            if bool(data.get("active", False)):
                active = True
                gate_ms = max(gate_ms, int(data.get("gate_ms", 0) or 0))
    except OSError:
        return False, 0
    return active, gate_ms


def daemon_main(stop_event=None):
    """The microphone loop. On its own (mode `daemon`) it ends on SIGINT/SIGTERM;
    inside the host process (mode `host`) it runs in a thread and ends when
    `stop_event` is set (signals belong to the main thread there)."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    log.info("starting, num_bands=%d, control dir=%s, state=%s", NUM_BANDS, CONTROL_DIR, STATE_PATH)
    analyzer = None  # only constructed (and its stream opened) while active

    running = True

    def stop(signum, frame):
        nonlocal running
        running = False

    if stop_event is None:
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)

    period = 1.0 / TICK_HZ
    active = False
    reported_silence = False  # so a blocked microphone is reported once per opening
    retry_after = 0.0  # monotonic time before which we do not retry opening the mic
    try:
        while running and not (stop_event is not None and stop_event.is_set()):
            wanted, gate_ms = read_control()
            if analyzer is not None and analyzer.gate_ms != gate_ms:
                analyzer.gate_ms = gate_ms
                log.info("gate_ms -> %s", gate_ms)
            if wanted and not active:
                if time.monotonic() < retry_after:
                    time.sleep(period if active else IDLE_CHECK_S)
                    continue
                log.info("show started -> opening microphone")
                analyzer = SpectrumAnalyzer(num_bands=NUM_BANDS)
                analyzer.gate_ms = gate_ms
                try:
                    analyzer.start()
                except Exception as e:  # noqa: BLE001 - PortAudio/CoreAudio errors are not typed
                    # Seen once while the Mac was going to sleep (PaMacCore 28479 ->
                    # PaErrorCode -9986): the device is temporarily unavailable.
                    # Crashing here took the whole daemon down (launchd restarted
                    # it); instead keep running and try again in a few seconds.
                    log.warning("microphone could not be opened (%s) -- retrying in %ss", e, OPEN_RETRY_S)
                    _offer_microphone_settings()
                    try:
                        analyzer.stop()
                    except Exception:  # noqa: BLE001
                        pass
                    analyzer = None
                    retry_after = time.monotonic() + OPEN_RETRY_S
                    time.sleep(period if active else IDLE_CHECK_S)
                    continue
                active = True
                reported_silence = False
            elif not wanted and active:
                log.info("show stopped -> closing microphone")
                analyzer.stop()
                analyzer = None
                active = False
                write_json_atomically(STATE_PATH, {"ts": time.time(), "active": False})

            if active and not analyzer.saw_signal and time.monotonic() - analyzer.opened_at > DIGITAL_SILENCE_S:
                # Every sample has been exactly zero since the microphone opened.
                # A room that is merely quiet still carries the converter's own
                # noise, so this is the OS handing us silence: blocked, or muted.
                if not reported_silence:
                    log.warning("the microphone has delivered nothing but digital silence for %ss: "
                                "it is blocked or muted, not quiet", DIGITAL_SILENCE_S)
                    reported_silence = True
                    _offer_microphone_settings()

            if active:
                try:
                    write_json_atomically(STATE_PATH, {
                        "ts": time.time(),
                        "active": True,
                        "levels": analyzer.get_levels(),
                        "band_rel": analyzer.get_band_rel(),
                        "gate_open": bool(analyzer.gate_open),
                        "loudness": float(analyzer.get_loudness()),
                        "loudness_rel": float(analyzer.get_loudness_rel()),
                    })
                except Exception as e:  # noqa: BLE001
                    # One bad write must never end the loop: the show would
                    # freeze on the last frame with no sign of why.
                    log.warning("could not publish the audio state (%s); continuing", e)
            time.sleep(period if active else IDLE_CHECK_S)
    finally:
        if analyzer:
            analyzer.stop()
        log.info("stopped")


# ============================================================================
# Stream Deck: WebSocket protocol
# ============================================================================
# Minimal client for the Stream Deck plugin WebSocket protocol.
# Language-agnostic wire format: the app launches CodePath with
# -port/-pluginUUID/-registerEvent/-info, the plugin connects to that port and
# sends one registration message, then exchanges plain JSON events/commands.
#
# Deliberately small and dependency-light (just `websockets`) so it can be read
# top to bottom; this is the "adapter" layer, not the core show logic.

def parse_launch_args(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("-port", type=int, required=True)
    parser.add_argument("-pluginUUID", type=str, required=True)
    parser.add_argument("-registerEvent", type=str, required=True)
    parser.add_argument("-info", type=str, required=True)
    ns, _unknown = parser.parse_known_args(argv)
    return {
        "port": ns.port,
        "plugin_uuid": ns.pluginUUID,
        "register_event": ns.registerEvent,
        "info": json.loads(ns.info),
    }


class StreamDeckConnection:
    """Thin wrapper: connect, register, dispatch incoming events, send commands.

    `on_event(event_name, message)` is called for every incoming message; the
    caller (the plugin section) owns all actual behavior. Outgoing commands are sent via
    `send()`, which is safe to call from the asyncio loop's own thread only:
    background threads (e.g. the sounddevice audio callback) must hand off via
    `call_soon_threadsafe` (see the plugin section's mic probe for the pattern).
    """

    def __init__(self, launch_args, on_event, on_connected=None):
        self.launch_args = launch_args
        self.on_event = on_event
        self.on_connected = on_connected  # optional async callback, run once ws is usable
        self.ws = None

    async def run(self):
        uri = f"ws://127.0.0.1:{self.launch_args['port']}"
        async with websockets.connect(uri) as ws:
            self.ws = ws
            await self._register()
            log.info("registered with Stream Deck, entering event loop")
            if self.on_connected is not None:
                await self.on_connected()
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    log.warning("non-JSON message ignored: %r", raw)
                    continue
                event = msg.get("event")
                try:
                    await self.on_event(event, msg)
                except Exception:
                    log.exception("handler for event %r raised", event)

    async def _register(self):
        await self.ws.send(json.dumps({
            "event": self.launch_args["register_event"],
            "uuid": self.launch_args["plugin_uuid"],
        }))

    async def send(self, payload):
        if payload.get("context") == PI_BRIDGE_CONTEXT:
            pi_bridge_write(PI_TO_PAGE, payload)  # a browser is asking, not the app's panel
            return
        await self.ws.send(json.dumps(payload))

    async def log_message(self, message):
        await self.send({"event": "logMessage", "payload": {"message": message}})

    async def set_image(self, context, image_data_url, *, state=0, target=0):
        await self.send({
            "context": context,
            "event": "setImage",
            "payload": {"image": image_data_url, "state": state, "target": target},
        })

    async def switch_to_profile(self, device, profile=None, page=None):
        payload = {}
        if profile is not None:
            payload["profile"] = profile
        if page is not None:
            payload["page"] = page
        await self.send({
            "context": self.launch_args["plugin_uuid"],
            "device": device,
            "event": "switchToProfile",
            "payload": payload,
        })

    async def get_global_settings(self):
        """Response arrives later as a "didReceiveGlobalSettings" event, not a
        return value here -- the caller's on_event handler must look for it."""
        await self.send({"event": "getGlobalSettings", "context": self.launch_args["plugin_uuid"]})

    async def set_global_settings(self, settings: dict):
        await self.send({
            "event": "setGlobalSettings",
            "context": self.launch_args["plugin_uuid"],
            "payload": settings,
        })


# ============================================================================
# Stream Deck: the plugin (mode: plugin)
# ============================================================================
# Elgato adapter entry point : after the configured
# idle threshold with no system-wide keyboard/mouse input (the idle section),
# runs a live audio-reactive show on every connected Stream Deck: on the buttons the
# user placed if the current page has any, otherwise by switching that device to
# the bundled profile for its type (manifest.json Profiles[], generated by
# dev/make_profiles.py); any button press stops the show and lets Stream Deck return to
# whatever profile was active before (switchToProfile with no profile field).
#
# This adapter is "display only" -- it never touches the microphone. Audio capture
# + analysis runs in the daemon section (wrapped by Castika.DeckShow.app, a launchd
# login agent -- see installer/daemon_app.sh), a process kept completely outside Stream
# Deck's process tree because mic capture inside anything Stream Deck.app spawned
# consistently read exact-zero audio for reasons that were never fully root-caused
# a plain standalone process reads real audio with zero
# special handling. The core engine stays host-agnostic: whichever host app is running (Elgato's own, or Companion
# later) should only ever be responsible for display, never for listening.

LEVEL_MODE_ABSOLUTE = "absolute"
LEVEL_MODE_RELATIVE = "relative"  # each band relative to its recent min/max range

# Stream Deck does not capture a plugin's own stdout/stderr anywhere retrievable
# (confirmed empirically: ~/Library/Logs/ElgatoStreamDeck/<uuid>N.log only ever
# receives explicit logMessage SDK calls, not raw process output) -- without a
# file handler here, every log.info() call in this file is unverifiable, and
# this project checks what it does by its logs, never by looking at the screen.
# logs/plugin.log mirrors logs/deckshow.log's pattern.

# RENDER_FPS: defined once at the top of the file
# Native button image size per device type (manifest DeviceType enum): drawing at
# the device's own resolution avoids the app rescaling thin 1-2px slats into a
# blur (사용자 지적, 2026-09-19: "이 장치의 픽셀수를 잘 계산해"). Values from
# node-elgato-stream-deck's models/definitions.ts (the library Bitfocus Companion
# drives the hardware with), 2026-09-20: Original/MK.2 72, Mini 80, XL 96,
# Plus 120, Neo 96, Galleon K100 160, Plus XL 112. Studio (10) is left out: its
# keys are 144x112, not square, which key_image_bytes does not draw. Virtual
# Stream Deck (11) is on-screen with no native size; 96 keeps slats crisp.
KEY_IMAGE_SIZE_BY_DEVICE_TYPE = {0: 72, 1: 80, 2: 96, 3: 72, 7: 120, 9: 96, 11: 96, 12: 160, 13: 112}
DEFAULT_KEY_IMAGE_SIZE = 72
DEFAULT_PEAK_HOLD = True  # the peak line is on unless the user turns it off
DEFAULT_IDLE_THRESHOLD_MINUTES = 1  # overridden by ui/inspector.html's saved global setting
DEFAULT_STOP_ON_ACTIVITY = True  # typing or moving the mouse ends the show, like a screen saver
IDLE_POLL_INTERVAL_S = 2
ACTIVE_POLL_INTERVAL_S = 0.5  # while the show runs: how soon activity is noticed
ACTIVITY_MARGIN_S = 1.0  # the idle timer must fall this far behind the show's age to count as input
PI_BRIDGE_POLL_S = 0.5  # how often the plugin looks for what the browser settings page said
# No duration cap on a show (사용자 지적, 2026-09-19: "상한을 두지마. 본인이 끄지
# 않고 간걸") -- it runs until a button is pressed, or until the keyboard or
# mouse is used while "Stop on use" is on (2026-09-29). The one case that is stopped
# automatically is the Folder trap: if the device is inside a Folder when idle
# triggers, switchToProfile does not pull it out (confirmed on the Mini), our
# buttons never appear, and nothing could ever be pressed to stop the mic -- so a
# show with no buttons visible NO_KEYS_GRACE_S after starting is ended.
NO_KEYS_GRACE_S = 5
# The show button is a toggle (사용자 확정, 2026-09-19): short press starts the show
# when it is not running (even while OFF for the session) and stops it when it
# is; a long press on any button turns the show OFF for the session -- stops it and
# disables auto-start. OFF is for the current session only: it is cleared on the
# next boot (plugin start) and on wake from sleep (systemDidWakeUp) when Auto
# Restart is on, or by the PI's "This deck: On".
LONG_PRESS_S = 1.5
# CYCLE_PHASE_S: defined once in the GridShow section
# Start mode (사용자 정의, 2026-09-19: "시작 모드: 항상 / 대기"): both modes START
# only on system idle. ALWAYS restarts at every idle period even after a button
# stopped it; STANDBY runs once per session -- after a button stops it, it stays
# stopped until the next boot / wake (Auto Restart, "자동 다시 시작") or the PI's
# "This deck: On". (An earlier reading, "run immediately whenever enabled", was wrong:
# 사용자 지적 "시작 조건이잖아. 항상 감시를 하는 거냐?")
START_MODE_STANDBY = "standby"
START_MODE_ALWAYS = "always"
# 2026-09-19 최종 확정 (공식 문서 원문으로 재확인):
# - "Plugins may only switch to profiles distributed with the plugin, as defined
#   within the manifest, and cannot access user-defined profiles." -> "Default
#   Profile"(사용자가 만든 프로필)로의 전환은 애초에 불가능했다. 이전에 성공한 것
#   처럼 보였던 건 사용자가 그 직전에 손으로 이미 그 페이지로 이동해 둔 것과 우연히
#   타이밍이 맞았을 뿐, 우리 switchToProfile 호출 자체는 조용히 실패했을 것이다.
# - 대신 manifest.json의 Profiles 배열에 우리가 직접 번들로 넣은 프로필(DeckShow.
#   streamDeckProfile, DeviceType=1=Mini)로는 전환이 된다 -- "번들된" 프로필이라
#   위 제약에 걸리지 않는다.
# - 복귀는 profile 필드를 아예 생략한다: 공식 문서 원문 "When not specified, Stream
#   Deck will switch to the previous profile." 이건 우리가 무엇이었는지 몰라도(3.2
#   절의 "현재 프로필을 읽을 수 없다"는 제약과 무관하게) Stream Deck 자신이 알아서
#   돌려주는, 진짜 문서화된 동작이다 -- 이전의 "Default Profile로 명시 복귀" 시도는
#   틀린 방향이었다.
# Bundled show profiles, one per DeviceType, generated by dev/make_profiles.py and
# listed in manifest.json Profiles[] -- read from there so the names can never
# drift apart. A device whose type has no entry can only run the show in place.
_MANIFEST_FILE = ROOT / "streamdeck" / "com.castika.deckshow.sdPlugin" / "manifest.json"


def _load_show_profiles():
    import json
    try:
        return {p["DeviceType"]: p["Name"] for p in json.loads(_MANIFEST_FILE.read_text(encoding="utf-8")).get("Profiles", [])}
    except (OSError, ValueError, KeyError) as e:
        log.warning("could not read Profiles[] from %s: %s", _MANIFEST_FILE, e)
        return {}


SHOW_PROFILES_BY_DEVICE_TYPE = _load_show_profiles()

# Defaults = the values the user settled on during the 2026-09-19 tuning session
# ("지금 지정값을 기본값으로 저장해"); overridden by ui/inspector.html's saved
# global settings via _apply_global_settings. ui/inspector.html's initial control
# values must match these.
DEFAULT_COLOR_SCHEME = COLOR_SCHEME_TIER
DEFAULT_SEGMENT = SEGMENT_ICONS  # 사용자 확정 2026-09-19: 기본 Icons
DEFAULT_BARS_PER_KEY = 4  # "Bars" in the PI
DEFAULT_LEVEL_MODE = LEVEL_MODE_RELATIVE
# Default text for the icon show, shown in order one character per beat (사용자
# 제공 가사, 2026-09-19); the PI's Glyphs field starts with the same text.
_DEFAULT_GLYPHS_FILE = ROOT / "streamdeck" / "com.castika.deckshow.sdPlugin" / "default_glyphs.txt"
DEFAULT_GLYPHS = _DEFAULT_GLYPHS_FILE.read_text(encoding="utf-8") if _DEFAULT_GLYPHS_FILE.exists() else ""
DEFAULT_SENSITIVITY = 1.0  # gain applied to every band level (0.5x .. 4x), "Sensitivity" in the PI
DEFAULT_GATE_MS = 0  # time gate: sound must last this long before it shows (0 = off), "Gate" in the PI


def _data_url(png_bytes):
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


def _parse_hex_color(hex_str):
    """"#rrggbb" (ui/inspector.html's <input type=color> value) -> (r, g, b), or
    None if malformed -- callers keep the previous color rather than crash on a
    bad setting."""
    if not isinstance(hex_str, str) or len(hex_str) != 7 or hex_str[0] != "#":
        return None
    try:
        return tuple(int(hex_str[i:i + 2], 16) for i in (1, 3, 5))
    except ValueError:
        return None


class DeviceState:
    """One connected Stream Deck (2026-09-20, 사용자 지적 "동일한 미니를 2개 가지고
    있는 사용자라면?"): the app reports every device separately (-info devices[],
    deviceDidConnect), each with its own id, type and grid, and our keys arrive
    per device -- so key tracking, the renderer and the profile switch are all
    per device. The grid is whatever the app says NOW (a Virtual Stream Deck is
    resized at will), never a table lookup ("격자를 매번 읽어야 한다")."""

    def __init__(self, info):
        self.id = info["id"]
        self.name = info.get("name", "?")
        self.type = None
        self.size = None  # (columns, rows) as reported by the app
        self.key_size = DEFAULT_KEY_IMAGE_SIZE
        self.update(info)
        self.contexts_by_coord = {}  # (row, col) -> context, keys visible on this device now
        self.rows = None  # bounding box of contexts_by_coord
        self.cols = None
        self.renderer = None
        self.icon_show = None  # IconShow, built lazily for SEGMENT_ICONS
        self.last_cell_color = {}  # (row, col) -> last cells sent, for dedup
        self.switched_profile = False  # this show switched this device to our profile
        self.enabled = True  # per-deck "This deck: On / Off" (ui/inspector.html on a key of this deck, or a long press on it)
        # -info lists every deck the app KNOWS, including one another program
        # (Companion) currently holds; only decks that send deviceDidConnect are
        # really attached (2026-09-20 11:24: Mini in -info, no connect while
        # Companion held it). The app sends the connect for every attached deck
        # right after registration, so this is False only until then.
        self.connected = False

    def update(self, info):
        """Re-read type/grid from a (re)connect: both can change for a virtual deck."""
        if "type" in info:
            self.type = info["type"]
            self.key_size = KEY_IMAGE_SIZE_BY_DEVICE_TYPE.get(self.type, DEFAULT_KEY_IMAGE_SIZE)
        if "name" in info:
            self.name = info["name"]
        size = info.get("size") or {}
        if "columns" in size and "rows" in size:
            self.size = (size["columns"], size["rows"])

    @property
    def show_profile(self):
        # Exact DeviceType only: switching a type-11 device to the type-1 profile
        # was silently ignored by the app (tested 2026-09-20, log 01:33:26), so a
        # profile for another type is no use.
        return SHOW_PROFILES_BY_DEVICE_TYPE.get(self.type)

    def recompute_grid(self):
        """Grid = bounding box of the buttons visible RIGHT NOW (not the largest ever
        seen): a user page with our action only on its top row must render as a
        1-row grid so each key is a whole meter, not the top half of one
        (사용자 지적, 2026-09-19: "디폴트 페이지의 1-3 위치에 두었는데 안 나오네")."""
        keys = list(self.visible_keys())
        if keys:
            self.rows = max(r for r, _ in keys) + 1
            self.cols = max(c for _, c in keys) + 1
        else:
            self.rows = self.cols = None

    def in_grid(self, row, col):
        """The app reports EVERY button of a profile, including ones outside the
        device's grid (a 9x4 bundled profile on a 3x2 Virtual Stream Deck sent
        willAppear for all 36 keys, 2026-09-20 01:34:57); only those inside the
        `size` it reported for this device are real."""
        if self.size is None:
            return True
        cols, rows = self.size
        return row < rows and col < cols

    def visible_keys(self):
        return (k for k in self.contexts_by_coord if self.in_grid(*k))

    def __str__(self):
        grid = "%sx%s" % self.size if self.size else "?"
        return f"{self.name}[{self.id[:8]} type={self.type} grid={grid}]"


class Plugin:
    def __init__(self, conn: StreamDeckConnection, loop: asyncio.AbstractEventLoop, devices_info):
        self.conn = conn
        self.loop = loop
        self.devices = {}  # device id -> DeviceState
        self.disabled_devices = set()  # ui/inspector.html "This device": ids of decks left out of the show
        for info in devices_info:
            self._upsert_device(info)
        self.running = False
        self.preinstalling = False  # _preinstall_profiles in progress: idle auto-start waits
        self._idle_start_pending = False  # idle came while something blocked the start; do it as soon as it can
        self.audio = AudioClient()
        self._render_task = None
        self._png_cache = {}  # (cells, size) -> png data-url, cleared every frame (팔레트 캐시)
        self.idle_watcher = IdleWatcher(DEFAULT_IDLE_THRESHOLD_MINUTES * 60)
        self.color_scheme = DEFAULT_COLOR_SCHEME
        self.peak_hold = DEFAULT_PEAK_HOLD
        self.stop_on_activity = DEFAULT_STOP_ON_ACTIVITY
        self._show_started_at = 0.0
        self.segment = DEFAULT_SEGMENT
        # Style "Random" (SEGMENT_CYCLE, 사용자 확정 2026-09-19): phases in order --
        # 0: Text with the built-in text (its own text x2 + same-size random block,
        # paced by characters), 1: Stripe for CYCLE_PHASE_S, 2: Icons (Bootstrap,
        # random) for CYCLE_PHASE_S, then back to 0. Time, not counts, for the
        # last two: a quiet room would otherwise never finish them.
        self.cycle_phase = 0
        self.cycle_phase_started = None
        self.icon_font_path = ""  # ui/inspector.html's Icon font (empty = bundled Bootstrap Icons)
        self.icon_glyphs = ""  # ui/inspector.html's Text (empty = the font's built-in text, if it has one)
        self.bars_per_key = DEFAULT_BARS_PER_KEY
        self.palette = DEFAULT_PALETTE
        self.rotation = 0  # degrees, one of 0/90/180/270 -- ui/inspector.html's rotationDegrees
        self.level_mode = DEFAULT_LEVEL_MODE  # ui/inspector.html's levelMode
        self.auto_restart = True  # ui/inspector.html's "Auto Restart"
        self.start_mode = START_MODE_ALWAYS  # ui/inspector.html's "Start"
        self._raw_settings = {}  # last didReceiveGlobalSettings payload, re-sent whole on our own writes
        self._long_press_task = None
        self.sensitivity = DEFAULT_SENSITIVITY
        self.gate_ms = DEFAULT_GATE_MS

    async def on_event(self, event, msg):
        if event == "willAppear":
            self._remember_key(msg)
        elif event == "willDisappear":
            self._forget_key(msg)
        elif event == "didReceiveGlobalSettings":
            self._apply_global_settings(msg.get("payload", {}).get("settings", {}))
        elif event == "keyDown":
            # Show button = toggle (사용자 확정, 2026-09-19): short press starts the show
            # when it is not running (even while OFF for the session) and stops it
            # when it is; a long press (any button) turns the show OFF for the
            # session. Decided on keyUp / when the long-press timer fires.
            if self._long_press_task is None:
                self._long_press_device = msg.get("device")
                self._long_press_task = asyncio.create_task(self._long_press_timer())
        elif event == "keyUp":
            task, self._long_press_task = self._long_press_task, None
            if task is not None and not task.done():
                task.cancel()
                if self.running:
                    if self.start_mode == START_MODE_STANDBY:
                        log.info("short press in Standby mode -> stopped; auto-start stays off until boot/wake or PI On")
                        await self._set_enabled(False)
                    else:
                        log.info("short press -> stopping show (restarts at the next idle period)")
                    await self._stop()
                else:
                    log.info("short press while not running -> starting show")
                    ds = self.devices.get(msg.get("device"))
                    if ds is not None and not ds.enabled:
                        await self._set_enabled(True, ds.id)  # a quick press turns a deck back on
                    await self._start()
        elif event == "sendToPlugin":
            command = msg.get("payload", {}).get("command")
            if command == "listFonts":
                await self._send_font_list(msg.get("context"))
            elif command == "whichDevice":
                ds = self.devices.get(msg.get("device"))
                await self.conn.send({"event": "sendToPropertyInspector", "context": msg.get("context"),
                                      "payload": {"deviceName": ds.name if ds else "this deck"}})

            elif command == "openFontsFolder":
                USER_FONTS_DIR.mkdir(parents=True, exist_ok=True)
                reveal_in_file_manager(USER_FONTS_DIR)
                await self._send_font_list(msg.get("context"))
            if command == "startNow":
                if self.running:
                    log.info("Start Now from the PI: already running")
                else:
                    # Asking for the show is asking for this deck to be in it,
                    # exactly like a quick press on one of its buttons. Without
                    # this, Start Now was silently skipped on a deck a long
                    # press had switched off (2026-09-29).
                    ds = self.devices.get(msg.get("device"))
                    if ds is not None and not ds.enabled:
                        log.info("Start Now on a deck that is off -> turning it back on")
                        await self._set_enabled(True, ds.id)
                    elif ds is None and not self.enabled:
                        # The browser settings page belongs to no deck: with every
                        # deck off there would be nothing to draw on.
                        log.info("Start Now with every deck off -> turning them all back on")
                        await self._set_enabled(True)
                    log.info("Start Now from the PI -> starting show")
                    await self._start()
        elif event == "deviceDidConnect":
            # Also sent when the app re-attaches a device after Mac display sleep
            # (session suspend, seen in the app log 2026-09-19), and when a
            # Virtual Stream Deck is resized: re-read the grid, repaint everything.
            info = dict(msg.get("payload", {}).get("deviceInfo", {}))
            info["id"] = msg.get("device")
            ds = self._upsert_device(info)
            ds.connected = True
            ds.last_cell_color.clear()
            log.info("deviceDidConnect: %s -> repaint", ds)
        elif event == "deviceDidDisconnect":
            ds = self.devices.get(msg.get("device"))
            if ds is not None:
                ds.connected = False
                ds.last_cell_color.clear()
                log.info("deviceDidDisconnect: %s", ds)
        elif event == "systemDidWakeUp":
            if not self.enabled and self.auto_restart:
                log.info("system woke up -> show back ON (Auto Restart)")
                await self._set_enabled(True)

    @property
    def enabled(self):
        """In the show on at least one deck. The setting is per deck (ui/inspector.html's
        Show applies to the deck the key sits on; a long press turns off the deck
        that was pressed) -- 사용자 확정 2026-09-20 "show on off를 이용하자"."""
        return any(ds.enabled for ds in self.devices.values()) if self.devices else True

    def _upsert_device(self, info):
        ds = self.devices.get(info.get("id"))
        if ds is None:
            ds = DeviceState(info)
            ds.enabled = ds.id not in self.disabled_devices
            self.devices[ds.id] = ds
            log.info("device: %s (show profile: %s)", ds, ds.show_profile or "none for this type -> in place only")
        else:
            ds.update(info)
        return ds

    def _device_for(self, msg):
        """The DeviceState an action event belongs to (every willAppear/keyDown
        carries the device id). Unknown id: register it with what we have."""
        return self._upsert_device({"id": msg.get("device")})

    def _font_choices(self):
        return font_choices()

    async def _send_font_list(self, pi_context):
        await self.conn.send({"event": "sendToPropertyInspector", "context": pi_context,
                              "payload": {"fonts": self._font_choices(), "folder": str(USER_FONTS_DIR)}})

    async def _long_press_timer(self):
        try:
            await asyncio.sleep(LONG_PRESS_S)
        except asyncio.CancelledError:
            return
        log.info("long press -> show OFF on %s for this session", self._long_press_device and self._long_press_device[:8])
        await self._set_enabled(False, self._long_press_device)
        if self.running:
            await self._stop()

    async def _set_enabled(self, enabled, device_id=None):
        """In the show / not for one deck (device_id) or every deck (None); written to
        the global settings' disabledDevices so the PI's Show radio stays in sync."""
        ids = [device_id] if device_id else list(self.devices)
        for did in ids:
            if enabled:
                self.disabled_devices.discard(did)
            else:
                self.disabled_devices.add(did)
        self._apply_disabled_devices()
        self._raw_settings["disabledDevices"] = sorted(self.disabled_devices)
        await self.conn.set_global_settings(self._raw_settings)

    def _apply_disabled_devices(self):
        for ds in self.devices.values():
            was, ds.enabled = ds.enabled, ds.id not in self.disabled_devices
            if was and not ds.enabled and self.running:
                # switched off mid-show: hand its buttons back now, not at stop
                ds.last_cell_color.clear()
                asyncio.create_task(self._blank_device(ds))
                if ds.switched_profile:
                    ds.switched_profile = False
                    asyncio.create_task(self.conn.switch_to_profile(ds.id, profile=None))

    async def pi_bridge_loop(self):
        """The settings page opened in a browser talks through files (the host
        serves it at /pi). Everything it sends is handled exactly like the same
        message from the app's own panel, so there is one implementation."""
        while True:
            await asyncio.sleep(PI_BRIDGE_POLL_S)
            for msg in pi_bridge_take(PI_TO_PLUGIN):
                event = msg.get("event")
                try:
                    if event == "getGlobalSettings":
                        pi_bridge_write(PI_TO_PAGE, {"event": "didReceiveGlobalSettings",
                                                     "payload": {"settings": dict(self._raw_settings)}})
                    elif event == "setGlobalSettings":
                        # Merge, never replace: the app keeps ONE settings object
                        # for the plugin, so a sender that names two keys must not
                        # wipe the rest (the panel always sends the whole object,
                        # but nothing guarantees that of every caller).
                        changed = msg.get("payload") or {}
                        settings = dict(self._raw_settings)
                        settings.update(changed)
                        log.info("settings changed from the browser page (%d value(s))", len(changed))
                        self._apply_global_settings(settings)
                        await self.conn.set_global_settings(settings)
                    elif event == "sendToPlugin":
                        await self.on_event("sendToPlugin", {"context": PI_BRIDGE_CONTEXT,
                                                             "device": next(iter(self.devices), None),
                                                             "payload": msg.get("payload") or {}})
                except Exception as e:  # noqa: BLE001 - a bad message must not end the loop
                    log.warning("browser settings page: could not handle %s (%s)", event, e)

    async def idle_poll_loop(self):
        while True:
            await asyncio.sleep(ACTIVE_POLL_INTERVAL_S if self.running else IDLE_POLL_INTERVAL_S)
            edge = self.idle_watcher.poll()
            if edge == "became_idle":
                # Remember the start instead of doing it here: the reason may be
                # busy right now (the profile install runs at plugin start, and
                # it takes seconds). The edge comes once per idle period, so a
                # start dropped here would never happen -- the machine is
                # already idle and nothing will say "became_idle" again.
                self._idle_start_pending = True
            elif edge == "became_active":
                self._idle_start_pending = False  # this idle period is over

            if self.running and self.stop_on_activity and user_active_since(self._show_started_at):
                log.info("keyboard or mouse used -> stopping show")
                await self._stop()

            if self._idle_start_pending and not self.running and self.enabled and not self.preinstalling:
                self._idle_start_pending = False
                log.info("system idle >= %ss -> auto-starting show", self.idle_watcher.threshold_s)
                await self._start()

    def _invalidate(self, redraw=False, icons=False, bars=False):
        """A setting changed: drop the per-device caches it affects so the next
        frame rebuilds/redraws on every device."""
        for ds in self.devices.values():
            if icons:
                ds.icon_show = None
            if bars:
                ds.renderer = None
            if redraw or icons or bars:
                ds.last_cell_color.clear()

    def _apply_global_settings(self, settings):
        self._raw_settings = dict(settings)
        auto_restart = settings.get("autoRestart")
        if isinstance(auto_restart, bool) and auto_restart != self.auto_restart:
            self.auto_restart = auto_restart
            log.info("applied setting from ui/inspector.html: autoRestart = %s", auto_restart)

        start_mode = settings.get("startMode")
        if start_mode in (START_MODE_STANDBY, START_MODE_ALWAYS) and start_mode != self.start_mode:
            self.start_mode = start_mode
            log.info("applied setting from ui/inspector.html: startMode = %s", start_mode)

        disabled = settings.get("disabledDevices")
        if isinstance(disabled, list) and set(disabled) != self.disabled_devices:
            if disabled and getattr(self, "_clear_off_on_start", False) and self.auto_restart:
                log.info("Show OFF left over from the previous session on %d deck(s) -> cleared on start (Auto Restart)", len(disabled))
                asyncio.create_task(self._set_enabled(True))
            else:
                self.disabled_devices = {d for d in disabled if isinstance(d, str)}
                self._apply_disabled_devices()
                log.info("applied setting from ui/inspector.html: Show OFF on %s", [d[:8] for d in self.disabled_devices] or "no deck")
        self._clear_off_on_start = False

        minutes = settings.get("idleThresholdMinutes")
        if isinstance(minutes, (int, float)) and minutes > 0:
            self.idle_watcher.threshold_s = minutes * 60
            log.info("applied setting from ui/inspector.html: idle threshold = %s minutes", minutes)

        peak = settings.get("peakHold")
        if isinstance(peak, bool) and peak != self.peak_hold:
            self.peak_hold = peak
            self._invalidate(redraw=True)
            log.info("applied setting from ui/inspector.html: peakHold = %s", peak)

        stop_on_activity = settings.get("stopOnActivity")
        if isinstance(stop_on_activity, bool) and stop_on_activity != self.stop_on_activity:
            self.stop_on_activity = stop_on_activity
            log.info("applied setting from ui/inspector.html: stopOnActivity = %s", stop_on_activity)

        scheme = settings.get("colorScheme")
        if scheme in (COLOR_SCHEME_TONE, COLOR_SCHEME_TIER) and scheme != self.color_scheme:
            self.color_scheme = scheme
            self._invalidate(redraw=True)
            log.info("applied setting from ui/inspector.html: colorScheme = %s", scheme)

        font_path = settings.get("iconFontPath")
        glyphs = settings.get("iconGlyphs")
        if isinstance(font_path, str):
            font_path = str(Path(font_path.strip()).expanduser()) if font_path.strip() else ""
            for bundled in (BUNDLED_TEXT_FONT, BUNDLED_ICON_FONT):  # user-folder copy of a bundled font -> the bundled one
                if font_path and Path(font_path).name.lower() == Path(bundled).name.lower():
                    font_path = bundled
        if isinstance(font_path, str) and font_path != self.icon_font_path:
            self.icon_font_path = font_path
            self._invalidate(icons=True)
            log.info("applied setting from ui/inspector.html: iconFontPath = %r", self.icon_font_path)
        if isinstance(glyphs, str) and glyphs != self.icon_glyphs:
            self.icon_glyphs = glyphs
            self._invalidate(icons=True)
            log.info("applied setting from ui/inspector.html: iconGlyphs = %d chars", len(glyphs))

        segment = settings.get("segmentStyle")
        if segment in (SEGMENT_SOLID, SEGMENT_BAR, SEGMENT_ICONS, SEGMENT_CYCLE) and segment != self.segment:
            self.cycle_phase = 0
            self.cycle_phase_started = None
            self._invalidate(icons=True)
            self.segment = segment
            self._invalidate(redraw=True)
            log.info("applied setting from ui/inspector.html: segmentStyle = %s", segment)

        bars = settings.get("barsPerKey")
        if isinstance(bars, (int, float)) and 1 <= bars <= MAX_BARS_PER_KEY and int(bars) != self.bars_per_key:
            self.bars_per_key = int(bars)
            self._invalidate(bars=True)
            self._invalidate(redraw=True)
            log.info("applied setting from ui/inspector.html: barsPerKey = %s", bars)

        low = _parse_hex_color(settings.get("colorLow")) or self.palette[0]
        mid = _parse_hex_color(settings.get("colorMid")) or self.palette[1]
        high = _parse_hex_color(settings.get("colorHigh")) or self.palette[2]
        if (low, mid, high) != self.palette:
            self.palette = (low, mid, high)
            self._invalidate(redraw=True)
            log.info("applied setting from ui/inspector.html: palette = %s", self.palette)

        rotation = settings.get("rotationDegrees")
        if rotation in (0, 90, 180, 270) and rotation != self.rotation:
            self.rotation = rotation
            self._invalidate(bars=True)
            self._invalidate(redraw=True)
            log.info("applied setting from ui/inspector.html: rotationDegrees = %s", rotation)

        sens = settings.get("sensitivity")
        if isinstance(sens, (int, float)) and 0.1 <= sens <= 10 and float(sens) != self.sensitivity:
            self.sensitivity = float(sens)
            log.info("applied setting from ui/inspector.html: sensitivity = %sx", sens)

        gate_ms = settings.get("gateMs")
        if isinstance(gate_ms, (int, float)) and 0 <= gate_ms <= 5000 and int(gate_ms) != self.gate_ms:
            self.gate_ms = int(gate_ms)
            if self.running:
                set_daemon_active(True, gate_ms=self.gate_ms)  # the gate lives in the daemon's analyzer
            log.info("applied setting from ui/inspector.html: gateMs = %s", gate_ms)

        level_mode = settings.get("levelMode")
        if level_mode in (LEVEL_MODE_ABSOLUTE, LEVEL_MODE_RELATIVE) and level_mode != self.level_mode:
            self.level_mode = level_mode
            log.info("applied setting from ui/inspector.html: levelMode = %s", level_mode)

    def _remember_key(self, msg):
        payload = msg.get("payload", {})
        coords = payload.get("coordinates", {})
        row, col = coords.get("row"), coords.get("column")
        if row is None or col is None:
            return
        ds = self._device_for(msg)
        # NOT proof of attachment: the app sends willAppear for keys of a deck it
        # knows but does not hold (Mini under Companion, 2026-09-20 11:25:30).
        ds.contexts_by_coord[(row, col)] = msg.get("context")
        ds.recompute_grid()
        if not ds.in_grid(row, col):
            return  # outside this device's grid (see DeviceState.in_grid) -- tracked for nothing
        log.info("button appeared at (row=%s, col=%s) on %s; visible grid now %sx%s, %d buttons tracked",
                  row, col, ds, ds.rows, ds.cols, sum(1 for _ in ds.visible_keys()))

    def _forget_key(self, msg):
        payload = msg.get("payload", {})
        coords = payload.get("coordinates", {})
        key = (coords.get("row"), coords.get("column"))
        ds = self._device_for(msg)
        # Only drop the button if it is the SAME instance: on a profile switch the
        # old page's willDisappear can arrive after the new page's willAppear for
        # the same coordinates, and would otherwise delete the fresh context --
        # that button then never receives another image (사용자 지적, 2026-09-19:
        # one button of the show staying black while its column was at full level).
        if ds.contexts_by_coord.get(key) == msg.get("context"):
            del ds.contexts_by_coord[key]
            ds.recompute_grid()
            log.info("button disappeared at (row=%s, col=%s) on %s", *key, ds)
        else:
            log.info("stale willDisappear at (row=%s, col=%s) on %s ignored (context already replaced)", *key, ds)

    def _any_keys(self):
        return any(ds.enabled and any(True for _ in ds.visible_keys()) for ds in self.devices.values())

    async def _preinstall_profiles(self):
        """Install the bundled profile on EVERY connected device once, at plugin
        start (사용자 지시 2026-09-20: "기기가 3대면 설치할 때 동시에 3곳에 번들을 다
        넣으라고"). The app only installs a bundled profile when the plugin first
        switches to it, and asks the user then (manifest docs: "Upon the plugin
        switching to the profile, the user will be prompted to install"), so:
        switch, wait for the profile's keys to appear (= prompt accepted), switch
        straight back, remember the device. One device at a time -- the prompts
        are modal. A device whose prompt is not answered in PREINSTALL_WAIT_S is
        retried at the next start."""
        try:
            done = set(PROFILES_INSTALLED_FILE.read_text(encoding="utf-8").split())
        except FileNotFoundError:
            done = set()
        self.preinstalling = True
        try:
            await asyncio.sleep(2)  # let the initial willAppear burst land first
            await self._preinstall_each(done)
        finally:
            self.preinstalling = False

    async def _preinstall_each(self, done):
        for ds in list(self.devices.values()):
            if ds.id in done or not ds.show_profile or self.running or not ds.enabled or not ds.connected:
                continue
            before = set(ds.contexts_by_coord.values())
            log.info("%s: installing bundled profile %r (one-time; the app may ask to confirm)", ds, ds.show_profile)
            await self.conn.switch_to_profile(ds.id, profile=ds.show_profile)
            deadline = self.loop.time() + PREINSTALL_WAIT_S
            while self.loop.time() < deadline:
                await asyncio.sleep(0.2)
                if set(ds.contexts_by_coord.values()) - before:
                    break
            else:
                log.warning("%s: no buttons from the bundled profile after %ss -- prompt declined or still open; will retry at next start",
                            ds, PREINSTALL_WAIT_S)
                continue
            await self.conn.switch_to_profile(ds.id, profile=None)
            done.add(ds.id)
            PROFILES_INSTALLED_FILE.parent.mkdir(parents=True, exist_ok=True)
            PROFILES_INSTALLED_FILE.write_text("\n".join(sorted(done)), encoding="utf-8")
            log.info("%s: bundled profile installed, back to the previous profile", ds)
            await asyncio.sleep(0.5)  # let the switch-back settle before the next device's prompt

    async def _start(self, force_switch=False):
        # Two ways to show (사용자 지적, 2026-09-19): if any of our buttons are visible
        # on the CURRENT page, run the show right there -- no profile switch, no
        # return needed, the user placed the buttons where they want the show.
        # Otherwise switch to our bundled DeckShow profile (the only profile a
        # plugin may switch to) and return to the previous one when stopped.
        # The renderer is built lazily in _render_frame once buttons are known.
        # Decided per device (2026-09-20): each connected deck either already
        # shows our buttons (in place) or is switched to the bundled profile for
        # its type; one with neither stays as it is.
        if not any(ds.connected and ds.enabled for ds in self.devices.values()):
            # No deck attached to this host (e.g. Companion holds it) or all are
            # Show Off: nothing to draw on, so do not even open the mic
            # (2026-09-19, 두 호스트 동시 사용 시 충돌 방지).
            log.info("start skipped: no attached deck in the show (%s)",
                     ", ".join(f"{ds.name}:{'on' if ds.enabled else 'off'}/{'attached' if ds.connected else 'absent'}" for ds in self.devices.values()) or "no devices")
            return
        switched = []
        for ds in self.devices.values():
            ds.renderer = None
            ds.icon_show = None
            ds.last_cell_color.clear()
            if not ds.enabled or not ds.connected:
                ds.switched_profile = False
                log.info("%s: %s -> left alone", ds, "Show Off" if not ds.enabled else "not attached (another program holds it?)")
                continue
            if any(True for _ in ds.visible_keys()) and not (force_switch and ds.show_profile):
                ds.switched_profile = False
                log.info("%s: show in place on %d visible button(s) (no profile switch)", ds, sum(1 for _ in ds.visible_keys()))
            elif ds.show_profile:
                ds.switched_profile = True
                switched.append(ds.id)
                log.info("%s: no buttons visible -> switchToProfile(profile=%r)", ds, ds.show_profile)
                await self.conn.switch_to_profile(ds.id, profile=ds.show_profile)
            else:
                ds.switched_profile = False
                log.info("%s: no buttons and no bundled profile for its type -> left alone", ds)
        self.running = True
        self._show_started_at = time.monotonic()  # "Stop on use" measures the idle timer against this
        SHOW_RUNNING_MARKER.parent.mkdir(parents=True, exist_ok=True)
        SHOW_RUNNING_MARKER.write_text("\n".join(switched), encoding="utf-8")  # devices to restore if this process dies
        set_daemon_active(True, gate_ms=self.gate_ms)  # tell the daemon section to open the mic now
        self._render_task = asyncio.create_task(self._render_loop())

    async def _stop(self):
        # Called from two different tasks: on_event's keyDown handler (a separate
        # task from the render loop -- cancelling self._render_task there is a
        # normal cross-task cancel), and _render_loop's own no-buttons (Folder)
        # branch (the CURRENT task -- cancelling it there would raise
        # CancelledError out of the very `await`s below, on this task's own next
        # suspension point, silently skipping _blank_all/switch_to_profile via
        # the try/except in _render_loop). Guard against that self-cancel case
        # (found the hard way: the safety-timeout path never actually restored
        # the previous profile until this guard was added, 2026-09-19).
        log.info("stopping live show")
        self.running = False
        self._show_started_at = 0.0
        set_daemon_active(False)  # tell the daemon to close the mic -- before cancelling
        task, self._render_task = self._render_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
        for ds in self.devices.values():
            ds.last_cell_color.clear()
            try:
                await self._blank_device(ds)
                if ds.switched_profile:
                    ds.switched_profile = False
                    log.info("%s: switchToProfile with no profile field -- Stream Deck returns to the "
                              "previous profile on its own (확정)", ds)
                    await self.conn.switch_to_profile(ds.id, profile=None)
                elif ds.contexts_by_coord:
                    log.info("%s: in-place show stopped; staying on the current page", ds)
            except Exception as e:  # socket already closed on app quit: nothing left to restore through
                log.warning("%s: could not restore on stop (%s)", ds, e)
        try:
            SHOW_RUNNING_MARKER.unlink()
        except FileNotFoundError:
            pass

    async def _render_loop(self):
        period = 1.0 / RENDER_FPS
        started_at = self.loop.time()
        try:
            last_beat = self.loop.time()
            while self.running:
                if self.loop.time() - last_beat >= HEARTBEAT_S:
                    set_daemon_active(True, gate_ms=self.gate_ms)  # heartbeat: daemon closes the mic if this stops
                    last_beat = self.loop.time()
                if self.loop.time() - started_at > NO_KEYS_GRACE_S and not self._any_keys():
                    log.warning("no buttons visible on any device %ss after starting (device stuck "
                                "in a Folder?) -- stopping so the mic doesn't stay open", NO_KEYS_GRACE_S)
                    await self._stop()
                    break
                self._png_cache = {}  # fresh per-frame palette cache
                for ds in list(self.devices.values()):
                    if ds.enabled and ds.connected and any(True for _ in ds.visible_keys()):
                        await self._render_frame(ds)
                await asyncio.sleep(period)
        except asyncio.CancelledError:
            pass
        except Exception:
            # A crash here must not leave the mic stuck open / running stuck True
            # forever with nobody the wiser (this exact bug happened once during
            # dev testing: a new willAppear grew self.rows after the renderer was
            # already built for the old size, and the stale self.rows bound check
            # below let an out-of-range row through into a fixed-size grid).
            log.exception("render loop crashed -- stopping the show to avoid a stuck mic")
            await self._stop()

    async def _render_frame(self, ds):
        if not ds.rows or not ds.cols:
            return  # still waiting for willAppear from the profile we just switched to
        # Everything is computed in a "virtual" upright grid: one column per
        # meter, bottom-up fill across the rows. Rotation ("회전"): for
        # 90/270 the virtual grid is the physical one with rows/cols swapped (a
        # device on its side is taller than it is wide); `_virtual_index_map`
        # then says which virtual button each physical button shows, and the button image
        # itself is rotated by the same angle inside key_image_bytes.
        # "Bars" packs K meters side by side into one button, so the meter count is
        # vcols * K (사용자 확정, 2026-09-19: 한 칸에 들어가는 막대 개수만큼 대역이
        # 늘어난다; 2026-09-19 later: count chosen directly instead of a width).
        segment = self._effective_segment()
        if segment == SEGMENT_ICONS:
            await self._render_frame_icons(ds)
            return
        k = self.bars_per_key
        vrows, vcols = (ds.cols, ds.rows) if self.rotation in (90, 270) else (ds.rows, ds.cols)
        vmap = self._virtual_index_map(vrows, vcols)  # physical (row, col) -> virtual key index
        # Which virtual rows are actually present in each virtual column: those
        # buttons, bottom-up, form that column's meter (any placement works).
        present = {}
        for (row, col) in ds.visible_keys():
            if row >= vmap.shape[0] or col >= vmap.shape[1]:
                continue
            vr, vc = divmod(int(vmap[row][col]), vcols)
            present.setdefault(vc, []).append(vr)
        for vc in present:
            present[vc].sort(reverse=True)  # larger virtual row = lower on the device = bottom first
        rows_per_meter = [len(present.get(vc, [])) or 1 for vc in range(vcols) for _ in range(k)]
        meters = vcols * k
        if ds.renderer is None or ds.renderer.cols != meters or ds.renderer.rows_per_meter != rows_per_meter:
            log.info("%s: (re)building renderer: %s meters, buttons per meter=%s (physical=%sx%s, rotation=%s, bands/button=%s)",
                      ds, meters, rows_per_meter[::k], ds.rows, ds.cols, self.rotation, k)
            ds.renderer = ShowRenderer(cols=meters, rows_per_meter=rows_per_meter)
            ds.last_cell_color.clear()  # old renderer's dedup cache no longer applies
        raw_levels, _loudness_rel = self.audio.read(meters, mode=self.level_mode)
        levels = [self._shape_level(x) for x in raw_levels]
        if self.segment == SEGMENT_CYCLE and self._cycle_phase_expired():
            self._next_cycle_phase()  # Stripe phase over
        meter_cells = ds.renderer.render(levels, peak_hold=self.peak_hold)  # per meter: cells indexed by row_from_bottom
        self._frame_count = getattr(self, "_frame_count", 0) + 1
        if self._frame_count % 20 == 0:  # ~every 2s at 10fps: debug visibility into real levels
            log.info("mode=%s gate=%sms sens=%.1fx raw=%s -> shown=%s", self.level_mode, self.gate_ms,
                     self.sensitivity, [round(x, 3) for x in raw_levels], [round(x, 3) for x in levels])
        for (row, col), context in ds.contexts_by_coord.items():
            if not ds.in_grid(row, col) or row >= vmap.shape[0] or col >= vmap.shape[1]:
                continue  # off-grid, or grid grew (more willAppear) since this frame's map was built
            vr, vc = divmod(int(vmap[row][col]), vcols)
            rank = present[vc].index(vr)  # this button's row_from_bottom within its column's meter
            cells = tuple(meter_cells[vc * k + j][rank] for j in range(k))
            if ds.last_cell_color.get((row, col)) == cells:
                continue  # dedup: unchanged since last frame, skip setImage
            ds.last_cell_color[(row, col)] = cells
            await self.conn.set_image(context, self._image_for(cells, ds.key_size))

    async def _render_frame_icons(self, ds):
        """SEGMENT_ICONS (테마 B): every visible button is its own band; a beat on that
        band re-rolls the button's glyph and colors (the same rule as the renderer section)."""
        if ds.icon_show is None:
            # Rules (사용자 확정, 2026-09-19): a font's built-in text (only ESAMANRU
            # has one: the bundled lyrics) is used when the Text field is empty;
            # any text -- built-in or typed -- means Sequence, no text means Random.
            font = self.icon_font_path or BUNDLED_TEXT_FONT
            text = self.icon_glyphs.strip() or (DEFAULT_GLYPHS if Path(font) == Path(BUNDLED_TEXT_FONT) else "")
            if self.segment == SEGMENT_CYCLE:
                if self.cycle_phase == 0:
                    font, text = BUNDLED_TEXT_FONT, DEFAULT_GLYPHS
                else:  # phase 2: Bootstrap icons at random
                    font, text = BUNDLED_ICON_FONT, ""
            try:
                ds.icon_show = IconShow(font, text or None, sequential=bool(text),
                                        alternate_random=(text == DEFAULT_GLYPHS))
            except (OSError, ValueError) as e:
                log.warning("icon font %r unusable (%s) -- falling back to the bundled icons", self.icon_font_path, e)
                ds.icon_show = IconShow()
            ds.last_cell_color.clear()
        keys = sorted(ds.visible_keys())
        raw_levels, _ = self.audio.read(len(keys), mode=self.level_mode)
        levels = [self._shape_level(x) for x in raw_levels]
        self._frame_count = getattr(self, "_frame_count", 0) + 1
        if self._frame_count % 20 == 0:
            log.info("icons mode=%s gate=%sms sens=%.1fx raw=%s -> shown=%s", self.level_mode, self.gate_ms,
                     self.sensitivity, [round(x, 3) for x in raw_levels], [round(x, 3) for x in levels])
        changed = 0
        for key, level in zip(keys, levels):  # reading order, so Sequence text flows across keys
            ds.icon_show.update(key, level)
            changed += ds.icon_show.zoom_step(key, level)
        if self.segment == SEGMENT_CYCLE:
            if self.cycle_phase == 0 and ds.icon_show.cycles_done >= 1:
                self._next_cycle_phase()  # text x2 + random block done -> Stripe
                return
            elif self.cycle_phase == 2 and self._cycle_phase_expired():
                self._next_cycle_phase()  # icons phase over -> Text again
                return
        if changed:
            self._change_count = getattr(self, "_change_count", 0) + changed
            if self._change_count % 50 < changed:
                log.info("icons: %d character changes so far", self._change_count)
        for key, level in zip(keys, levels):
            st = ds.icon_show._state[key]
            cell = (st["glyph"], st["bg"], st["fg"], round(level, 2))
            if ds.last_cell_color.get(key) == cell:
                continue
            ds.last_cell_color[key] = cell
            png = ds.icon_show.image_bytes(key, level, size=ds.key_size, rotation=self.rotation)
            await self.conn.set_image(ds.contexts_by_coord[key], _data_url(png))

    def _effective_segment(self):
        if self.segment != SEGMENT_CYCLE:
            return self.segment
        return (SEGMENT_ICONS, SEGMENT_BAR, SEGMENT_ICONS)[self.cycle_phase]

    def _cycle_phase_expired(self):
        if self.cycle_phase_started is None:
            self.cycle_phase_started = self.loop.time()
        return self.loop.time() - self.cycle_phase_started >= CYCLE_PHASE_S

    def _next_cycle_phase(self):
        self.cycle_phase = (self.cycle_phase + 1) % 3
        self.cycle_phase_started = self.loop.time()
        for ds in self.devices.values():
            ds.icon_show = None
            ds.renderer = None
            ds.last_cell_color.clear()
        log.info("Style Random -> phase %d (%s)", self.cycle_phase, ("Text/Icon", "Stripe", "Icons")[self.cycle_phase])

    def _shape_level(self, level):
        """Sensitivity is a CURVE, level ** (1/s), not a gain: a plain multiply
        capped the meter (0.5x put the maximum exactly at the bottom key's top,
        so the key above could never light), whereas the curve keeps the full
        range at every setting and only changes how easily bars rise. (The gate
        is time-based and lives in the daemon's analyzer, not here.)"""
        level = min(1.0, max(0.0, level))
        if self.sensitivity != 1.0 and level > 0:
            level = level ** (1.0 / self.sensitivity)
        return level

    def _virtual_index_map(self, vrows, vcols):
        """Physical (row, col) -> index of the virtual key shown there. `np.rot90`
        rotates counter-clockwise; rotating the upright virtual layout CCW by
        `rotation` degrees is what reads upright once the device itself has been
        turned `rotation` degrees CLOCKWISE (verified with a worked example)."""
        idx = np.arange(vrows * vcols).reshape(vrows, vcols)
        if self.rotation == 0:
            return idx
        return np.rot90(idx, k=self.rotation // 90)

    def _image_for(self, cells, size):
        cached = self._png_cache.get((cells, size))
        if cached is None:
            cached = _data_url(key_image_bytes(
                cells, color_scheme=self.color_scheme, palette=self.palette,
                segment=self._effective_segment(), rotation=self.rotation,
                size=size))
            self._png_cache[(cells, size)] = cached
        return cached

    async def _blank_device(self, ds):
        if not ds.switched_profile:
            # In-place show: hand the buttons back to their normal look (setImage
            # with no image resets to the action's manifest image).
            for context in ds.contexts_by_coord.values():
                await self.conn.send({"context": context, "event": "setImage", "payload": {}})
            return
        black = _data_url(solid_color_png_bytes((0, 0, 0), ds.key_size))
        for context in ds.contexts_by_coord.values():
            await self.conn.set_image(context, black)


# Dev/testing convenience only (사용자 지적, 2026-09-19 "강제 실행 시켜봐"): drop a
# file at this path (`touch`) to force the show to start immediately on the next
# plugin connect, without waiting for real system idle time -- for checking
# visual changes (colors/segment style/rotation) without actually walking away
# from the keyboard. One-shot: the marker is deleted as soon as it's used.
# User-supplied fonts live here (no install needed -- the panel's file picker
# only yields a file name, never a path, 2026-09-19): drop .ttf/.otf/.ttc/.woff
# files in and pick them by name. Both screens offer the same list: the Stream
# Deck panel asks the plugin, and the Companion form asks the adapter's /fonts
# (2026-09-29). The bundled fonts are listed first.
USER_FONTS_DIR = user_data_dir() / "fonts"
BUNDLED_FONTS_DIR = ROOT / "streamdeck" / "com.castika.deckshow.sdPlugin" / "fonts"
FONT_SUFFIXES = (".ttf", ".otf", ".woff", ".ttc")
FORCE_START_MARKER = ROOT / ".runtime" / "force_start_once"


def font_choices():
    """Bundled fonts first (ESAMANRU, BOOTSTRAP ICONS), then the user's folder;
    names shown in upper case without extension (사용자 확정, 2026-09-19). Both the
    Stream Deck panel and the Companion form offer this list, so a font is used
    by dropping it in the folder, never by typing a path."""
    USER_FONTS_DIR.mkdir(parents=True, exist_ok=True)
    choices = [{"name": "ESAMANRU", "path": BUNDLED_TEXT_FONT},
               {"name": "BOOTSTRAP ICONS", "path": BUNDLED_ICON_FONT}]
    bundled_names = {Path(c["path"]).name.lower() for c in choices}
    for f in sorted(USER_FONTS_DIR.glob("*")):
        # a copy of a bundled font in the user's folder is not a second choice
        if f.suffix.lower() in FONT_SUFFIXES and f.name.lower() not in bundled_names:
            choices.append({"name": f.stem.upper(), "path": str(f)})
    return choices
# Exists while a show is running. If a plugin process dies mid-show (crash, or a
# restart for a code change), the device is left on the DeckShow profile with no
# one to switch it back -- and the next start would then make DeckShow its own
# "previous profile", so no later stop could ever return (2026-09-19, seen in
# the log: 19:09 show -> restart -> 19:17 Start Now -> 19:20 stop stayed on
# DeckShow). The next process sees this file and restores first.
SHOW_RUNNING_MARKER = ROOT / ".runtime" / "show_running"
# Device ids whose bundled profile has been installed by _preinstall_profiles,
# one per line, so the one-time install prompt is not repeated at every start.
PROFILES_INSTALLED_FILE = ROOT / ".runtime" / "profiles_installed"
PREINSTALL_WAIT_S = 90  # how long to wait for the user to answer the app's install prompt


async def plugin_main(argv):
    launch_args = parse_launch_args(argv)
    log.info("launch args: port=%s uuid=%s", launch_args["port"], launch_args["plugin_uuid"])

    loop = asyncio.get_running_loop()
    plugin_holder = {}

    async def on_event(event, msg):
        await plugin_holder["plugin"].on_event(event, msg)

    devices = launch_args["info"].get("devices", [])
    log.info("-info devices: %s", [(d.get("name"), d.get("type"), d.get("size")) for d in devices])

    async def on_connected():
        await conn.get_global_settings()
        plugin_holder["plugin"]._clear_off_on_start = True
        if SHOW_RUNNING_MARKER.exists():
            switched = [d for d in SHOW_RUNNING_MARKER.read_text(encoding="utf-8").split() if d]
            log.warning("previous plugin process died mid-show (%d device(s) on the show profile)", len(switched))
            set_daemon_active(False)
            for device_id in switched:
                log.warning("-> restoring the previous profile on %s first", device_id[:8])
                await conn.switch_to_profile(device_id, profile=None)
            SHOW_RUNNING_MARKER.unlink()
        # As a task: on_connected runs BEFORE the receive loop, and the
        # pre-install waits for willAppear events that loop delivers.
        asyncio.create_task(plugin_holder["plugin"]._preinstall_profiles())
        if FORCE_START_MARKER.exists():
            how = FORCE_START_MARKER.read_text(encoding="utf-8").strip()
            FORCE_START_MARKER.unlink()
            log.info("FORCE_START_MARKER found (%s) -- starting show in 2s (testing only)", how or "normal")

            async def force_start():
                await asyncio.sleep(2)  # deviceDidConnect events arrive only once the receive loop runs
                await plugin_holder["plugin"]._start(force_switch=(how == "switch"))
            asyncio.create_task(force_start())

    conn = StreamDeckConnection(launch_args, on_event, on_connected=on_connected)
    plugin = Plugin(conn, loop, devices)
    plugin_holder["plugin"] = plugin
    asyncio.create_task(plugin.idle_poll_loop())
    asyncio.create_task(plugin.pi_bridge_loop())

    # A restart (streamdeck://plugins/restart, app quit) terminates this process
    # while a show may be running: leave the device the way we found it.
    stop_event = asyncio.Event()

    def on_terminate():
        log.info("termination signal -> stopping show before exit")
        stop_event.set()

    import signal
    # SIGHUP does not exist on Windows, and its event loop has no
    # add_signal_handler, so take what this platform has and fall back to the
    # plain handler (which runs in the main thread between loop iterations).
    for sig in (signal.SIGTERM, signal.SIGINT) + ((signal.SIGHUP,) if hasattr(signal, "SIGHUP") else ()):
        try:
            loop.add_signal_handler(sig, on_terminate)
        except NotImplementedError:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(on_terminate))

    async def terminate_when_asked():
        await stop_event.wait()
        if plugin.running:
            await plugin._stop()
        await asyncio.sleep(0.2)  # let the last messages flush over the socket
        if conn.ws is not None:
            await conn.ws.close()  # ends conn.run()'s receive loop -> main() returns -> exit

    asyncio.create_task(terminate_when_asked())
    await conn.run()
    # The socket is gone (Stream Deck app quit or dropped us) -- nothing can be
    # drawn or restored any more, but the mic must not stay open (2026-09-20).
    if plugin.running:
        log.warning("connection lost mid-show -> closing the mic; profiles are restored at the next start")
        plugin.running = False
        set_daemon_active(False)


# ============================================================================
# Companion: HTTP/TCP API client
# ============================================================================
# The three Companion interfaces the show needs, all official and all local
# (source: bitfocus/companion companion/lib/Service/*):
#
#   HTTP  POST /api/location/<page>/<row>/<column>/style   {"png64": "data:image/png;base64,..."}
#         GET  /api/variable/internal/<name>/value          e.g. surface_streamdeck_XXXX_page
#   TCP   surface <surfaceId> page-set <page>               (TCP API must be enabled in Companion)
#
# Only the standard library: this runs in the audio app's launchd agent, and
# urllib is plenty for a few hundred small localhost requests a second.

class CompanionApi:
    def __init__(self, host="127.0.0.1", http_port=8000, tcp_port=16759, timeout=1.0):
        self.host = host
        self.http_port = http_port
        self.tcp_port = tcp_port
        self.timeout = timeout

    # -- HTTP -----------------------------------------------------------------
    def _url(self, path):
        return f"http://{self.host}:{self.http_port}{path}"

    def _request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self._url(path), data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")

    def alive(self):
        """Companion's web server answers on this port."""
        try:
            self._request("GET", "/api/variable/internal/time_hms/value")
            return True
        except urllib.error.HTTPError:
            return True  # any HTTP answer means Companion is there
        except (urllib.error.URLError, socket.timeout, OSError):
            return False

    def variable(self, name):
        """Value of an internal variable as a string, or None if it does not exist
        (404) -- a surface that is configured but not attached has no
        surface_<id>_page variable at all."""
        try:
            _status, text = self._request("GET", f"/api/variable/internal/{urllib.parse.quote(name)}/value")
            return text
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def custom_variable(self, name):
        """Value of a custom variable as a string, or None if it is not defined."""
        try:
            _status, text = self._request("GET", f"/api/custom-variable/{urllib.parse.quote(name)}/value")
            return text
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def surface_page(self, surface):
        text = self.variable(f"surface_{surface.variable_id}_page")
        try:
            return int(text) if text is not None else None
        except ValueError:
            return None

    def set_style_png(self, page, row, col, png_data_url):
        """Replace a button's image. This edits the button's stored style -- only
        ever call it on the show page, never on the user's own pages."""
        self._request("POST", f"/api/location/{page}/{row}/{col}/style", {"png64": png_data_url})

    def clear_style_png(self, page, row, col):
        self._request("POST", f"/api/location/{page}/{row}/{col}/style", {"png64": ""})

    # -- TCP --------------------------------------------------------------------
    def tcp(self, command):
        """One command over Companion's TCP API; returns its reply line."""
        with socket.create_connection((self.host, self.tcp_port), timeout=self.timeout) as s:
            s.sendall((command + "\n").encode())
            s.settimeout(self.timeout)
            try:
                return s.recv(256).decode("utf-8", "replace").strip()
            except socket.timeout:
                return ""

    def set_surface_page(self, surface_id, page):
        reply = self.tcp(f"surface {surface_id} page-set {page}")
        log.info("page-set %s -> %s: %s", surface_id, page, reply or "(no reply)")
        return reply


# ============================================================================
# Companion: reading Companion's own surface/page registry
# ============================================================================
# What Bitfocus Companion knows about its surfaces and pages, read from
# Companion's own data files (2026-09-20).
#
# Companion's public HTTP/TCP/UDP APIs have no "list surfaces" call (every route
# checked), but Companion writes its scan/registration result -- the list shown
# on its Surfaces page -- to a plain SQLite key/value store under the user's
# Application Support folder. That is an ordinary user file (no TCC protection,
# verified from a shell without Full Disk Access), and with Companion's default
# "Watch for new USB Devices" + "Auto-enable newly discovered surfaces" it stays
# current by itself, so reading it is the one automatic way to follow surfaces
# being added and removed. It is a storage format, not an API: every read here
# is defensive and a failure just means "no surfaces known".
#
# Key grid vs Companion grid (사용자 지적: "그리드는 버튼만을 말하지 않아"): a
# surface's `gridSize` counts touch-strip segments, dials and touch points too
# (Stream Deck +: 4x2 buttons but a 4x4 grid). The drawable buttons come from the
# model name instead (KEY_GRIDS, same source as dev/make_profiles.py); gridSize is
# only the fallback for all-button surfaces such as the emulator.

COMPANION_DIR = companion_data_dir()

# Companion `type` string (as stored per surface) -> (columns, rows) of LCD buttons
# at rows 0.., columns 0.. of that surface. node-elgato-stream-deck
# models/definitions.ts, 2026-09-20. Matched by substring, most specific first.
KEY_GRIDS = (
    ("Stream Deck + XL", (9, 4)),
    ("Stream Deck XL", (8, 4)),
    ("Stream Deck Mini", (3, 2)),
    ("Stream Deck Neo", (4, 2)),
    ("Stream Deck +", (4, 2)),
    ("Stream Deck Plus", (4, 2)),
    ("Galleon", (3, 4)),
    ("Stream Deck", (5, 3)),  # original / MK.2
)
KEY_SIZES = (  # native key pixels, same table as the plugin section
    ("Stream Deck + XL", 112), ("Stream Deck XL", 96), ("Stream Deck Mini", 80), ("Stream Deck Neo", 96),
    ("Stream Deck +", 120), ("Stream Deck Plus", 120), ("Galleon", 160), ("Stream Deck", 72),
)
DEFAULT_KEY_SIZE = 72


@dataclass
class Surface:
    id: str  # e.g. "streamdeck:BL40J1B08403", "emulator:...", satellite ids
    type: str
    enabled: bool
    grid: tuple  # (columns, rows) Companion reserves for it (keys + strips + dials)
    keys: tuple  # (columns, rows) of drawable LCD keys, top-left
    key_size: int
    page: int  # page number it is on (as stored; the live value comes from the HTTP variable)
    startup_page: int  # the page Companion itself starts this surface on (its own setting; read, never written)

    @property
    def variable_id(self):
        """Companion names its per-surface variables surface_<id>_... with ':' -> '_'."""
        return self.id.replace(":", "_")


@dataclass
class Registry:
    data_dir: Path = None
    http_port: int = 8000
    tcp_enabled: bool = False
    tcp_port: int = 16759
    surfaces: list = field(default_factory=list)
    pages: dict = field(default_factory=dict)  # number -> name
    page_ids: dict = field(default_factory=dict)  # page id -> number

    def pages_named(self, prefix):
        """Page numbers whose name starts with `prefix`, in page order."""
        return [n for n, pname in sorted(self.pages.items()) if pname.startswith(prefix)]


def _latest_data_dir(base=COMPANION_DIR):
    """Companion keeps its store under a per-version folder (v5.0/...); take the
    highest one that actually has a db."""
    candidates = []
    for d in base.glob("v*"):
        if (d / "db.sqlite").exists():
            try:
                candidates.append((tuple(int(x) for x in d.name[1:].split(".")), d))
            except ValueError:
                continue
    return max(candidates)[1] if candidates else None


def _key_grid(type_name, grid):
    for needle, keys in KEY_GRIDS:
        if needle in type_name:
            return keys
    return grid  # unknown model / emulator: every cell is a button


def _key_size(type_name):
    for needle, size in KEY_SIZES:
        if needle in type_name:
            return size
    return DEFAULT_KEY_SIZE


def read_registry(base=COMPANION_DIR):
    """Snapshot of Companion's surfaces/pages/API settings, or an empty Registry
    if Companion has no data here. Reads a COPY of the db (+ its WAL) so a
    Companion mid-write never trips us, and never opens the live file for write."""
    reg = Registry()
    try:
        cfg = json.loads((base / "config.json").read_text(encoding="utf-8"))
        reg.http_port = int(cfg.get("http_port", reg.http_port))
    except (OSError, ValueError):
        pass
    data_dir = _latest_data_dir(base)
    if data_dir is None:
        return reg
    reg.data_dir = data_dir
    tmp = Path(tempfile.mkdtemp(prefix="castika_companion_"))
    try:
        for suffix in ("", "-wal", "-shm"):
            src = data_dir / f"db.sqlite{suffix}"
            if src.exists():
                shutil.copy2(src, tmp / f"db.sqlite{suffix}")
        con = sqlite3.connect(str(tmp / "db.sqlite"))  # our private copy; the ?mode=ro URI form fails on Apple's python3 sqlite
        try:
            _read_userconfig(con, reg)
            _read_pages(con, reg)
            _read_surfaces(con, reg)
        finally:
            con.close()
    except (sqlite3.Error, OSError) as e:
        log.warning("could not read Companion's store at %s: %s", data_dir, e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return reg


def _rows(con, table):
    try:
        for key, value in con.execute(f"select id, value from {table}"):
            if not isinstance(value, (str, bytes)):
                yield key, value  # scalar rows (e.g. page_config_version) are stored raw
                continue
            try:
                yield key, json.loads(value)
            except ValueError:
                continue
    except sqlite3.Error as e:
        log.warning("table %s unreadable: %s", table, e)


def _read_userconfig(con, reg):
    for key, value in _rows(con, "main"):
        if key == "userconfig" and isinstance(value, dict):
            reg.tcp_enabled = bool(value.get("tcp_enabled", False))
            reg.tcp_port = int(value.get("tcp_listen_port", reg.tcp_port) or reg.tcp_port)


def _read_pages(con, reg):
    for key, value in _rows(con, "pages"):
        try:
            number = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            reg.pages[number] = str(value.get("name", ""))
            if value.get("id"):
                reg.page_ids[value["id"]] = number


def _read_surfaces(con, reg):
    for key, value in _rows(con, "surfaces"):
        if not isinstance(value, dict):
            continue
        type_name = str(value.get("type", ""))
        gs = value.get("gridSize") or {}
        grid = (int(gs.get("columns", 0) or 0), int(gs.get("rows", 0) or 0))
        group = value.get("groupConfig") or {}
        page = group.get("page")
        if not isinstance(page, int):
            page = reg.page_ids.get(group.get("last_page_id"), 1)
        startup = reg.page_ids.get(group.get("startup_page_id"), 1)
        reg.surfaces.append(Surface(
            id=str(key), type=type_name, enabled=bool(value.get("enabled", True)),
            grid=grid, keys=_key_grid(type_name, grid), key_size=_key_size(type_name), page=page,
            startup_page=startup,
        ))


# ============================================================================
# Companion: show page file (fallback without the module)
# ============================================================================
# The ONE Companion page file the show draws on:
#     companion/Castika DeckShow.companionconfig   (9x4 buttons, written by the adapter)
# The user imports it (Companion > Import / Export > Import > Buttons tab) once
# per kind of deck they have -- the same file again for a second grid size -- and
# the adapter hands the resulting pages out by itself (사용자 요청 2026-09-20:
# "하나만 가져다가 여러 곳에 배치를 알아서"). It is the Companion counterpart of the
# bundled Stream Deck profiles.
#
# Why a page per GRID at all: Companion draws per page and every surface crops
# its page from the top-left, so a 3x2 deck looking at a 9x4 spectrum would see
# three low bands' tips only. So each page is drawn for one button grid, at its
# top-left, the rest of the 9x4 left black; decks with the same grid share one.
#
# Every cell of the page is a button that
#   - has an IMAGE layer and `canModifyStyleInApis: true` -- both are required
#     for the HTTP style API's png64 to land on a Companion 5 layered button
#     (LayeredButtonStyleEditor.updateFromLegacyProperties returns early without
#     canModifyStyleInApis, and png64 needs an 'image' element)
#   - runs Companion's own "Surface: Set to page -> Back" on press, so a button
#     press returns that surface to where it came from without our help
#   - draws no top bar (canvas decoration 'none'), black background
#
# The JSON shapes were taken from a page Companion 5.0.6 exported after one such
# button was built in its UI (2026-09-20), plus LayerDefaults.ts for the image
# layer. Ids are deterministic so regenerating changes nothing.

PAGE_NAME = "Castika DeckShow"  # every imported copy carries this name; the adapter numbers them itself
COLUMNS, ROWS = 9, 4  # the largest key grid of any model (Stream Deck + XL)
FILE_VERSION = 12  # Companion 5.0 export format (ImportExport/Constants.ts)
COMPANION_BUILD = "5.0.6"


def _id(*parts):
    return uuid.uuid5(uuid.NAMESPACE_URL, "castika-companion-page/" + "/".join(str(p) for p in parts)).hex[:21]


def v(value):
    return {"value": value, "isExpression": False}


def button(row, col, grid):
    return {
        "type": "button-layered",
        "style": {
            "layers": [
                {"id": "canvas", "name": "Canvas", "usage": "auto", "type": "canvas",
                 "decoration": v("none"), "showStatusIcons": v("none")},
                {"id": "box0", "name": "Background", "usage": "auto", "type": "box",
                 "enabled": v(True), "opacity": v(100), "x": v(0), "y": v(0), "width": v(100), "height": v(100),
                 "rotation": v(0), "color": v(0), "cornerRadius": v(0), "borderWidth": v(0), "borderColor": v(0),
                 "borderPosition": v("inside")},
                {"id": "image0", "name": "Show", "usage": "auto", "type": "image",
                 "enabled": v(True), "opacity": v(100), "x": v(0), "y": v(0), "width": v(100), "height": v(100),
                 "rotation": v(0), "base64Image": v(None), "halign": v("center"), "valign": v("center"),
                 "fillMode": v("fill")},
            ]
        },
        "options": {"stepProgression": "auto", "stepExpression": "", "rotaryActions": False,
                    "canModifyStyleInApis": True, "notes": "Castika DeckShow: drawn by the show, press = back"},
        "feedbacks": [],
        "steps": {"0": {"action_sets": {
            "down": [{"id": _id("action", grid, row, col), "definitionId": "set_page", "connectionId": "internal",
                      "options": {"surfaceId": v("self"), "page": v("back")}, "type": "action", "children": {}}],
            "up": []}, "options": {"runWhileHeld": []}}},
        "localVariables": [],
    }


def build(cols=COLUMNS, rows=ROWS):
    name = PAGE_NAME
    controls = {str(r): {str(c): button(r, c, name) for c in range(cols)} for r in range(rows)}
    return name, {
        "version": FILE_VERSION,
        "type": "page",
        "companionBuild": COMPANION_BUILD,
        "page": {"id": _id("page", name), "name": name, "controls": controls,
                 "gridSize": {"minColumn": 0, "maxColumn": cols - 1, "minRow": 0, "maxRow": rows - 1}},
        "instances": {},
        "connectionCollections": [],
        "oldPageNumber": 99,
        "imageLibrary": [],
        "imageLibraryCollections": [],
    }



def write_page_file(folder):
    """Writes <folder>/Castika DeckShow.companionconfig (compact JSON) and returns its path."""
    name, export = build()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.companionconfig"
    path.write_text(json.dumps(export, separators=(",", ":"), ensure_ascii=False) + "\n", encoding="utf-8")
    return path


# ============================================================================
# Companion: local endpoint for the Companion module
# ============================================================================
# Local HTTP endpoint for the Companion module (dev/companion-module/), the thin
# JavaScript layer Companion itself runs (2026-09-20). The module
# owns nothing but the buttons the user placed; everything else (idle detection,
# audio, rendering) stays in this Python adapter. Protocol, all JSON on
# 127.0.0.1:MODULE_PORT, module -> adapter only:
#
#   POST /keys      {"keys": [{"id", "page", "row", "col", "size"}, ...]}
#                   the full list of "DeckShow Button" buttons the module has
#                   (sent again whenever one is added or removed)
#   GET  /frames?seq=N   -> {"running": bool, "seq": M, "images": {id: data-url, ...}}
#                   images that changed since seq N; the module polls this ~10x/s
#   POST /press     {"id": ...}   a placed button was pressed -> start/stop toggle
#   POST /session   {"off": true} no more auto-starts until a button is pressed
#                                 (the long press of the module's preset)
#   POST /settings  {...}         the module's config form (same keys as the
#                   Stream Deck inspector), applied on top of everything else
#   GET  /status    -> {"running", "keys", "adapter": "companion"}
#   GET  /surfaces  -> {"surfaces": [...], "pages": [{"n", "name"}], "tcp": bool}
#   GET  /fonts     -> {"fonts": [{"name", "path"}], "folder": <user fonts folder>}
# Every answer carries "boot": this run's id. The module watches it: a new id means
# the installation restarted and forgot the form's settings and the placed buttons,
# so it sends both again (2026-09-29: after a restart the show fell back to the
# settings file and drew the wrong style).
#                   Companion's own surface registry, so the module's config form
#                   can offer a "switch to page" per deck (사용자 제안 2026-09-20)
#
# Nothing here reaches outside the machine: the server binds to 127.0.0.1 only.

MODULE_PORT = 18790
PI_PAGE_FILE = ROOT / "streamdeck" / "com.castika.deckshow.sdPlugin" / "ui" / "inspector.html"
# Appended to that page when a browser asks for it. The page talks to the Stream
# Deck app over a WebSocket; here the same calls go to this server instead, so
# the page is used unchanged and there is only one settings screen to maintain.
PI_BROWSER_SHIM = """
<script>
(function () {
  function post(text) {
    fetch("/pi/send", { method: "POST", headers: { "Content-Type": "application/json" }, body: text })
      .catch(function () {});
  }
  // readyState is not decoration: the page refuses to send unless it reads 1
  // (OPEN), the way a real WebSocket does. Without it every change was dropped
  // in silence while the page still showed values (2026-09-29).
  function Bridge() {
    this.readyState = 1;  // OPEN: this transport is ready as soon as it exists
    this.onopen = null; this.onmessage = null;
    var self = this; setTimeout(function () { self.start(); }, 0);
  }
  Bridge.prototype.send = function (text) { post(text); };
  Bridge.prototype.close = function () { this.readyState = 3; };
  Bridge.prototype.start = function () {
    if (this.onopen) this.onopen();
    var self = this;
    setInterval(function () {
      fetch("/pi/events").then(function (r) { return r.json(); }).then(function (msgs) {
        for (var i = 0; i < msgs.length; i++) {
          if (self.onmessage) self.onmessage({ data: JSON.stringify(msgs[i]) });
        }
      }).catch(function () {});
    }, 500);
  };
  window.WebSocket = Bridge;
  // "This deck" belongs to the button the panel was opened from; a browser has
  // no button, so that one row (and its rule) is hidden here and everything
  // else works as usual.
  var style = document.createElement("style");
  style.textContent = "#deckRow, #deckSep { display: none !important; }";
  document.head.appendChild(style);
  // Say whose settings these are: the page is served by the Companion adapter's
  // port but it configures the Stream Deck plugin, and Companion keeps its own
  // settings in its connection (사용자 지적 2026-09-29).
  document.title = "Castika DeckShow - Stream Deck settings";
  var whose = document.getElementById("showDevice");
  if (whose) { whose.textContent = "Stream Deck settings"; }
  var note = document.createElement("div");
  note.textContent = "These settings are for the Stream Deck plugin only. They do not apply to Bitfocus Companion, which keeps its own settings in its connection.";
  note.style.cssText = "margin: 0 12px 10px; font-size: 11.5px; line-height: 1.4; color: var(--text);";
  var header = document.querySelector(".learn");
  if (header && header.parentNode) { header.parentNode.insertBefore(note, header.nextSibling); }
  connectElgatoStreamDeckSocket(0, "browser-settings", "registerPropertyInspector",
    JSON.stringify({ colors: {} }), JSON.stringify({ action: "com.castika.deckshow.key" }));
})();
</script>
"""


BOOT_ID = uuid.uuid4().hex[:12]  # changes whenever this process starts


class ModuleState:
    """What the module told us and what we have drawn for it. Shared between
    the HTTP threads and the adapter's loop, so every access takes the lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.keys = {}  # key id -> {"page", "row", "col", "size"}
        self.keys_version = 0  # bumped on every /keys, so the adapter can rebuild its layout
        self.frames = {}  # key id -> (seq, data_url)
        self.seq = 0
        self.running = False
        self.pressed = []  # key ids pressed since the adapter last looked
        self.session_off = None  # set by POST /session: True = no more auto-starts this session
        self.settings = None  # last /settings body, or None
        self.settings_version = 0
        self.surfaces_provider = None  # set by the adapter: () -> {"surfaces": [...], "tcp": bool}
        self.last_seen = -1e9  # time.monotonic() of the module's last request; "never" must not look recent (monotonic starts near 0 on macOS)

    def set_keys(self, keys):
        with self.lock:
            self.keys = {str(k["id"]): {"page": int(k["page"]), "row": int(k["row"]), "col": int(k["col"]),
                                        "size": int(k.get("size") or 72)} for k in keys}
            self.keys_version += 1
            self.frames = {kid: f for kid, f in self.frames.items() if kid in self.keys}

    def put_frames(self, images):
        """images: {key id: data_url}; one seq for the whole batch."""
        with self.lock:
            if not images:
                return
            self.seq += 1
            for kid, url in images.items():
                self.frames[kid] = (self.seq, url)

    def clear_frames(self):
        with self.lock:
            self.seq += 1
            self.frames = {kid: (self.seq, None) for kid in self.keys}  # None = back to the button's own style

    def frames_since(self, seq):
        with self.lock:
            return self.running, self.seq, {kid: url for kid, (s, url) in self.frames.items() if s > seq}


class _Handler(BaseHTTPRequestHandler):
    state: ModuleState = None  # set by serve()

    def log_message(self, *args):  # quiet; the adapter logs what matters
        pass

    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        import time
        self.state.last_seen = time.monotonic()
        url = urlparse(self.path)
        if url.path == "/frames":
            seq = int((parse_qs(url.query).get("seq") or ["0"])[0])
            running, cur, images = self.state.frames_since(seq)
            self._json(200, {"running": running, "seq": cur, "images": images, "boot": BOOT_ID})
        elif url.path == "/status":
            with self.state.lock:
                self._json(200, {"running": self.state.running, "keys": len(self.state.keys),
                                 "adapter": "companion", "boot": BOOT_ID})
        elif url.path == "/fonts":
            self._json(200, {"fonts": font_choices(), "folder": str(USER_FONTS_DIR)})
        elif url.path == "/surfaces":
            provider = self.state.surfaces_provider
            self._json(200, provider() if provider else {"surfaces": [], "tcp": False})
        elif url.path in ("/pi", "/pi/"):
            self._settings_page()
        elif url.path == "/pi/events":
            self._json(200, pi_bridge_take(PI_TO_PAGE))
        else:
            self._json(404, {"error": "unknown path"})

    def _settings_page(self):
        """The Stream Deck app's own settings page, served to a browser.

        The file is sent as it is and one script is appended: it replaces the
        WebSocket the page expects with the same calls over this server, so the
        page itself never has to know where it is running."""
        try:
            html = PI_PAGE_FILE.read_text(encoding="utf-8")
        except OSError as e:
            return self._json(404, {"error": f"the settings page is not there ({e})"})
        body = (html + PI_BROWSER_SHIM).encode("utf-8")
        log.info("browser settings page served (%d bytes)", len(body))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        import time
        self.state.last_seen = time.monotonic()
        url = urlparse(self.path)
        try:
            body = self._body()
        except ValueError:
            return self._json(400, {"error": "bad json"})
        if url.path == "/keys":
            keys = body.get("keys") or []
            try:
                self.state.set_keys(keys)
            except (KeyError, TypeError, ValueError) as e:
                return self._json(400, {"error": f"bad key entry: {e}"})
            # The sizes come from the surface itself, whatever it is (a Stream
            # Deck, a Loupedeck, the built-in emulator): we draw at what it asks
            # for, so this line is how an unknown surface tells us its pixels.
            sizes = sorted({int(k.get("size") or 0) for k in keys})
            log.info("module reports %d button(s), image size(s): %s", len(keys),
                     ", ".join("%dpx" % n for n in sizes) or "none")
            self._json(200, {"ok": True})
        elif url.path == "/press":
            with self.state.lock:
                self.state.pressed.append(str(body.get("id")))
            self._json(200, {"ok": True})
        elif url.path == "/session":
            with self.state.lock:
                self.state.session_off = bool(body.get("off", True))
            self._json(200, {"ok": True})
        elif url.path == "/settings":
            with self.state.lock:
                self.state.settings = dict(body)
                self.state.settings_version += 1
            self._json(200, {"ok": True})
        elif url.path == "/pi/send":
            # Logged so a machine where the panel is blank can be told apart from
            # one where the page never reached us at all (2026-09-29).
            log.info("browser settings page sent: %s", body.get("event") or "?")
            pi_bridge_write(PI_TO_PLUGIN, body)  # the plugin picks it up and answers
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "unknown path"})


def serve(state, port=MODULE_PORT):
    """Starts the server in a daemon thread; returns it (or None if the port is taken)."""
    _Handler.state = state
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    except OSError as e:
        log.error("module endpoint could not bind 127.0.0.1:%s (%s) -- the Companion module will not connect", port, e)
        return None
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="module-http", daemon=True).start()
    log.info("module endpoint listening on http://127.0.0.1:%s", port)
    return httpd


# ============================================================================
# Companion: the adapter (mode: companion)
# ============================================================================
# Companion adapter: the same idle-time show for decks that Bitfocus Companion
# owns instead of the Stream Deck app. Companion never hands a module the pixels
# of a deck, so the module and this adapter split the work:
#
#   - the module (dev/companion-module) sits in Companion, carries the actions,
#     the preset and the feedback, and tells us over HTTP (the module endpoint
#     section) where its DeckShow Buttons are, what the connection form says and
#     when one was pressed
#   - this adapter draws the frames and hands them back as png64; the module's
#     feedback puts each picture on its button. Buttons on one page are drawn as
#     one picture, so a column of them is one meter
#   - which decks exist, their model and grid, and the page each one shows: read
#     from Companion's own store (the registry section)
#   - a deck set to "Show on a specific page and back" is turned to that page
#     over the TCP API when the show starts and back when it ends; every other
#     deck is drawn on whatever page it is already showing
#   - mic: the daemon section, told through its own control file (adapter="companion")
#
# Settings come from the module's connection form. Two older paths are still
# read, in this order: the form wins, then a Companion custom variable named
# `castika` (`key=value` pairs, for people who drive Companion by script), then
# companion.json next to the user fonts. The page-file section below writes
# importable show pages for the same reason: they were the only way before the
# module existed, and a Companion without our module can still use them.
#
# Runs in the host process next to the audio daemon (one login item, two
# threads); when Companion is not running it just polls quietly.


ADAPTER = "companion"
SETTINGS_FILE = user_data_dir() / "companion.json"
DEFAULT_GLYPHS_FILE = ROOT / "streamdeck" / "com.castika.deckshow.sdPlugin" / "default_glyphs.txt"
SHOW_PAGE_NAME = "Castika DeckShow"  # every imported copy of the page file (the page-file section) starts with this
# RENDER_FPS: defined once at the top of the file
IDLE_POLL_S = 2
REGISTRY_REFRESH_S = 10
PAGE_POLL_S = 0.5  # how often to ask Companion which page each surface is on (= key-press detection)
MODULE_STALE_S = 5  # the module polls /frames every 0.1-0.5 s; silence this long = Companion (or the connection) is gone
DISPLAY_POLL_S = 2  # display asleep/awake check (CGDisplayIsAsleep, microseconds)
COMPANION_FORCE_START_MARKER = ROOT / ".runtime" / "companion_force_start_once"  # testing hook, like the plugin's
SETTINGS_VARIABLE = "castika"  # $(custom:castika) in Companion
SETTINGS_POLL_S = 3
# custom-variable key -> (settings key, parser). Values as the inspector words them.
_STYLES = {"text": SEGMENT_ICONS, "icons": SEGMENT_ICONS, "stripe": SEGMENT_BAR, "bar": SEGMENT_BAR,
           "solid": SEGMENT_SOLID, "random": SEGMENT_CYCLE, "cycle": SEGMENT_CYCLE}
_ON = {"on": True, "off": False, "true": True, "false": False, "1": True, "0": False}
SETTING_KEYS = {
    "show": ("enabled", lambda v: _ON[v.lower()]),
    "start_after": ("idleThresholdMinutes", lambda v: max(0.5, min(30.0, float(v)))),
    "style": ("segmentStyle", lambda v: _STYLES[v.lower()]),
    "text": ("iconGlyphs", str),
    "font": ("iconFontPath", str),
    "color": ("colorScheme", lambda v: {"fixed": COLOR_SCHEME_TIER, "gradient": COLOR_SCHEME_TONE}[v.lower()]),
    "color_low": ("colorLow", str),
    "color_mid": ("colorMid", str),
    "color_high": ("colorHigh", str),
    "bars": ("barsPerKey", lambda v: max(1, min(14, int(v)))),
    "rotate": ("rotationDegrees", lambda v: {"0": 0, "90": 90, "180": 180, "270": 270}[v]),
    "sensitivity": ("sensitivity", lambda v: max(0.25, min(4.0, float(v)))),
    "gate_ms": ("gateMs", lambda v: max(0, min(5000, int(v)))),
    "peak": ("peakHold", lambda v: _ON[v.lower()]),
    "stop_on_activity": ("stopOnActivity", lambda v: _ON[v.lower()]),
    "analysis": ("levelMode", lambda v: {"relative": "relative", "absolute": "absolute"}[v.lower()]),
    "surfaces_off": ("disabledSurfaces", lambda v: [x.strip() for x in v.split(",") if x.strip()]),
}


def parse_settings_variable(text):
    """`key=value key2=value2 ...` -> {settings key: parsed value}; a value with
    spaces goes in double quotes. Bad keys/values are reported, not fatal."""
    import shlex
    out, errors = {}, []
    try:
        tokens = shlex.split(text or "")
    except ValueError as e:
        return out, [f"unbalanced quotes ({e})"]
    for tok in tokens:
        key, sep, value = tok.partition("=")
        key = key.strip().lower()
        if not sep or key not in SETTING_KEYS:
            errors.append(f"unknown '{tok}'")
            continue
        settings_key, parse = SETTING_KEYS[key]
        try:
            out[settings_key] = parse(value.strip())
        except (KeyError, ValueError):
            errors.append(f"bad value '{tok}'")
    return out, errors

DEFAULT_SETTINGS = {
    "enabled": True,
    "idleThresholdMinutes": 1,
    "segmentStyle": SEGMENT_ICONS,
    "barsPerKey": 4,
    "colorScheme": COLOR_SCHEME_TIER,
    "colorLow": "#00e65a", "colorMid": "#ffdc00", "colorHigh": "#ff2828",
    "rotationDegrees": 0,
    "levelMode": "relative",
    "sensitivity": 1.0,
    "gateMs": 0,
    "peakHold": True,
    "stopOnActivity": DEFAULT_STOP_ON_ACTIVITY,
    "iconFontPath": "",
    "iconGlyphs": "",
    "showPageName": SHOW_PAGE_NAME,  # prefix of the show pages' names
    "disabledSurfaces": [],  # surface ids left out of the show (Companion's own enable switch is honoured too)
    "companionHost": "127.0.0.1",
}


def _hex(c, fallback):
    if isinstance(c, str) and len(c) == 7 and c[0] == "#":
        try:
            return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))
        except ValueError:
            pass
    return fallback


def load_settings():
    data = dict(DEFAULT_SETTINGS)
    try:
        data.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except FileNotFoundError:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(DEFAULT_SETTINGS, indent=2), encoding="utf-8")
        log.info("wrote default settings to %s", SETTINGS_FILE)
    except (OSError, ValueError) as e:
        log.warning("settings unreadable (%s) -- defaults", e)
    if "{" in str(data.get("showPageName", "")):
        data["showPageName"] = SHOW_PAGE_NAME  # earlier per-grid naming: the name is a plain prefix now
    return data


def show_settings(d):
    seg = d.get("segmentStyle")
    return ShowSettings(
        segment=seg if seg in (SEGMENT_SOLID, SEGMENT_BAR, SEGMENT_ICONS, SEGMENT_CYCLE) else SEGMENT_ICONS,
        bars_per_key=max(1, min(14, int(d.get("barsPerKey", 4) or 4))),
        color_scheme=d.get("colorScheme") if d.get("colorScheme") in (COLOR_SCHEME_TONE, COLOR_SCHEME_TIER) else COLOR_SCHEME_TIER,
        palette=(_hex(d.get("colorLow"), DEFAULT_PALETTE[0]), _hex(d.get("colorMid"), DEFAULT_PALETTE[1]), _hex(d.get("colorHigh"), DEFAULT_PALETTE[2])),
        rotation=d.get("rotationDegrees") if d.get("rotationDegrees") in (0, 90, 180, 270) else 0,
        level_mode="absolute" if d.get("levelMode") == "absolute" else "relative",
        sensitivity=float(d.get("sensitivity", 1.0) or 1.0),
        icon_font_path=str(d.get("iconFontPath") or ""),
        icon_glyphs=str(d.get("iconGlyphs") or ""),
        default_glyphs=DEFAULT_GLYPHS_FILE.read_text(encoding="utf-8") if DEFAULT_GLYPHS_FILE.exists() else "",
        peak_hold=bool(d.get("peakHold", True)),
    )


def _module_form_to_settings(form):
    """The module's config form uses the custom-variable key names (style, bars,
    ...); values arrive typed (numbers, booleans) or as strings."""
    out = {}
    for key, (settings_key, parse) in SETTING_KEYS.items():
        if key not in form or form[key] in (None, ""):
            continue
        try:
            out[settings_key] = parse(str(form[key]).lower() if isinstance(form[key], bool) else str(form[key]))
        except (KeyError, ValueError):
            log.warning("module settings: bad value for %s: %r", key, form[key])
    return out


class CompanionShow:
    def __init__(self):
        self.shutdown = threading.Event()  # set by the signal handlers; run() returns on it
        self.session_off = False  # long press: no auto-start until a button is pressed again
        self._last_activity_poll = 0.0
        self._show_started_at = 0.0
        self._module_viewed = None  # pages the participating decks were on when the grids were built
        self.file_settings = load_settings()
        self.settings = dict(self.file_settings)
        self._variable_text = None
        self._settings_read_at = -1e9
        self.idle = IdleWatcher(float(self.settings["idleThresholdMinutes"]) * 60)
        self.display = DisplayWatcher()  # log display off/on, repaint after a wake (OS API, see the display section)
        self.audio = AudioClient()
        self.registry = None
        self.registry_read_at = -1e9  # force the first read
        self.api = None
        self.running = False
        self.surfaces = []  # enabled surfaces taking part
        self.page_of = {}  # surface id -> its show page number (one page per key grid)
        self.grid_of_page = {}  # show page number -> (cols, rows) drawn on it
        self.saved_pages = {}  # surface id -> page to return to
        self.grids = {}  # show page number -> GridShow
        self.missing_cells = set()  # (page, row, col) with no button (404), logged once
        # Module mode: the Companion module reports the "Castika
        # Show Key" buttons the user placed; the show then runs on those, in
        # place, like the Stream Deck plugin -- no page switching at all. The
        # page-import mode above is the fallback when the module is not there.
        self.module = ModuleState()
        self.module.surfaces_provider = self._surfaces_for_module
        self.module_http = serve(self.module)
        self.module_surface_pages = {}  # surface id -> page to switch to on show start
        self.module_surface_modes = {}  # surface id -> "off" | "keys" | page number
        self._module_keys_version = -1
        self._module_settings_version = 0
        self.module_settings = {}
        self.module_mode = False  # the running show is a module (in-place) show

    # -- registry / readiness -------------------------------------------------
    def refresh(self, force=False):
        if force or time.monotonic() - self.registry_read_at > REGISTRY_REFRESH_S:
            self.registry = read_registry()
            self.registry_read_at = time.monotonic()
            self.api = CompanionApi(self.settings["companionHost"], self.registry.http_port, self.registry.tcp_port)
        return self.registry

    def ready(self):
        """Everything a show needs, or a log line saying what is missing."""
        reg = self.refresh()
        if reg.data_dir is None:
            return self._not("Companion has no data folder yet (never run?)")
        if not self.api.alive():
            return self._not("Companion is not running (no answer on http://%s:%s)" % (self.settings["companionHost"], reg.http_port))
        if not reg.tcp_enabled:
            return self._not("Companion's TCP API is off: Settings > Protocols > TCP Listener (page switching needs it)")
        disabled = set(self.settings.get("disabledSurfaces") or [])
        # One show page per distinct button grid; the pages are whatever copies of
        # the page file the user imported, handed out in page order.
        pages = reg.pages_named(self.settings["showPageName"])
        grids, taking_part = [], []
        for sf in reg.surfaces:
            if not sf.enabled or sf.id in disabled:
                continue
            # No reliable "attached right now" signal exists: Companion keeps the
            # surface_<id>_page variable even for a deck another program holds
            # (Mini under the Stream Deck app, 2026-09-20 12:2x). Page state is
            # per configured surface, so switching an absent deck is harmless.
            taking_part.append(sf)
            if sf.keys not in grids:
                grids.append(sf.keys)
        if not taking_part:
            return self._not("no enabled surface (%d configured)" % len(reg.surfaces))
        if len(pages) < len(grids):
            path = write_page_file(ROOT / "companion")
            need = len(grids) - len(pages)
            return self._not("import '%s' in Companion (Import / Export > Import > Buttons tab) %d more time(s): "
                             "one page per kind of deck (%s), %d page(s) found" %
                             (path, need, ", ".join(f"{c}x{r}" for c, r in grids), len(pages)))
        page_for_grid = dict(zip(grids, pages))
        self.surfaces = taking_part
        self.page_of = {sf.id: page_for_grid[sf.keys] for sf in taking_part}
        self.grid_of_page = {page: grid for grid, page in page_for_grid.items()}
        return True

    def poll_settings(self):
        """Re-read $(custom:castika) and apply changes: live for what the show
        can change on the fly (style, colors, sensitivity...), next start for the
        rest (surfaces, start_after)."""
        if self.api is None or time.monotonic() - self._settings_read_at < SETTINGS_POLL_S:
            return
        self._settings_read_at = time.monotonic()
        try:
            text = self.api.custom_variable(SETTINGS_VARIABLE)
        except OSError:
            return
        if text == self._variable_text:
            return
        self._variable_text = text
        overrides, errors = parse_settings_variable(text) if text is not None else ({}, [])
        for err in errors:
            log.warning("$(custom:%s): %s", SETTINGS_VARIABLE, err)
        self._variable_overrides = overrides
        self._apply_settings_layers()

    def _apply_settings_layers(self):
        """settings = file < $(custom:castika) < module config form."""
        new = dict(self.file_settings)
        new.update(getattr(self, "_variable_overrides", {}) or {})
        new.update(self.module_settings)
        changed = {k: v for k, v in new.items() if self.settings.get(k) != v}
        if not changed:
            return
        self.settings = new
        log.info("settings changed: %s", changed)
        self.idle.threshold_s = float(self.settings["idleThresholdMinutes"]) * 60
        if self.running:
            if not self.settings.get("enabled", True):
                self.stop("show=off in settings")
                return
            fresh = show_settings(self.settings)
            for grid in self.grids.values():
                grid.s = fresh
                grid.reset()
            set_daemon_active(True, gate_ms=int(self.settings.get("gateMs") or 0), adapter=ADAPTER)

    def _surfaces_for_module(self):
        reg = self.registry or self.refresh()
        return {"surfaces": [{"id": sf.id, "name": f"{sf.type} ({sf.id})", "short": sf.type,
                              "keys": list(sf.keys), "page": sf.page}
                             for sf in reg.surfaces if sf.enabled],
                # Companion's own pages, so the form can offer them instead of a
                # free number that may point nowhere (사용자 2026-09-29).
                "pages": [{"n": n, "name": name} for n, name in sorted(reg.pages.items())],
                "tcp": bool(reg.tcp_enabled)}

    def poll_module(self):
        """Pick up what the module sent: key list, settings form, presses."""
        m = self.module
        with m.lock:
            keys_version, settings_version = m.keys_version, m.settings_version
            settings, pressed = m.settings, m.pressed
            session_off, m.session_off = m.session_off, None
            m.pressed = []
        if settings_version != self._module_settings_version:
            self._module_settings_version = settings_version
            self.module_settings = _module_form_to_settings(settings or {})
            # per deck: "off" | "keys" | page number (module form, 사용자 제안: 기기별 참여 여부 + 전환 페이지)
            modes = (settings or {}).get("surfaces") or {}
            self.module_surface_modes = {str(k): (int(v) if isinstance(v, (int, float)) else str(v)) for k, v in modes.items()}
            self.module_surface_pages = {k: v for k, v in self.module_surface_modes.items() if isinstance(v, int) and v > 0}
            if self.module_surface_modes:
                log.info("module: decks %s", {k[:24]: v for k, v in self.module_surface_modes.items()})
            self._apply_settings_layers()
        if keys_version != self._module_keys_version:
            self._module_keys_version = keys_version
            with m.lock:
                n = len(m.keys)
                sizes = sorted({int(k["size"]) for k in m.keys.values()})
            # The size comes from the surface itself, whatever it is (a Stream
            # Deck, a Loupedeck, the built-in emulator): we draw at what it asks
            # for, so this is how an unknown surface tells us its pixels.
            log.info("module: %d placed button(s), image size(s): %s", n,
                     ", ".join("%dpx" % v for v in sizes) or "none")
            if self.running and self.module_mode:
                self._build_module_grids()  # keys came or went mid-show
        if session_off is not None:
            self.session_off = session_off
            if session_off:
                log.info("module: show off for this session (long press) -> no auto-start until a button is pressed")
                if self.running:
                    self.stop("off for this session")
            else:
                log.info("module: show allowed again this session")
        for kid in pressed:
            if self.running:
                self.stop(f"button {kid} pressed")
            else:
                if self.session_off:
                    self.session_off = False  # a press brings it back, as on the deck itself
                    log.info("button pressed -> show allowed again this session")
                log.info("button %s pressed -> starting show", kid)
                self.start()

    def _pages_viewed_by_participants(self):
        """Page numbers shown by decks that take part (mode "keys" or a switch
        page), or None when the deck list is unknown (then every page counts)."""
        reg = self.registry or self.refresh()
        if not reg.surfaces:
            return None
        pages = set()
        for sf in reg.surfaces:
            if not sf.enabled:
                continue
            mode = self.module_surface_modes.get(sf.id, "keys")
            if mode == "off":
                continue
            if isinstance(mode, int):
                pages.add(mode)
                continue
            try:
                page = self.api.surface_page(sf) if self.api else None
            except OSError:
                page = None
            pages.add(page or sf.page)
        return pages

    def _switch_module_surfaces(self):
        """Module-form "switch <deck> to page N on idle": TCP page-set each such
        deck (remembering where it was) so the buttons the user placed on page N
        fill the deck; 0 / unset = the deck stays where it is."""
        self.saved_pages = {}
        if not self.module_surface_pages:
            return
        reg = self.refresh()
        if not reg.tcp_enabled:
            return self._not("switch-to-page needs Companion's TCP API: Settings > Protocols > TCP Listener")
        by_id = {sf.id: sf for sf in reg.surfaces}
        for sid, page in self.module_surface_pages.items():
            sf = by_id.get(sid)
            if sf is None:
                continue
            try:
                current = self.api.surface_page(sf) or sf.page
                if current == page:
                    continue  # already there: nothing to remember, nothing to restore
                self.saved_pages[sid] = current
                self.api.set_surface_page(sid, page)
            except OSError as e:
                log.warning("page-set %s -> %s failed: %s", sid, page, e)

    def _restore_module_surfaces(self):
        for sid, page in self.saved_pages.items():
            try:
                self.api.set_surface_page(sid, page)
            except OSError as e:
                log.warning("restore %s -> %s failed: %s", sid, page, e)
        self.saved_pages = {}

    def _module_keys(self):
        with self.module.lock:
            return dict(self.module.keys)

    def _build_module_grids(self):
        """One GridShow per Companion page that has placed buttons, laid out on the
        keys' bounding box like the Stream Deck plugin does per device."""
        keys = self._module_keys()
        settings = show_settings(self.settings)
        # Only pages some participating deck is looking at get drawn: a deck set
        # to "off" is left alone even if its page has show buttons, and a page no
        # deck shows is not worth rendering.
        viewed = self._pages_viewed_by_participants()
        self._module_viewed = viewed  # so the loop notices a deck turning to another page
        by_page = {}
        for kid, k in keys.items():
            if viewed is not None and k["page"] not in viewed:
                continue
            by_page.setdefault(k["page"], {})[(k["row"], k["col"])] = (kid, k["size"])
        self.grids = {}
        self.module_key_ids = {}  # page -> {(row, col): key id}
        for page, cells in by_page.items():
            rows = max(r for r, _ in cells) + 1
            cols = max(c for _, c in cells) + 1
            size = max(sz for _, sz in cells.values())
            self.grids[page] = GridShow(cols, rows, size, settings, keys=set(cells))
            self.module_key_ids[page] = {pos: kid for pos, (kid, _) in cells.items()}
        log.info("module show layout: %s", {p: f"{g.cols}x{g.rows} ({len(g.keys)} buttons)" for p, g in self.grids.items()})

    def _not(self, why):
        if getattr(self, "_last_not", None) != why:
            log.info("not ready: %s", why)
            self._last_not = why
        return False

    # -- show ---------------------------------------------------------------------
    def start(self):
        if self.running:
            return
        module_alive = time.monotonic() - self.module.last_seen < MODULE_STALE_S
        if not self._module_keys() and module_alive:
            # The module is connected but the user has not placed any button yet:
            # say so, and do not fall back to page switching behind their back.
            return self._not("Companion module connected, but no \"DeckShow Button\" placed yet (Buttons > Presets > Castika DeckShow)")
        if self._module_keys() and module_alive:  # stale keys from a Companion that quit do not count
            self.module_mode = True
            self._switch_module_surfaces()
            self._build_module_grids()
            self.running = True
            self._show_started_at = time.monotonic()
            with self.module.lock:
                self.module.running = True
            set_daemon_active(True, gate_ms=int(self.settings.get("gateMs") or 0), adapter=ADAPTER)
            log.info("show started in place on the module's buttons, %d page(s)", len(self.grids))
            return
        if not self.ready():
            return
        self.module_mode = False
        self.saved_pages = {}
        self.grids = {}
        self.missing_cells.clear()
        settings = show_settings(self.settings)
        for sf in self.surfaces:
            page = self.page_of[sf.id]
            current = self.api.surface_page(sf) or sf.page
            if current == page:
                # Already sitting on its show page (left there by a crash, or by
                # the user): the buttons' "Back" would lead right back here and a
                # button press could never be noticed. Step off first so there is
                # somewhere to go back to -- to the page Companion's OWN startup
                # setting names for this surface, never a page of our choosing
                # (사용자 지적 2026-09-20: Companion's start-page settings are the
                # user's; this adapter only reads them).
                current = sf.startup_page
                self.api.set_surface_page(sf.id, current)
            self.saved_pages[sf.id] = current
            if page not in self.grids:
                cols, rows = self.grid_of_page[page]
                self.grids[page] = GridShow(cols, rows, sf.key_size, settings)
            log.info("%s (%s, buttons %dx%d) on page %s -> show page %s", sf.id, sf.type, sf.keys[0], sf.keys[1], self.saved_pages[sf.id], page)
            try:
                self.api.set_surface_page(sf.id, page)
            except OSError as e:
                log.warning("page-set failed for %s: %s", sf.id, e)
        self.running = True
        self._show_started_at = time.monotonic()
        set_daemon_active(True, gate_ms=int(self.settings.get("gateMs") or 0), adapter=ADAPTER)
        log.info("show started: %d surface(s) on page(s) %s", len(self.surfaces), sorted(self.grids))

    def stop(self, reason):
        if not self.running:
            return
        log.info("stopping show: %s", reason)
        self.running = False
        self._show_started_at = 0.0
        set_daemon_active(False, adapter=ADAPTER)
        if self.module_mode:
            with self.module.lock:
                self.module.running = False
            self.module.clear_frames()  # module puts the buttons back to their own style
            self.grids = {}
            self._restore_module_surfaces()
            return
        # png64 "" is accepted (HTTP 200) but leaves the last picture in place on
        # a Companion 5 layered button, so blank with a black image instead.
        black = "data:image/png;base64," + base64.b64encode(solid_color_png_bytes((0, 0, 0), 72)).decode("ascii")
        for page, grid in self.grids.items():
            for (row, col) in list(grid.last):
                try:
                    self.api.set_style_png(page, row, col, black)
                except Exception as e:  # noqa: BLE001 -- best effort, Companion may be gone
                    log.warning("clear %s/%s,%s: %s", page, row, col, e)
                    break
        for sf in self.surfaces:
            try:
                if self.api.surface_page(sf) == self.page_of.get(sf.id):
                    self.api.set_surface_page(sf.id, self.saved_pages.get(sf.id, 1))
            except Exception as e:  # noqa: BLE001
                log.warning("restore %s: %s", sf.id, e)
        self.grids = {}

    def run(self):
        log.info("starting; settings %s", SETTINGS_FILE)
        last_beat = last_idle = last_page_poll = last_display = 0.0
        period = 1.0 / RENDER_FPS
        while not self.shutdown.is_set():
            now = time.monotonic()
            self.refresh()  # registry snapshot + API client, throttled
            self.poll_settings()
            self.poll_module()
            if now - last_display >= DISPLAY_POLL_S:
                last_display = now
                edge = self.display.poll()
                if edge == "slept":
                    log.info("display turned off (show %s)", "keeps running" if self.running else "not running")
                elif edge == "woke":
                    log.info("display turned on -> repaint everything")
                    for grid in self.grids.values():
                        grid.last.clear()  # every key is re-sent on the next frame
            if not self.running:
                if now - last_idle >= IDLE_POLL_S:
                    last_idle = now
                    edge = self.idle.poll()
                    forced = COMPANION_FORCE_START_MARKER.exists()
                    if forced:
                        COMPANION_FORCE_START_MARKER.unlink()
                        log.info("force-start marker found (testing only)")
                    if self.session_off and edge == "became_idle":
                        log.info("idle, but the show is off for this session (long press) -> not starting")
                    if (edge == "became_idle" and self.settings.get("enabled", True)
                            and not self.session_off) or forced:
                        self.start()
                time.sleep(0.5)
                continue
            # Running: typing or moving the mouse ends the show, like a screen saver.
            if self.settings.get("stopOnActivity", DEFAULT_STOP_ON_ACTIVITY) and now - self._last_activity_poll >= ACTIVE_POLL_INTERVAL_S:
                self._last_activity_poll = now
                if user_active_since(self._show_started_at):
                    self.stop("keyboard or mouse used")
                    continue
            if now - last_beat >= HEARTBEAT_S:
                set_daemon_active(True, gate_ms=int(self.settings.get("gateMs") or 0), adapter=ADAPTER)
                last_beat = now
            if self.module_mode:
                # A deck turned to another page: draw on that page's buttons
                # instead (사용자 2026-09-29: "지금 첫 페이지인데?" -- the grids were
                # built for the page a deck showed when the show started).
                if now - last_page_poll >= PAGE_POLL_S:
                    last_page_poll = now
                    viewed = self._pages_viewed_by_participants()
                    if viewed != self._module_viewed:
                        log.info("deck turned to page(s) %s -> rebuilding the show there",
                                 sorted(viewed) if viewed else "unknown")
                        self._build_module_grids()
                        if not self.grids:
                            self.stop("no DeckShow Button on the page the deck shows")
                            continue
                if time.monotonic() - self.module.last_seen > MODULE_STALE_S:
                    # Companion quit (or the connection was disabled) while the show ran:
                    # nobody is fetching frames, so stop and let the daemon close the mic.
                    self.stop("Companion module stopped polling (Companion quit?)")
                    continue
                try:
                    images = {}
                    for page, grid in self.grids.items():
                        ids = self.module_key_ids[page]
                        for pos, png in grid.frame(self.audio).items():
                            images[ids[pos]] = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
                    self.module.put_frames(images)
                except Exception as e:  # noqa: BLE001
                    log.exception("frame failed")
                    self.stop(f"draw error: {e}")
                    continue
                time.sleep(period)
                continue
            if now - last_page_poll >= PAGE_POLL_S:
                last_page_poll = now
                try:
                    for sf in self.surfaces:
                        page = self.api.surface_page(sf)
                        if page is not None and page != self.page_of.get(sf.id):
                            self.stop(f"{sf.id} left its show page (button pressed) -> page {page}")
                            break
                except OSError as e:
                    self.stop(f"Companion unreachable ({e})")
                if not self.running:
                    continue
            try:
                for page, grid in self.grids.items():
                    for (row, col), png in grid.frame(self.audio).items():
                        if (page, row, col) in self.missing_cells:
                            continue
                        try:
                            self.api.set_style_png(page, row, col, "data:image/png;base64," + base64.b64encode(png).decode("ascii"))
                        except Exception as e:  # noqa: BLE001
                            if "404" in str(e):
                                self.missing_cells.add((page, row, col))
                                log.warning("show page %s has no button at row %s col %s -- skipped", page, row, col)
                            else:
                                raise
            except Exception as e:  # noqa: BLE001
                log.exception("frame failed")
                self.stop(f"draw error: {e}")
                continue
            time.sleep(period)


# ============================================================================
# Entry point
# ============================================================================
def companion_main():
    show = CompanionShow()

    # A handler runs in the middle of whatever the main thread was doing, so it
    # only raises a flag. Handing the pages back from inside it deadlocked on a
    # lock the interrupted code was holding, and the process then ignored every
    # further signal (2026-09-29: SIGTERM left the old host alive).
    def _terminate(signum, frame):
        show.shutdown.set()

    for sig in (signal.SIGTERM, signal.SIGINT) + ((signal.SIGHUP,) if hasattr(signal, "SIGHUP") else ()):
        signal.signal(sig, _terminate)
    try:
        show.run()
    except KeyboardInterrupt:
        pass
    finally:
        show.stop("signal")  # hand the pages back and close the mic before exiting


def host_main():
    """One process for both background roles (what the login item runs): the
    microphone daemon in a thread, the Companion adapter in the main thread.
    If either side dies the whole process exits, and the launcher (launchd on
    macOS, the Windows launcher) starts it again."""
    stop_event = threading.Event()

    def daemon_thread():
        try:
            daemon_main(stop_event)
        except BaseException:  # noqa: BLE001 - nothing may keep running without the mic loop
            log.exception("audio daemon thread died -> exiting so the launcher restarts the host")
            os._exit(1)
        if not stop_event.is_set():
            log.error("audio daemon thread ended on its own -> exiting so the launcher restarts the host")
            os._exit(1)

    threading.current_thread().name = "COMPANION"
    threading.Thread(target=daemon_thread, name="AUDIO-DAEMON", daemon=True).start()
    show = CompanionShow()

    # Flag only: see companion_main() for why the handler does no work here.
    def _terminate(signum, frame):
        show.shutdown.set()

    for sig in (signal.SIGTERM, signal.SIGINT) + ((signal.SIGHUP,) if hasattr(signal, "SIGHUP") else ()):
        signal.signal(sig, _terminate)
    try:
        show.run()
    except KeyboardInterrupt:
        pass
    finally:
        show.stop("signal")  # hand the pages back before the mic goes
        stop_event.set()
        time.sleep(0.3)  # let the daemon thread close the microphone and write "stopped"
        # Closing the audio stream can deadlock inside CoreAudio/PortAudio (seen
        # 2026-09-29: every thread waiting on a mutex), and a host that hangs
        # keeps the adapter's port, so the next one cannot bind. The work above
        # is done by now, so leave without waiting for the audio stack.
        os._exit(0)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = argv[0] if argv else ""
    if mode == "host":
        _setup_logging_host()
        host_main()
    elif mode == "daemon":
        _setup_logging("deckshow.log", "AUDIO-DAEMON")
        daemon_main()
    elif mode == "companion":
        _setup_logging("companion.log", "COMPANION")
        companion_main()
    elif mode == "plugin":
        _setup_logging("plugin.log", "")
        asyncio.run(plugin_main(argv[1:]))
    else:
        print("usage: deckshow.py host | daemon | companion | plugin <Stream Deck SDK args>", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
