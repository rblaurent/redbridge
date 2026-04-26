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
from typing import Any, Protocol

from PIL import Image
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.Devices.StreamDeck import DialEventType, TouchscreenEventType
from StreamDeck.ImageHelpers import PILHelper

import behaviors  # noqa: F401 — register behaviors
from behaviors.base import Behavior, EventBus, Target, TargetKind
from registry import get as get_behavior


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

        self._brightness: int = 75
        self._screensaver_minutes: int = 15
        self._tick_hz: float = 4.0
        self._last_input: float = time.monotonic()
        self._asleep: bool = False

    # ---- lifecycle -----------------------------------------------------------

    def start(self) -> bool:
        try:
            decks = DeviceManager().enumerate()
        except Exception as e:
            print(f"[runtime] HID probe failed: {e}", flush=True)
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
            print(
                f"[runtime] deck attached: {self._deck.deck_type()} "
                f"serial={self._deck.get_serial_number()}",
                flush=True,
            )
        else:
            print("[runtime] no deck attached — running mirror-only", flush=True)

        self._tick_thread = threading.Thread(target=self._tick_loop, daemon=True)
        self._tick_thread.start()
        return self._deck is not None

    def stop(self) -> None:
        self._stop.set()
        if self._deck is not None:
            try:
                self._deck.reset()
                self._deck.close()
            except Exception:
                pass
        if self._tick_thread is not None:
            self._tick_thread.join(timeout=1.0)

    # ---- settings ------------------------------------------------------------

    def apply_settings(self, settings: Any) -> None:
        with self._lock:
            self._brightness = settings.brightness
            self._screensaver_minutes = settings.screensaver_minutes
            self._tick_hz = max(1.0, min(30.0, float(settings.tick_hz)))
        if self._deck is not None:
            try:
                self._deck.set_brightness(self._brightness)
            except Exception as e:
                print(f"[runtime] set_brightness failed: {e}", flush=True)
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
            self._last_png.clear()
        self._render_all()

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"type": "render", "target": tid, "png_b64": b64}
                for tid, b64 in self._last_png.items()
            ]

    # ---- render --------------------------------------------------------------

    def _render_all(self) -> None:
        with self._lock:
            keys = list(self._keys.items())
            strip = list(self._strip.items())
        for idx, b in keys:
            self._render_key(idx, b)
        for idx, b in strip:
            self._render_strip(idx, b)

    def _render_key(self, idx: int, b: Behavior) -> None:
        img = self._safe_render(b)
        if img is None:
            return
        if self._deck is not None:
            try:
                native = PILHelper.to_native_key_format(self._deck, img)
                self._deck.set_key_image(idx, native)
            except Exception as e:
                print(f"[runtime] set_key_image({idx}) failed: {e}", flush=True)
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
                print(f"[runtime] set_touchscreen_image({idx}) failed: {e}", flush=True)
        self._broadcast_render(f"strip:{idx}", img)

    @staticmethod
    def _safe_render(b: Behavior) -> Image.Image | None:
        try:
            return b.render()
        except Exception as e:
            print(f"[runtime] render failed for {b.type_id}: {e}", flush=True)
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
            print(f"[runtime] on_press({key}) failed: {e}", flush=True)
        self._render_key(key, b)

    def _on_dial(self, _deck: Any, dial: int, event: DialEventType, value: Any) -> None:
        self._wake()
        if event == DialEventType.PUSH:
            self._broadcast_input(f"dial:{dial}:press", "press" if value else "release")
            if not value:
                return
            with self._lock:
                b = self._dial_press.get(dial)
            if b is None:
                return
            try:
                b.on_press()
            except Exception as e:
                print(f"[runtime] dial.on_press({dial}) failed: {e}", flush=True)
        elif event == DialEventType.TURN:
            delta = int(value)
            self._broadcast_input(f"dial:{dial}:rotate", "rotate", {"delta": delta})
            with self._lock:
                b = self._dial_rotate.get(dial)
            if b is None:
                return
            try:
                b.on_rotate(delta)
            except Exception as e:
                print(f"[runtime] dial.on_rotate({dial}) failed: {e}", flush=True)

    def _on_touch(self, _deck: Any, event: TouchscreenEventType, value: dict) -> None:
        self._wake()
        x = int(value.get("x", 0)) if isinstance(value, dict) else 0
        idx = max(0, min(3, x // 200))
        self._broadcast_input(f"strip:{idx}", event.name.lower(), {"value": value})

    # ---- tick ----------------------------------------------------------------

    def _tick_loop(self) -> None:
        while not self._stop.wait(1.0 / self._tick_hz):
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
                strip = list(self._strip.items())
            for idx, b in keys:
                try:
                    if b.tick():
                        self._render_key(idx, b)
                except Exception as e:
                    print(f"[runtime] tick key {idx}: {e}", flush=True)
            for idx, b in strip:
                try:
                    if b.tick():
                        self._render_strip(idx, b)
                except Exception as e:
                    print(f"[runtime] tick strip {idx}: {e}", flush=True)
