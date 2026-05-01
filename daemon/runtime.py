"""Render + input pipeline for the Stream Deck Plus.

Owns the physical device, builds behavior instances from a layout, renders them
to the hardware and to a WS hub (for the web-UI live mirror), and dispatches
physical input back into behaviors.
"""

from __future__ import annotations

import asyncio
import base64
import io
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from PIL import Image
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.Devices.StreamDeck import DialEventType, TouchscreenEventType
from StreamDeck.ImageHelpers import PILHelper

import behaviors  # noqa: F401 — register behaviors
from behaviors.base import Behavior, EventBus, Target, TargetKind
from log import setup as setup_log
from registry import get as get_behavior, reload_behaviors

_log = setup_log().getChild("runtime")

_DAEMON_DIR = Path(__file__).resolve().parent
_BEHAVIORS_DIR = _DAEMON_DIR / "behaviors"
_WATCHED_FILES = [_DAEMON_DIR / "gfx.py"]


class _Hub(Protocol):
    async def broadcast(self, msg: dict[str, Any]) -> None: ...


class DeckRuntime:
    def __init__(self, hub: _Hub, loop: asyncio.AbstractEventLoop) -> None:
        self._hub = hub
        self._loop = loop
        self._bus = EventBus()
        self._deck: Any | None = None
        self._keys: dict[int, Behavior] = {}
        self._dial_rotate: dict[int, Behavior] = {}
        self._dial_press: dict[int, Behavior] = {}
        self._strip: dict[int, Behavior] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._tick_thread: threading.Thread | None = None
        self._last_png: dict[str, str] = {}
        self._overlay_strip: dict[int, Behavior] = {}
        self._overlay_dial_rotate: dict[int, Behavior] = {}
        self._overlay_dial_press: dict[int, Behavior] = {}

        self._brightness: int = 75
        self._screensaver_minutes: int = 15
        self._tick_hz: float = 4.0
        self._last_input: float = time.monotonic()
        self._asleep: bool = False
        self._boost_hz: float = 0.0
        self._boost_until: float = 0.0

        self._dirty_keys: set[int] = set()
        self._dirty_strips: set[int] = set()
        self._tick_wake = threading.Event()

        self._layout_loader: Callable[[], Any] | None = None
        self._watch_thread: threading.Thread | None = None

        self._bus.subscribe("overlay:set", self._on_overlay_set)
        self._bus.subscribe("overlay:clear", self._on_overlay_clear)
        self._bus.subscribe("tick:boost", self._on_tick_boost)
        self._bus.subscribe("column:swap", self._on_column_swap)

    # ---- lifecycle -----------------------------------------------------------

    def set_layout_loader(self, loader: Callable[[], Any]) -> None:
        self._layout_loader = loader

    def start(self) -> bool:
        try:
            decks = DeviceManager().enumerate()
        except Exception as e:
            _log.error("HID probe failed: %s", e)
            decks = []
        if decks:
            self._deck = decks[0]
            self._deck.open()
            self._deck.reset()
            self._deck.set_key_callback(self._on_key)
            if hasattr(self._deck, "set_dial_callback"):
                self._deck.set_dial_callback(self._on_dial)
            if hasattr(self._deck, "set_touchscreen_callback"):
                self._deck.set_touchscreen_callback(self._on_touch)
            _log.info(
                "deck attached: %s serial=%s",
                self._deck.deck_type(), self._deck.get_serial_number(),
            )
        else:
            _log.info("no deck attached — running mirror-only")

        self._tick_thread = threading.Thread(target=self._tick_loop, daemon=True)
        self._tick_thread.start()
        self._watch_thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._watch_thread.start()
        return self._deck is not None

    def stop(self) -> None:
        self._stop.set()
        self._tick_wake.set()
        if self._deck is not None:
            try:
                self._deck.reset()
                self._deck.close()
            except Exception:
                pass
        if self._tick_thread is not None:
            self._tick_thread.join(timeout=1.0)
        if self._watch_thread is not None:
            self._watch_thread.join(timeout=1.0)

    # ---- settings ------------------------------------------------------------

    def apply_settings(self, settings: Any) -> None:
        with self._lock:
            self._brightness = settings.brightness
            self._screensaver_minutes = settings.screensaver_minutes
            self._tick_hz = max(1.0, min(60.0, float(settings.tick_hz)))
        if self._deck is not None:
            try:
                self._deck.set_brightness(self._brightness)
            except Exception as e:
                _log.error("set_brightness failed: %s", e)
        self._asleep = False
        self._last_input = time.monotonic()

    def _wake(self) -> None:
        self._last_input = time.monotonic()
        if self._asleep:
            self._asleep = False
            if self._deck is not None:
                try:
                    self._deck.set_brightness(self._brightness)
                except Exception:
                    pass

    # ---- layout --------------------------------------------------------------

    def apply_layout(self, layout: Any) -> None:
        with self._lock:
            self._keys.clear()
            self._dial_rotate.clear()
            self._dial_press.clear()
            self._strip.clear()
            self._overlay_strip.clear()
            self._overlay_dial_rotate.clear()
            self._overlay_dial_press.clear()
            for k_str, a in layout.keys.items():
                idx = int(k_str)
                cls = get_behavior(a.behavior) or get_behavior("empty")
                self._keys[idx] = cls(Target(TargetKind.KEY, idx), dict(a.config), self._bus)
            for d_str, d in layout.dials.items():
                idx = int(d_str)
                rot_cls = get_behavior(d.rotate.behavior) or get_behavior("empty")
                prs_cls = get_behavior(d.press.behavior) or get_behavior("empty")
                self._dial_rotate[idx] = rot_cls(
                    Target(TargetKind.DIAL_ROTATE, idx), dict(d.rotate.config), self._bus
                )
                self._dial_press[idx] = prs_cls(
                    Target(TargetKind.DIAL_PRESS, idx), dict(d.press.config), self._bus
                )
            for s_str, a in layout.strip.items():
                idx = int(s_str)
                cls = get_behavior(a.behavior) or get_behavior("empty")
                self._strip[idx] = cls(Target(TargetKind.STRIP_REGION, idx), dict(a.config), self._bus)
            self._dirty_keys.update(self._keys.keys())
            self._dirty_strips.update(self._strip.keys())
            self._last_png.clear()
        self._tick_wake.set()

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"type": "render", "target": tid, "png_b64": b64}
                for tid, b64 in self._last_png.items()
            ]

    # ---- render --------------------------------------------------------------

    def _render_key(self, idx: int, b: Behavior) -> None:
        img = self._safe_render(b)
        if img is None:
            return
        if self._deck is not None:
            try:
                native = PILHelper.to_native_key_format(self._deck, img)
                self._deck.set_key_image(idx, native)
            except Exception as e:
                _log.error("set_key_image(%d) failed: %s", idx, e)
        self._broadcast_render(f"key:{idx}", img)

    def _render_strip(self, idx: int, b: Behavior) -> None:
        img = self._safe_render(b)
        if img is None:
            return
        if self._deck is not None:
            try:
                native = PILHelper.to_native_touchscreen_format(self._deck, img)
                self._deck.set_touchscreen_image(
                    native, x_pos=idx * 200, y_pos=0, width=200, height=100
                )
            except Exception as e:
                _log.error("set_touchscreen_image(%d) failed: %s", idx, e)
        self._broadcast_render(f"strip:{idx}", img)

    @staticmethod
    def _safe_render(b: Behavior) -> Image.Image | None:
        try:
            return b.render()
        except Exception as e:
            _log.error("render failed for %s: %s", b.type_id, e)
            return None

    def _broadcast_render(self, target_id: str, img: Image.Image) -> None:
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=False)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        with self._lock:
            self._last_png[target_id] = b64
        self._submit({"type": "render", "target": target_id, "png_b64": b64})

    def _broadcast_input(self, target_id: str, event: str, extra: dict | None = None) -> None:
        msg: dict[str, Any] = {"type": "input", "target": target_id, "event": event}
        if extra:
            msg.update(extra)
        self._submit(msg)

    def _submit(self, msg: dict[str, Any]) -> None:
        try:
            asyncio.run_coroutine_threadsafe(self._hub.broadcast(msg), self._loop)
        except RuntimeError:
            pass

    # ---- overlay -------------------------------------------------------------

    def _on_overlay_set(self, payload: dict[str, Any]) -> None:
        strips = payload.get("strip", {})
        with self._lock:
            self._overlay_strip.update(strips)
            self._overlay_dial_rotate.update(payload.get("dial_rotate", {}))
            self._overlay_dial_press.update(payload.get("dial_press", {}))
            self._dirty_strips.update(strips.keys())
        self._tick_wake.set()

    def _on_overlay_clear(self, payload: dict[str, Any]) -> None:
        with self._lock:
            for idx in payload.get("strip", []):
                self._overlay_strip.pop(idx, None)
                if idx in self._strip:
                    self._dirty_strips.add(idx)
            for idx in payload.get("dial_rotate", []):
                self._overlay_dial_rotate.pop(idx, None)
            for idx in payload.get("dial_press", []):
                self._overlay_dial_press.pop(idx, None)
        self._tick_wake.set()

    def _on_tick_boost(self, payload: dict[str, Any]) -> None:
        self._boost_hz = float(payload.get("hz", 60.0))
        self._boost_until = float(payload.get("until", 0.0))

    def _on_column_swap(self, payload: dict[str, Any]) -> None:
        profile = payload.get("profile", {})
        slots = [
            ("key_1",         TargetKind.KEY,          self._keys,        1),
            ("key_5",         TargetKind.KEY,          self._keys,        5),
            ("dial_rotate_1", TargetKind.DIAL_ROTATE,  self._dial_rotate, 1),
            ("dial_press_1",  TargetKind.DIAL_PRESS,   self._dial_press,  1),
            ("strip_1",       TargetKind.STRIP_REGION, self._strip,       1),
        ]
        with self._lock:
            for field, kind, store, idx in slots:
                a = profile.get(field)
                if a:
                    cls = get_behavior(a["behavior"]) or get_behavior("empty")
                    store[idx] = cls(Target(kind, idx), a.get("config", {}), self._bus)
            self._dirty_keys.update({1, 5})
            self._dirty_strips.add(1)
        self._tick_wake.set()

    # ---- input ---------------------------------------------------------------

    def _on_key(self, _deck: Any, key: int, pressed: bool) -> None:
        self._wake()
        self._broadcast_input(f"key:{key}", "press" if pressed else "release")
        if not pressed:
            return
        with self._lock:
            b = self._keys.get(key)
        if b is None:
            return
        try:
            b.on_press()
        except Exception as e:
            _log.error("on_press(%d) failed: %s", key, e)
        with self._lock:
            self._dirty_keys.add(key)
        self._tick_wake.set()

    def _on_dial(self, _deck: Any, dial: int, event: DialEventType, value: Any) -> None:
        self._wake()
        if event == DialEventType.PUSH:
            self._broadcast_input(f"dial:{dial}:press", "press" if value else "release")
            if not value:
                return
            with self._lock:
                b = self._overlay_dial_press.get(dial) or self._dial_press.get(dial)
            if b is None:
                return
            try:
                b.on_press()
            except Exception as e:
                _log.error("dial.on_press(%d) failed: %s", dial, e)
        elif event == DialEventType.TURN:
            delta = int(value)
            self._broadcast_input(f"dial:{dial}:rotate", "rotate", {"delta": delta})
            with self._lock:
                b = self._overlay_dial_rotate.get(dial) or self._dial_rotate.get(dial)
            if b is None:
                return
            try:
                b.on_rotate(delta)
            except Exception as e:
                _log.error("dial.on_rotate(%d) failed: %s", dial, e)

    def _on_touch(self, _deck: Any, event: TouchscreenEventType, value: dict) -> None:
        self._wake()
        x = int(value.get("x", 0)) if isinstance(value, dict) else 0
        idx = max(0, min(3, x // 200))
        self._broadcast_input(f"strip:{idx}", event.name.lower(), {"value": value})
        if event == TouchscreenEventType.SHORT:
            with self._lock:
                b = self._overlay_strip.get(idx) or self._strip.get(idx)
            if b is not None:
                try:
                    b.on_press()
                except Exception as e:
                    _log.error("strip.on_press(%d) failed: %s", idx, e)
            with self._lock:
                self._dirty_strips.add(idx)
            self._tick_wake.set()

    # ---- tick ----------------------------------------------------------------

    def _tick_loop(self) -> None:
        while True:
            now = time.monotonic()
            hz = max(self._tick_hz, self._boost_hz if now < self._boost_until else 0)
            self._tick_wake.wait(1.0 / hz)
            self._tick_wake.clear()
            if self._stop.is_set():
                break
            if (
                self._screensaver_minutes > 0
                and not self._asleep
                and time.monotonic() - self._last_input
                > self._screensaver_minutes * 60
            ):
                self._asleep = True
                if self._deck is not None:
                    try:
                        self._deck.set_brightness(0)
                    except Exception:
                        pass

            with self._lock:
                keys = list(self._keys.items())
                strip = [
                    (idx, self._overlay_strip.get(idx, base))
                    for idx, base in self._strip.items()
                ]
                pending_keys = self._dirty_keys.copy()
                self._dirty_keys.clear()
                pending_strips = self._dirty_strips.copy()
                self._dirty_strips.clear()
            for idx, b in keys:
                try:
                    if b.tick() or idx in pending_keys:
                        self._render_key(idx, b)
                except Exception as e:
                    _log.error("tick key %d: %s", idx, e)
            for idx, b in strip:
                try:
                    if b.tick() or idx in pending_strips:
                        self._render_strip(idx, b)
                except Exception as e:
                    _log.error("tick strip %d: %s", idx, e)

    # ---- hot reload ----------------------------------------------------------

    def _watch_loop(self) -> None:
        mtimes: dict[str, float] = {}
        for p in _BEHAVIORS_DIR.glob("*.py"):
            mtimes[str(p)] = p.stat().st_mtime
        for p in _WATCHED_FILES:
            if p.exists():
                mtimes[str(p)] = p.stat().st_mtime
        while not self._stop.wait(1.0):
            changed = False
            for p in list(_BEHAVIORS_DIR.glob("*.py")) + _WATCHED_FILES:
                if not p.exists():
                    continue
                key = str(p)
                mtime = p.stat().st_mtime
                prev = mtimes.get(key)
                if prev is None or mtime != prev:
                    mtimes[key] = mtime
                    if prev is not None:
                        changed = True
            if changed:
                _log.info("behavior file change detected, reloading…")
                try:
                    reload_behaviors()
                except Exception as e:
                    _log.error("reload failed: %s", e)
                    continue
                if self._layout_loader is not None:
                    try:
                        self.apply_layout(self._layout_loader())
                    except Exception as e:
                        _log.error("re-apply layout failed: %s", e)
                _log.info("reload complete")
