"""Stream Deck Plus HID abstraction.

Step-2 scope: enumerate the device, clear all keys + touchscreen to black, and
print every physical input. Later steps will wrap this into a class that owns
a render loop and dispatches to behavior instances.
"""

from __future__ import annotations

import signal
import sys
import threading
from typing import Any

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

from PIL import Image
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.Devices.StreamDeck import DialEventType, TouchscreenEventType
from StreamDeck.ImageHelpers import PILHelper


def _clear_keys(deck: Any) -> None:
    if deck.key_count() == 0:
        return
    w, h = deck.key_image_format()["size"]
    black = PILHelper.to_native_key_format(deck, Image.new("RGB", (w, h), (0, 0, 0)))
    for i in range(deck.key_count()):
        deck.set_key_image(i, black)


def _clear_touchscreen(deck: Any) -> None:
    if not hasattr(deck, "set_touchscreen_image"):
        return
    w, h = deck.touchscreen_image_format()["size"]
    black = PILHelper.to_native_touchscreen_format(
        deck, Image.new("RGB", (w, h), (0, 0, 0))
    )
    deck.set_touchscreen_image(black, x_pos=0, y_pos=0, width=w, height=h)


def _on_key(deck: Any, key: int, pressed: bool) -> None:
    print(f"KEY   key={key} pressed={pressed}", flush=True)


def _on_dial(deck: Any, dial: int, event: DialEventType, value: Any) -> None:
    if event == DialEventType.PUSH:
        print(f"DIAL  dial={dial} push={value}", flush=True)
    elif event == DialEventType.TURN:
        print(f"DIAL  dial={dial} turn={value:+d}", flush=True)
    else:
        print(f"DIAL  dial={dial} event={event} value={value}", flush=True)


def _on_touch(deck: Any, event: TouchscreenEventType, value: dict) -> None:
    print(f"TOUCH event={event.name} value={value}", flush=True)


def main() -> int:
    decks = DeviceManager().enumerate()
    if not decks:
        print(
            "No Stream Deck devices found. Check USB cable and that no other "
            "software (e.g. Elgato) is holding the device."
        )
        return 1

    opened: list[Any] = []
    for deck in decks:
        try:
            deck.open()
        except Exception as e:
            print(f"Failed to open {deck.deck_type()}: {e}")
            continue
        deck.reset()
        dial_count = getattr(deck, "dial_count", lambda: 0)()
        has_touch = hasattr(deck, "set_touchscreen_callback")
        print(
            f"Opened: {deck.deck_type()}  serial={deck.get_serial_number()}  "
            f"keys={deck.key_count()}  dials={dial_count}  "
            f"touch={'yes' if has_touch else 'no'}"
        )
        _clear_keys(deck)
        _clear_touchscreen(deck)
        deck.set_key_callback(_on_key)
        if hasattr(deck, "set_dial_callback"):
            deck.set_dial_callback(_on_dial)
        if has_touch:
            deck.set_touchscreen_callback(_on_touch)
        opened.append(deck)

    if not opened:
        return 1

    stop = threading.Event()

    def _shutdown(*_: Any) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _shutdown)
    try:
        signal.signal(signal.SIGTERM, _shutdown)
    except (ValueError, AttributeError):
        pass

    print("Listening. Press keys, turn/push dials, touch the strip. Ctrl+C to exit.")
    stop.wait()

    for deck in opened:
        try:
            deck.reset()
            deck.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
