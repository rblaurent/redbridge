# redbridge

Custom Stream Deck Plus configurator + daemon. Replaces the Elgato app with a
Python daemon that drives the device over HID, plus a local web UI for
configuration and live mirroring.

## Status

Steps 1–8 complete: scaffold, device I/O, behavior ABC + registry, FastAPI
config persistence, visual deck mock, WebSocket live mirror, config panel +
schema form, and the flagship `claude_code_idle` behavior + hook bridge.

## Prereqs

- Windows 11, Python 3.12, Node 20+
- Elgato software NOT running (don't install it)
- [`uv`](https://docs.astral.sh/uv/) for Python deps
- [`just`](https://github.com/casey/just) (optional, for recipes)
- `daemon/hidapi.dll` — bundled x64 build from
  https://github.com/libusb/hidapi/releases/tag/hidapi-0.14.0

## Ports

- Daemon (FastAPI + WS): `127.0.0.1:47337`
- Web UI (Vite dev): `127.0.0.1:5373` (proxies `/api`, `/hook`, `/ws` → daemon)

## Running

```bash
# daemon
cd daemon
uv sync
uv run uvicorn main:app --host 127.0.0.1 --port 47337

# web UI (in another terminal)
cd webui
npm install
npm run dev
```

Or via `just`:

```bash
just dev-daemon   # daemon with --reload
just dev-webui    # vite dev
just deck-test    # step-2 hardware smoke test
```

Open http://127.0.0.1:5373/ — clicking a key/dial/strip-region selects it;
physical input on the deck triggers yellow flashes in the mirror.
