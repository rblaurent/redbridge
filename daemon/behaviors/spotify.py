"""Spotify now-playing display and controls.

Five behaviors that share module-level state via a background poller:

- spotify_strip       (strip)       — track info and progress bar
- spotify_album_art   (key)         — album cover, press -> next track
- spotify_logo        (key)         — logo icon, press -> open/focus Spotify
- spotify_volume      (dial rotate) — per-app volume via pycaw
- spotify_play_pause  (dial press)  — toggle playback
"""

from __future__ import annotations

import asyncio
import ctypes
import io
import os
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass

from PIL import Image, ImageDraw

from behaviors.base import Behavior, EventBus, Target, TargetKind
from gfx import font
from registry import register
from win_focus import focus_window

_ASSETS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SPOTIFY_GREEN = (30, 185, 84)
DIM_GREY = (80, 80, 80)
BAR_BG = (42, 42, 42)
POLL_INTERVAL = 0.5

VK_MEDIA_PLAY_PAUSE = 0xB3
VK_MEDIA_NEXT_TRACK = 0xB0
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

_user32.keybd_event.argtypes = [
    wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_void_p,
]
_user32.keybd_event.restype = None
_user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
_user32.EnumWindows.restype = wintypes.BOOL
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

@dataclass
class _SpotifyState:
    active: bool = False
    track: str = ""
    artist: str = ""
    album: str = ""
    is_playing: bool = False
    position_ms: int = 0
    duration_ms: int = 0
    album_art_bytes: bytes | None = None
    volume: float = 0.0
    last_update: float = 0.0


_lock = threading.Lock()
_state = _SpotifyState()


def _snap() -> _SpotifyState:
    with _lock:
        return _SpotifyState(
            active=_state.active,
            track=_state.track,
            artist=_state.artist,
            album=_state.album,
            is_playing=_state.is_playing,
            position_ms=_state.position_ms,
            duration_ms=_state.duration_ms,
            album_art_bytes=_state.album_art_bytes,
            volume=_state.volume,
            last_update=_state.last_update,
        )


# ---------------------------------------------------------------------------
# Media keys
# ---------------------------------------------------------------------------

def _press_media_key(vk: int) -> None:
    _user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY, None)
    _user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, None)


# ---------------------------------------------------------------------------
# Volume (pycaw)
# ---------------------------------------------------------------------------

def _init_com() -> None:
    try:
        import comtypes
        comtypes.CoInitialize()
    except (ImportError, OSError):
        pass


def _get_spotify_volume() -> float:
    _init_com()
    try:
        from pycaw.pycaw import AudioUtilities
        for s in AudioUtilities.GetAllSessions():
            if s.Process and "spotify" in s.Process.name().lower():
                return s.SimpleAudioVolume.GetMasterVolume()
    except Exception:
        pass
    return 0.0


def _set_spotify_volume(level: float) -> None:
    _init_com()
    try:
        from pycaw.pycaw import AudioUtilities
        for s in AudioUtilities.GetAllSessions():
            if s.Process and "spotify" in s.Process.name().lower():
                s.SimpleAudioVolume.SetMasterVolume(
                    max(0.0, min(1.0, level)), None,
                )
                return
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Window helpers
# ---------------------------------------------------------------------------

def _find_spotify_hwnd() -> int:
    result = [0]

    def _cb(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if title == "Spotify" or title.endswith(" - Spotify"):
            result[0] = hwnd
            return False
        return True

    _user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    return result[0]


# ---------------------------------------------------------------------------
# Background poller
# ---------------------------------------------------------------------------

_poller_started = False
_poller_start_lock = threading.Lock()


async def _poll_loop() -> None:
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    )
    from winsdk.windows.storage.streams import DataReader

    manager = await SessionManager.request_async()
    prev_track_key: tuple[str, str] = ("", "")
    cached_art: bytes | None = None

    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            session = None
            for s in manager.get_sessions():
                if "spotify" in (s.source_app_user_model_id or "").lower():
                    session = s
                    break

            if session is None:
                with _lock:
                    _state.active = False
                continue

            props = await session.try_get_media_properties_async()
            timeline = session.get_timeline_properties()
            playback = session.get_playback_info()

            track = props.title or ""
            artist = props.artist or ""
            track_key = (track, artist)

            if track_key != prev_track_key:
                prev_track_key = track_key
                cached_art = None
                try:
                    thumb = props.thumbnail
                    if thumb:
                        stream = await thumb.open_read_async()
                        size = int(stream.size)
                        if size > 0:
                            reader = DataReader(stream)
                            await reader.load_async(size)
                            cached_art = bytes(reader.read_buffer(size))
                except Exception:
                    pass

            pos = timeline.position
            end = timeline.end_time
            pos_ms = (
                int(pos.total_seconds() * 1000)
                if hasattr(pos, "total_seconds")
                else 0
            )
            dur_ms = (
                int(end.total_seconds() * 1000)
                if hasattr(end, "total_seconds")
                else 0
            )

            with _lock:
                _state.active = True
                _state.track = track
                _state.artist = artist
                _state.album = props.album_title or ""
                _state.is_playing = int(playback.playback_status) == 4
                _state.position_ms = pos_ms
                _state.duration_ms = dur_ms
                _state.album_art_bytes = cached_art
                _state.last_update = time.monotonic()

        except Exception as e:
            print(f"[spotify] poll error: {e}", flush=True)
            with _lock:
                _state.active = False


def _poller_entry() -> None:
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_poll_loop())
    except ImportError as e:
        print(
            f"[spotify] winsdk not installed — run: uv pip install winsdk\n  {e}",
            flush=True,
        )
    except Exception as e:
        print(f"[spotify] poller crashed: {e}", flush=True)


def _ensure_poller() -> None:
    global _poller_started
    with _poller_start_lock:
        if _poller_started:
            return
        _poller_started = True
        threading.Thread(
            target=_poller_entry, daemon=True, name="spotify-poller",
        ).start()


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def _truncate(draw: ImageDraw.ImageDraw, text: str, f, max_w: int) -> str:
    if draw.textlength(text, font=f) <= max_w:
        return text
    for end in range(len(text), 0, -1):
        if draw.textlength(text[:end] + "…", font=f) <= max_w:
            return text[:end] + "…"
    return "…"


def _format_ms(ms: int) -> str:
    total = max(0, ms // 1000)
    m, s = divmod(total, 60)
    return f"{m}:{s:02d}"


def _round_corners(img: Image.Image, radius: int) -> Image.Image:
    ss = 4
    big = Image.new("L", (img.width * ss, img.height * ss), 0)
    ImageDraw.Draw(big).rounded_rectangle(
        (0, 0, img.width * ss - 1, img.height * ss - 1), radius * ss, fill=255,
    )
    mask = big.resize(img.size, Image.LANCZOS)
    result = Image.new("RGB", img.size, (0, 0, 0))
    result.paste(img, mask=mask)
    return result


# ---------------------------------------------------------------------------
# Strip — track info + progress bar
# ---------------------------------------------------------------------------

@register
class SpotifyStrip(Behavior):
    type_id = "spotify_strip"
    display_name = "Spotify strip"
    targets = {TargetKind.STRIP_REGION}
    config_schema: dict = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        _ensure_poller()
        self._prev_key: tuple = ()

    def tick(self) -> bool:
        s = _snap()
        key = (s.active, s.track, s.artist, s.is_playing, s.position_ms // 1000)
        if key != self._prev_key:
            self._prev_key = key
            return True
        return False

    def render(self) -> Image.Image | None:
        w, h = self.size()
        s = _snap()
        img = Image.new("RGB", (w, h), (0, 0, 0))
        draw = ImageDraw.Draw(img)

        if not s.active or not s.track:
            draw.text(
                (w // 2, h // 2), "No music playing",
                fill=DIM_GREY, font=font(14), anchor="mm",
            )
            return img

        tf = font(15)
        draw.text(
            (8, 14), _truncate(draw, s.track, tf, w - 16),
            fill=(255, 255, 255), font=tf, anchor="lm",
        )

        af = font(12)
        draw.text(
            (8, 34), _truncate(draw, s.artist, af, w - 16),
            fill=(160, 160, 160), font=af, anchor="lm",
        )

        time_str = f"{_format_ms(s.position_ms)} / {_format_ms(s.duration_ms)}"
        draw.text(
            (8, 58), time_str,
            fill=(100, 100, 100), font=font(11), anchor="lm",
        )

        bx1, bx2, by, bh = 8, w - 8, 78, 6
        bar_r = bh // 2
        draw.rounded_rectangle((bx1, by, bx2, by + bh), bar_r, fill=BAR_BG)
        if s.duration_ms > 0:
            pct = min(1.0, s.position_ms / s.duration_ms)
            fill_x = bx1 + int((bx2 - bx1) * pct)
            if fill_x > bx1 + bar_r:
                draw.rounded_rectangle(
                    (bx1, by, fill_x, by + bh), bar_r, fill=SPOTIFY_GREEN,
                )

        return img


# ---------------------------------------------------------------------------
# Album art key — cover image, press -> next track
# ---------------------------------------------------------------------------

@register
class SpotifyAlbumArt(Behavior):
    type_id = "spotify_album_art"
    display_name = "Spotify album art"
    targets = {TargetKind.KEY}
    config_schema: dict = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        _ensure_poller()
        self._prev_key: tuple = ()
        self._art_img: Image.Image | None = None
        self._art_bytes_id: int = 0

    def tick(self) -> bool:
        s = _snap()
        key = (s.active, s.track, s.artist, id(s.album_art_bytes))
        if key != self._prev_key:
            self._prev_key = key
            return True
        return False

    def render(self) -> Image.Image | None:
        w, h = self.size()
        s = _snap()

        art = s.album_art_bytes
        art_id = id(art) if art else 0
        if art and art_id != self._art_bytes_id:
            try:
                raw = Image.open(io.BytesIO(art)).convert("RGB")
                cw, ch = raw.size
                inset = int(min(cw, ch) * 0.05)
                raw = raw.crop((inset, inset, cw - inset, ch - inset))
                aw, ah = int(w * 0.9), int(h * 0.9)
                raw = raw.resize((aw, ah), Image.LANCZOS)
                rounded = _round_corners(raw, 16)
                canvas = Image.new("RGB", (w, h), (0, 0, 0))
                canvas.paste(rounded, ((w - aw) // 2, (h - ah) // 2))
                self._art_img = canvas
            except Exception:
                self._art_img = None
            self._art_bytes_id = art_id
        elif not art:
            self._art_img = None
            self._art_bytes_id = 0

        if self._art_img:
            return self._art_img.copy()

        return Image.new("RGB", (w, h), (0, 0, 0))

    def on_press(self) -> None:
        _press_media_key(VK_MEDIA_NEXT_TRACK)


# ---------------------------------------------------------------------------
# Logo key — Spotify icon, press -> open / focus
# ---------------------------------------------------------------------------

@register
class SpotifyLogo(Behavior):
    type_id = "spotify_logo"
    display_name = "Spotify logo"
    targets = {TargetKind.KEY}
    config_schema: dict = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        _ensure_poller()
        self._logo: Image.Image | None = None

    def render(self) -> Image.Image | None:
        if self._logo:
            return self._logo.copy()

        w, h = self.size()
        logo_path = os.path.join(_ASSETS, "spotify_logo.png")
        try:
            self._logo = Image.open(logo_path).convert("RGB").resize((w, h), Image.LANCZOS)
        except Exception:
            img = Image.new("RGB", (w, h), (0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.ellipse((10, 10, w - 10, h - 10), fill=SPOTIFY_GREEN)
            self._logo = img
        return self._logo.copy()

    def on_press(self) -> None:
        hwnd = _find_spotify_hwnd()
        if hwnd:
            focus_window(hwnd)
        else:
            try:
                os.startfile("spotify:")
            except Exception as e:
                print(f"[spotify] launch failed: {e}", flush=True)


# ---------------------------------------------------------------------------
# Volume dial — per-app volume via pycaw
# ---------------------------------------------------------------------------

@register
class SpotifyVolume(Behavior):
    type_id = "spotify_volume"
    display_name = "Spotify volume"
    targets = {TargetKind.DIAL_ROTATE}
    config_schema: dict = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        _ensure_poller()

    def on_rotate(self, delta: int) -> None:
        current = _get_spotify_volume()
        _set_spotify_volume(current + delta * 0.02)


# ---------------------------------------------------------------------------
# Play/pause dial press
# ---------------------------------------------------------------------------

@register
class SpotifyPlayPause(Behavior):
    type_id = "spotify_play_pause"
    display_name = "Spotify play/pause"
    targets = {TargetKind.DIAL_PRESS}
    config_schema: dict = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        _ensure_poller()

    def on_press(self) -> None:
        _press_media_key(VK_MEDIA_PLAY_PAUSE)
