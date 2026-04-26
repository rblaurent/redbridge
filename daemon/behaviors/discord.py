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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DISCORD_CLIENT_ID = "1372561907680149644"
DISCORD_CLIENT_SECRET = "WCVByCOz2eR7aywq4xe1RIgnMRr0Vh9i"
DISCORD_REDIRECT_URI = "http://localhost"
DISCORD_RPC_PORTS = range(6463, 6473)
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
            return data
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
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


def _rpc_msg(cmd: str, args: dict | None = None, evt: str | None = None) -> str:
    msg: dict = {"cmd": cmd, "nonce": _nonce()}
    if args is not None:
        msg["args"] = args
    if evt is not None:
        msg["evt"] = evt
    return json.dumps(msg)


# ---------------------------------------------------------------------------
# Background RPC client
# ---------------------------------------------------------------------------

_poller_started = False
_poller_start_lock = threading.Lock()
_cmd_queue: asyncio.Queue | None = None
_loop_ref: asyncio.AbstractEventLoop | None = None
_auth_prompted = False


def _enqueue_cmd(cmd: str, args: dict) -> None:
    if _cmd_queue is not None and _loop_ref is not None:
        _loop_ref.call_soon_threadsafe(_cmd_queue.put_nowait, (cmd, args))


async def _rpc_loop() -> None:
    import httpx
    import websockets

    global _cmd_queue, _loop_ref
    _loop_ref = asyncio.get_event_loop()
    _cmd_queue = asyncio.Queue()

    while True:
        delay = RECONNECT_DELAY
        try:
            delay = await _connect_and_run(httpx, websockets)
        except Exception as e:
            print(f"[discord] RPC error: {e}", flush=True)

        with _lock:
            _state.connected = False
            _state.in_voice = False
        await asyncio.sleep(delay)


async def _connect_and_run(httpx, websockets) -> float:
    ws = None
    for port in DISCORD_RPC_PORTS:
        uri = f"ws://127.0.0.1:{port}/?v=1&encoding=json"
        try:
            ws = await asyncio.wait_for(
                websockets.connect(uri, origin="https://streamkit.discord.com"),
                timeout=2.0,
            )
            print(f"[discord] connected on port {port}", flush=True)
            break
        except Exception:
            continue

    if ws is None:
        return RECONNECT_DELAY

    try:
        return await _run_session(ws, httpx)
    finally:
        await ws.close()


async def _run_session(ws, httpx) -> float:
    ready = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
    if ready.get("evt") != "READY":
        print(f"[discord] unexpected handshake: {ready}", flush=True)
        return RECONNECT_DELAY

    access_token = await _authenticate(ws, httpx)
    if not access_token:
        return AUTH_FAILURE_DELAY

    for evt in ("VOICE_SETTINGS_UPDATE", "VOICE_CHANNEL_SELECT"):
        await ws.send(_rpc_msg("SUBSCRIBE", {}, evt=evt))

    await ws.send(_rpc_msg("GET_SELECTED_VOICE_CHANNEL"))
    await ws.send(_rpc_msg("GET_VOICE_SETTINGS"))

    with _lock:
        _state.connected = True
        _state.last_update = time.monotonic()

    print("[discord] RPC connected and subscribed", flush=True)

    recv_task = asyncio.create_task(_recv_loop(ws, httpx))
    cmd_task = asyncio.create_task(_cmd_drain(ws))
    poll_task = asyncio.create_task(_channel_poll(ws))

    done, pending = await asyncio.wait(
        [recv_task, cmd_task, poll_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()
    for t in done:
        if t.exception():
            raise t.exception()
    return RECONNECT_DELAY


async def _authenticate(ws, httpx) -> str | None:
    global _auth_prompted

    token_data = _load_token()
    if token_data:
        print("[discord] trying cached token...", flush=True)
        await ws.send(_rpc_msg("AUTHENTICATE", {"access_token": token_data["access_token"]}))
        resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
        if resp.get("evt") != "ERROR":
            print("[discord] authenticated with cached token", flush=True)
            return token_data["access_token"]
        err = resp.get("data", {})
        print(f"[discord] cached token rejected: {err.get('code')} {err.get('message')}", flush=True)
        _delete_token()

    if _auth_prompted:
        print("[discord] already prompted once this session, skipping", flush=True)
        return None

    if not DISCORD_CLIENT_SECRET:
        print("[discord] DISCORD_CLIENT_SECRET not set", flush=True)
        return None

    _auth_prompted = True
    print("[discord] starting OAuth authorize flow (check Discord for prompt)...", flush=True)
    await ws.send(_rpc_msg("AUTHORIZE", {
        "client_id": DISCORD_CLIENT_ID,
        "scopes": ["rpc", "rpc.voice.read", "rpc.voice.write"],
    }))
    auth_resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=120.0))
    if auth_resp.get("evt") == "ERROR":
        err = auth_resp.get("data", {})
        print(f"[discord] authorize failed: {err.get('code')} {err.get('message')}", flush=True)
        return None
    code = auth_resp.get("data", {}).get("code")
    if not code:
        print(f"[discord] no auth code in response", flush=True)
        return None
    print("[discord] got auth code, exchanging for token...", flush=True)

    # Try with and without redirect_uri — Discord app config may vary
    for uri in (DISCORD_REDIRECT_URI, None):
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
        }
        if uri:
            payload["redirect_uri"] = uri
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                "https://discord.com/api/oauth2/token", data=payload,
            )
        if token_resp.status_code == 200:
            break
        print(f"[discord] token exchange (redirect_uri={uri!r}): {token_resp.status_code} {token_resp.text}", flush=True)
    else:
        print("[discord] token exchange failed — check redirect URIs in Discord dev portal", flush=True)
        return None

    token_data = token_resp.json()
    access_token = token_data["access_token"]
    _save_token({
        "access_token": access_token,
        "refresh_token": token_data.get("refresh_token", ""),
        "expires_at": time.time() + token_data.get("expires_in", 604800),
    })
    print(f"[discord] token saved to {_TOKEN_PATH}", flush=True)

    await ws.send(_rpc_msg("AUTHENTICATE", {"access_token": access_token}))
    resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
    if resp.get("evt") == "ERROR":
        err = resp.get("data", {})
        print(f"[discord] authenticate failed: {err.get('code')} {err.get('message')}", flush=True)
        _delete_token()
        return None

    print("[discord] authenticated via OAuth flow", flush=True)
    return access_token


async def _recv_loop(ws, httpx_mod) -> None:
    while True:
        raw = await ws.recv()
        msg = json.loads(raw)
        cmd = msg.get("cmd")
        evt = msg.get("evt")
        data = msg.get("data") or {}

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
                await ws.send(_rpc_msg("GET_SELECTED_VOICE_CHANNEL"))

        elif cmd == "GET_SELECTED_VOICE_CHANNEL":
            if evt == "ERROR" or not data:
                with _lock:
                    _state.in_voice = False
                    _state.last_update = time.monotonic()
            else:
                await _update_voice_channel(data, httpx_mod)

        elif cmd == "GET_VOICE_SETTINGS":
            if data:
                with _lock:
                    _state.muted = bool(data.get("mute", False))
                    _state.deafened = bool(data.get("deaf", False))
                    _state.last_update = time.monotonic()


async def _update_voice_channel(data: dict, httpx_mod) -> None:
    guild = data.get("guild") or {}
    guild_id = str(guild.get("id", ""))
    guild_icon_hash = guild.get("icon_url") or guild.get("icon") or ""

    voice_states = data.get("voice_states") or []
    users = []
    for vs in voice_states:
        nick = vs.get("nick") or ""
        user = vs.get("user") or {}
        name = nick or user.get("global_name") or user.get("username") or "?"
        users.append(name)

    prev_icon_key = ""
    with _lock:
        prev_icon_key = f"{_state.guild_id}:{_state.guild_icon_hash}"
        _state.in_voice = True
        _state.channel_name = data.get("name") or ""
        _state.guild_name = guild.get("name") or ""
        _state.guild_id = guild_id
        _state.guild_icon_hash = guild_icon_hash
        _state.users = users
        _state.last_update = time.monotonic()

    new_icon_key = f"{guild_id}:{guild_icon_hash}"
    if new_icon_key != prev_icon_key and guild_id and guild_icon_hash:
        await _fetch_guild_icon(guild_id, guild_icon_hash, httpx_mod)


async def _fetch_guild_icon(guild_id: str, icon_hash: str, httpx_mod) -> None:
    ext = "gif" if icon_hash.startswith("a_") else "png"
    url = f"https://cdn.discordapp.com/icons/{guild_id}/{icon_hash}.{ext}?size=128"
    try:
        async with httpx_mod.AsyncClient() as client:
            resp = await client.get(url, timeout=5.0)
            if resp.status_code == 200:
                with _lock:
                    _state.guild_icon_bytes = resp.content
                    _state.last_update = time.monotonic()
            else:
                print(f"[discord] guild icon fetch failed: {resp.status_code}", flush=True)
    except Exception as e:
        print(f"[discord] guild icon fetch error: {e}", flush=True)


async def _cmd_drain(ws) -> None:
    while True:
        cmd, args = await _cmd_queue.get()
        try:
            await ws.send(_rpc_msg(cmd, args))
        except Exception as e:
            print(f"[discord] send command error: {e}", flush=True)


async def _channel_poll(ws) -> None:
    while True:
        await asyncio.sleep(CHANNEL_POLL_INTERVAL)
        s = _snap()
        if s.in_voice:
            await ws.send(_rpc_msg("GET_SELECTED_VOICE_CHANNEL"))


def _rpc_entry() -> None:
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_rpc_loop())
    except Exception as e:
        print(f"[discord] RPC client crashed: {e}", flush=True)


def _ensure_poller() -> None:
    global _poller_started
    with _poller_start_lock:
        if _poller_started:
            return
        _poller_started = True
        threading.Thread(
            target=_rpc_entry, daemon=True, name="discord-rpc",
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
                print(f"[discord] launch failed: {e}", flush=True)


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
