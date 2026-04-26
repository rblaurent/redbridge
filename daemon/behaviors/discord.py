"""Discord voice channel display and controls.

Five behaviors that share module-level state via a background RPC client:

- discord_strip       (strip)       — channel info, users, mute/deafen status
- discord_guild_icon  (key)         — server icon, press -> toggle mute
- discord_logo        (key)         — logo icon, press -> open/focus Discord
- discord_volume      (dial rotate) — per-app volume via pycaw
- discord_deafen      (dial press)  — toggle deafen
"""

from __future__ import annotations

import asyncio
import ctypes
import io
import json
import os
import struct
import threading
import time
import uuid
from ctypes import wintypes
from dataclasses import dataclass, field

from PIL import Image, ImageDraw

from behaviors.base import Behavior, EventBus, Target, TargetKind
from gfx import font
from registry import register
from win_focus import focus_window

_ASSETS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")
_TOKEN_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".discord_token")
_LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".discord.log")


def _log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(f"[discord] {msg}", flush=True)
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DISCORD_CLIENT_ID = "1372561907680149644"
DISCORD_CLIENT_SECRET = "WCVByCOz2eR7aywq4xe1RIgnMRr0Vh9i"
DISCORD_REDIRECT_URI = "http://localhost"
DISCORD_IPC_PIPES = ["\\\\.\\pipe\\discord-ipc-" + str(i) for i in range(10)]
RECONNECT_DELAY = 5.0
AUTH_FAILURE_DELAY = 30.0
CHANNEL_POLL_INTERVAL = 3.0

DISCORD_BLURPLE = (88, 101, 242)
DISCORD_GREEN = (87, 242, 135)
DISCORD_RED = (237, 66, 69)
DISCORD_ORANGE = (250, 168, 26)
DIM_GREY = (80, 80, 80)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

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
class _DiscordState:
    connected: bool = False
    in_voice: bool = False
    channel_name: str = ""
    guild_name: str = ""
    guild_id: str = ""
    guild_icon_hash: str = ""
    guild_icon_bytes: bytes | None = None
    users: list[str] = field(default_factory=list)
    muted: bool = False
    deafened: bool = False
    volume: float = 0.0
    last_update: float = 0.0


_lock = threading.Lock()
_state = _DiscordState()


def _snap() -> _DiscordState:
    with _lock:
        return _DiscordState(
            connected=_state.connected,
            in_voice=_state.in_voice,
            channel_name=_state.channel_name,
            guild_name=_state.guild_name,
            guild_id=_state.guild_id,
            guild_icon_hash=_state.guild_icon_hash,
            guild_icon_bytes=_state.guild_icon_bytes,
            users=list(_state.users),
            muted=_state.muted,
            deafened=_state.deafened,
            volume=_state.volume,
            last_update=_state.last_update,
        )


# ---------------------------------------------------------------------------
# Volume (pycaw)
# ---------------------------------------------------------------------------

def _init_com() -> None:
    try:
        import comtypes
        comtypes.CoInitialize()
    except (ImportError, OSError):
        pass


def _get_discord_volume() -> float:
    _init_com()
    try:
        from pycaw.pycaw import AudioUtilities
        for s in AudioUtilities.GetAllSessions():
            if s.Process and "discord" in s.Process.name().lower():
                return s.SimpleAudioVolume.GetMasterVolume()
    except Exception:
        pass
    return 0.0


def _set_discord_volume(level: float) -> None:
    _init_com()
    try:
        from pycaw.pycaw import AudioUtilities
        for s in AudioUtilities.GetAllSessions():
            if s.Process and "discord" in s.Process.name().lower():
                s.SimpleAudioVolume.SetMasterVolume(
                    max(0.0, min(1.0, level)), None,
                )
                return
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Window helpers
# ---------------------------------------------------------------------------

def _find_discord_hwnd() -> int:
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
        if title == "Discord" or title.endswith("- Discord"):
            result[0] = hwnd
            return False
        return True

    _user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    return result[0]


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------

def _load_token() -> dict | None:
    try:
        with open(_TOKEN_PATH, "r") as f:
            data = json.load(f)
        if data.get("access_token"):
            _log(f"loaded token from {_TOKEN_PATH}")
            return data
        _log(f"token file exists but no access_token key")
    except FileNotFoundError:
        _log(f"no token file at {_TOKEN_PATH}")
    except Exception as e:
        _log(f"token load error: {e}")
    return None


def _save_token(data: dict) -> None:
    with open(_TOKEN_PATH, "w") as f:
        json.dump(data, f)


def _delete_token() -> None:
    try:
        os.remove(_TOKEN_PATH)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# RPC helpers
# ---------------------------------------------------------------------------

def _nonce() -> str:
    return uuid.uuid4().hex[:12]




# ---------------------------------------------------------------------------
# Background RPC client
# ---------------------------------------------------------------------------

_poller_started = False
_poller_start_lock = threading.Lock()
_cmd_queue: asyncio.Queue | None = None
_auth_prompted = False
_generation = id(threading.Lock())


def _enqueue_cmd(cmd: str, args: dict) -> None:
    if _cmd_queue is not None:
        _cmd_queue.put_nowait((cmd, args))


# ---------------------------------------------------------------------------
# IPC transport (named pipes)
# ---------------------------------------------------------------------------

OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2


class _IpcPipe:
    """Discord RPC over Windows named pipe."""

    def __init__(self, handle):
        import win32file
        self._h = handle
        self._wf = win32file

    def send(self, payload: dict, opcode: int = OP_FRAME) -> None:
        raw = json.dumps(payload).encode("utf-8")
        header = struct.pack("<II", opcode, len(raw))
        self._wf.WriteFile(self._h, header + raw)

    def recv(self) -> dict:
        _, header = self._wf.ReadFile(self._h, 8)
        _op, length = struct.unpack("<II", header)
        _, data = self._wf.ReadFile(self._h, length)
        return json.loads(data.decode("utf-8"))

    def close(self) -> None:
        try:
            self._wf.CloseHandle(self._h)
        except Exception:
            pass


def _ipc_connect() -> _IpcPipe | None:
    import win32file
    for path in DISCORD_IPC_PIPES:
        try:
            h = win32file.CreateFile(
                path,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None,
            )
            return _IpcPipe(h)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# RPC session (runs synchronously on dedicated thread)
# ---------------------------------------------------------------------------

def _rpc_loop(gen: int) -> None:
    import httpx

    global _cmd_queue, _loop_ref
    loop = asyncio.new_event_loop()
    _loop_ref = loop
    _cmd_queue = asyncio.Queue()

    while gen == _generation:
        delay = RECONNECT_DELAY
        try:
            delay = _connect_and_run(httpx, gen)
        except Exception as e:
            _log(f"RPC error: {e}")

        with _lock:
            _state.connected = False
            _state.in_voice = False
        if gen != _generation:
            break
        time.sleep(delay)
    _log("old poller thread exiting")


def _connect_and_run(httpx, gen: int) -> float:
    if not _load_token():
        return AUTH_FAILURE_DELAY

    pipe = _ipc_connect()
    if pipe is None:
        return RECONNECT_DELAY

    try:
        pipe.send({"v": 1, "client_id": DISCORD_CLIENT_ID}, opcode=OP_HANDSHAKE)
        ready = pipe.recv()
        if ready.get("evt") != "READY":
            _log(f"unexpected handshake: {ready}")
            return RECONNECT_DELAY
        _log("IPC connected")

        access_token = _do_authenticate(pipe, httpx)
        if not access_token:
            return AUTH_FAILURE_DELAY

        pipe.send(_rpc_payload("SUBSCRIBE", {}, evt="VOICE_SETTINGS_UPDATE"))
        pipe.send(_rpc_payload("SUBSCRIBE", {}, evt="VOICE_CHANNEL_SELECT"))
        pipe.send(_rpc_payload("GET_SELECTED_VOICE_CHANNEL"))
        pipe.send(_rpc_payload("GET_VOICE_SETTINGS"))

        with _lock:
            _state.connected = True
            _state.last_update = time.monotonic()
        _log("RPC subscribed")

        _recv_loop_sync(pipe, httpx, gen)
        return RECONNECT_DELAY
    finally:
        pipe.close()


def _rpc_payload(cmd: str, args: dict | None = None, evt: str | None = None) -> dict:
    msg: dict = {"cmd": cmd, "nonce": _nonce()}
    if args is not None:
        msg["args"] = args
    if evt is not None:
        msg["evt"] = evt
    return msg


def _do_authenticate(pipe: _IpcPipe, httpx) -> str | None:
    token_data = _load_token()
    if not token_data:
        _log("no token — run: uv run python -m behaviors.discord_auth")
        return None

    _log("trying cached token...")
    pipe.send(_rpc_payload("AUTHENTICATE", {"access_token": token_data["access_token"]}))
    resp = pipe.recv()
    if resp.get("evt") != "ERROR":
        _log("authenticated with cached token")
        return token_data["access_token"]
    err = resp.get("data", {})
    _log(f"cached token rejected ({err.get('code')}): {err.get('message')}")
    return None


def _recv_loop_sync(pipe: _IpcPipe, httpx_mod, gen: int) -> None:
    last_poll = time.monotonic()
    while gen == _generation:
        # Drain any queued commands
        while _cmd_queue and not _cmd_queue.empty():
            try:
                cmd, args = _cmd_queue.get_nowait()
                pipe.send(_rpc_payload(cmd, args))
            except Exception as e:
                _log(f"send command error: {e}")

        try:
            msg = pipe.recv()
        except Exception as e:
            _log(f"recv error (pipe closed?): {e}")
            break

        cmd = msg.get("cmd")
        evt = msg.get("evt")
        data = msg.get("data") or {}

        if cmd == "SET_VOICE_SETTINGS":
            _log(f"SET_VOICE_SETTINGS sent — response: {msg}")

        if cmd == "DISPATCH" and evt == "VOICE_SETTINGS_UPDATE":
            with _lock:
                if "mute" in data:
                    _state.muted = bool(data["mute"])
                if "deaf" in data:
                    _state.deafened = bool(data["deaf"])
                _state.last_update = time.monotonic()

        elif cmd == "DISPATCH" and evt == "VOICE_CHANNEL_SELECT":
            channel_id = data.get("channel_id")
            if not channel_id:
                with _lock:
                    _state.in_voice = False
                    _state.channel_name = ""
                    _state.guild_name = ""
                    _state.guild_id = ""
                    _state.guild_icon_hash = ""
                    _state.guild_icon_bytes = None
                    _state.users = []
                    _state.last_update = time.monotonic()
            else:
                pipe.send(_rpc_payload("GET_SELECTED_VOICE_CHANNEL"))

        elif cmd == "GET_SELECTED_VOICE_CHANNEL":
            if evt == "ERROR" or not data:
                with _lock:
                    _state.in_voice = False
                    _state.last_update = time.monotonic()
            else:
                _update_voice_channel(data, httpx_mod, pipe)

        elif cmd == "GET_GUILD":
            if evt != "ERROR" and data:
                _update_guild(data, httpx_mod)

        elif cmd == "GET_VOICE_SETTINGS":
            if data:
                with _lock:
                    _state.muted = bool(data.get("mute", False))
                    _state.deafened = bool(data.get("deaf", False))
                    _state.last_update = time.monotonic()

        now = time.monotonic()
        if now - last_poll >= CHANNEL_POLL_INTERVAL:
            last_poll = now
            s = _snap()
            if s.in_voice:
                pipe.send(_rpc_payload("GET_SELECTED_VOICE_CHANNEL"))


def _update_voice_channel(data: dict, httpx_mod, pipe: _IpcPipe | None = None) -> None:
    guild_id = str(data.get("guild_id") or "")

    voice_states = data.get("voice_states") or []
    users = []
    for vs in voice_states:
        nick = vs.get("nick") or ""
        user = vs.get("user") or {}
        name = nick or user.get("global_name") or user.get("username") or "?"
        users.append(name)

    with _lock:
        prev_guild_id = _state.guild_id
        _state.in_voice = True
        _state.channel_name = data.get("name") or ""
        _state.guild_id = guild_id
        _state.users = users
        _state.last_update = time.monotonic()

    if guild_id and guild_id != prev_guild_id and pipe is not None:
        pipe.send(_rpc_payload("GET_GUILD", {"guild_id": guild_id}))


def _update_guild(data: dict, httpx_mod) -> None:
    name = data.get("name") or ""
    icon_hash = data.get("icon_url") or ""

    with _lock:
        prev_icon = _state.guild_icon_hash
        _state.guild_name = name
        _state.guild_icon_hash = icon_hash
        _state.last_update = time.monotonic()

    if icon_hash and icon_hash != prev_icon:
        guild_id = _snap().guild_id
        _fetch_guild_icon(guild_id, icon_hash, httpx_mod)


def _fetch_guild_icon(guild_id: str, icon_hash: str, httpx_mod) -> None:
    if icon_hash.startswith("http"):
        url = icon_hash
    else:
        ext = "gif" if icon_hash.startswith("a_") else "png"
        url = f"https://cdn.discordapp.com/icons/{guild_id}/{icon_hash}.{ext}?size=256"
    try:
        resp = httpx_mod.get(url, timeout=5.0)
        if resp.status_code == 200:
            with _lock:
                _state.guild_icon_bytes = resp.content
                _state.last_update = time.monotonic()
        else:
            _log(f"guild icon fetch failed: {resp.status_code}")
    except Exception as e:
        _log(f"guild icon fetch error: {e}")


def _rpc_entry(gen: int) -> None:
    try:
        _rpc_loop(gen)
    except Exception as e:
        _log(f"RPC client crashed: {e}")


def _ensure_poller() -> None:
    global _poller_started
    with _poller_start_lock:
        if _poller_started:
            return
        _poller_started = True
        gen = _generation
        threading.Thread(
            target=_rpc_entry, args=(gen,), daemon=True, name="discord-rpc",
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


def _tint_image(img: Image.Image, color: tuple[int, int, int]) -> Image.Image:
    gray = img.convert("L")
    tinted = Image.new("RGB", img.size, color)
    tinted.putalpha(gray)
    result = Image.new("RGB", img.size, (0, 0, 0))
    result.paste(tinted, mask=gray)
    return result


# ---------------------------------------------------------------------------
# Strip — channel info, users, mute/deafen status
# ---------------------------------------------------------------------------

@register
class DiscordStrip(Behavior):
    type_id = "discord_strip"
    display_name = "Discord voice"
    targets = {TargetKind.STRIP_REGION}
    config_schema: dict = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        _ensure_poller()
        self._prev_key: tuple = ()

    def tick(self) -> bool:
        s = _snap()
        key = (s.connected, s.in_voice, s.channel_name, s.guild_name,
               tuple(s.users), s.muted, s.deafened)
        if key != self._prev_key:
            self._prev_key = key
            return True
        return False

    def render(self) -> Image.Image | None:
        w, h = self.size()
        s = _snap()
        img = Image.new("RGB", (w, h), (0, 0, 0))
        draw = ImageDraw.Draw(img)

        if not s.in_voice:
            draw.text(
                (w // 2, h // 2), "Not connected",
                fill=DIM_GREY, font=font(14), anchor="mm",
            )
            return img

        tf = font(15)
        draw.text(
            (8, 14), _truncate(draw, s.channel_name, tf, w - 16),
            fill=(255, 255, 255), font=tf, anchor="lm",
        )

        gf = font(12)
        draw.text(
            (8, 34), _truncate(draw, s.guild_name, gf, w - 16),
            fill=(160, 160, 160), font=gf, anchor="lm",
        )

        uf = font(11)
        user_text = ", ".join(s.users[:5])
        if len(s.users) > 5:
            user_text += f" +{len(s.users) - 5}"
        draw.text(
            (8, 54), _truncate(draw, user_text, uf, w - 16),
            fill=(120, 120, 120), font=uf, anchor="lm",
        )

        if s.deafened:
            status_color = DISCORD_ORANGE
            status_text = "DEAFENED"
        elif s.muted:
            status_color = DISCORD_RED
            status_text = "MUTED"
        else:
            status_color = DISCORD_GREEN
            status_text = "CONNECTED"

        sf = font(11)
        dot_y = 78
        draw.ellipse((8, dot_y - 4, 16, dot_y + 4), fill=status_color)
        draw.text(
            (20, dot_y), status_text,
            fill=status_color, font=sf, anchor="lm",
        )

        return img


# ---------------------------------------------------------------------------
# Guild icon key — server icon, press -> toggle mute
# ---------------------------------------------------------------------------

@register
class DiscordGuildIcon(Behavior):
    type_id = "discord_guild_icon"
    display_name = "Discord guild icon"
    targets = {TargetKind.KEY}
    config_schema: dict = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        _ensure_poller()
        self._prev_key: tuple = ()
        self._icon_img: Image.Image | None = None
        self._icon_bytes_id: int = 0

    def tick(self) -> bool:
        s = _snap()
        key = (s.in_voice, s.guild_id, s.guild_icon_hash, id(s.guild_icon_bytes))
        if key != self._prev_key:
            self._prev_key = key
            return True
        return False

    def render(self) -> Image.Image | None:
        w, h = self.size()
        s = _snap()

        if not s.in_voice:
            return Image.new("RGB", (w, h), (0, 0, 0))

        art = s.guild_icon_bytes
        art_id = id(art) if art else 0
        if art and art_id != self._icon_bytes_id:
            try:
                raw = Image.open(io.BytesIO(art)).convert("RGB")
                aw, ah = int(w * 0.85), int(h * 0.85)
                raw = raw.resize((aw, ah), Image.LANCZOS)
                rounded = _round_corners(raw, 16)
                canvas = Image.new("RGB", (w, h), (0, 0, 0))
                canvas.paste(rounded, ((w - aw) // 2, (h - ah) // 2))
                self._icon_img = canvas
            except Exception:
                self._icon_img = None
            self._icon_bytes_id = art_id
        elif not art:
            self._icon_img = None
            self._icon_bytes_id = 0

        if self._icon_img:
            return self._icon_img.copy()

        img = Image.new("RGB", (w, h), (30, 30, 30))
        draw = ImageDraw.Draw(img)
        initials = "".join(word[0] for word in s.guild_name.split()[:2]).upper() if s.guild_name else "?"
        draw.text(
            (w // 2, h // 2), initials,
            fill=DIM_GREY, font=font(36), anchor="mm",
        )
        return img

    def on_press(self) -> None:
        s = _snap()
        _enqueue_cmd("SET_VOICE_SETTINGS", {"mute": not s.muted})


# ---------------------------------------------------------------------------
# Logo key — Discord icon, press -> open / focus
# ---------------------------------------------------------------------------

@register
class DiscordLogo(Behavior):
    type_id = "discord_logo"
    display_name = "Discord logo"
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
        logo_path = os.path.join(_ASSETS, "discord_logo.png")
        try:
            raw = Image.open(logo_path).convert("RGBA")
            sz = int(min(w, h) * 0.67)
            raw = raw.resize((sz, sz), Image.LANCZOS)
            canvas = Image.new("RGB", (w, h), (0, 0, 0))
            canvas.paste(raw, ((w - sz) // 2, (h - sz) // 2), mask=raw.split()[3])
            self._logo = canvas
        except Exception:
            img = Image.new("RGB", (w, h), (0, 0, 0))
            ImageDraw.Draw(img).ellipse((20, 20, w - 20, h - 20), fill=DISCORD_BLURPLE)
            self._logo = img
        return self._logo.copy()

    def on_press(self) -> None:
        hwnd = _find_discord_hwnd()
        if hwnd:
            focus_window(hwnd)
        else:
            try:
                os.startfile("discord:")
            except Exception as e:
                _log(f" launch failed: {e}")


# ---------------------------------------------------------------------------
# Volume dial — per-app volume via pycaw
# ---------------------------------------------------------------------------

@register
class DiscordVolume(Behavior):
    type_id = "discord_volume"
    display_name = "Discord volume"
    targets = {TargetKind.DIAL_ROTATE}
    config_schema: dict = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        _ensure_poller()

    def on_rotate(self, delta: int) -> None:
        current = _get_discord_volume()
        _set_discord_volume(current + delta * 0.02)


# ---------------------------------------------------------------------------
# Deafen dial press
# ---------------------------------------------------------------------------

@register
class DiscordDeafen(Behavior):
    type_id = "discord_deafen"
    display_name = "Discord deafen"
    targets = {TargetKind.DIAL_PRESS}
    config_schema: dict = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict, bus: EventBus) -> None:
        super().__init__(target, config, bus)
        _ensure_poller()

    def on_press(self) -> None:
        s = _snap()
        _enqueue_cmd("SET_VOICE_SETTINGS", {"deaf": not s.deafened})
