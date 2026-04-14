"""FastAPI app: config persistence, behavior catalog, live state, WS, static UI."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import behaviors  # noqa: F401 — side-effect: populates registry
from registry import all_behaviors
from runtime import DeckRuntime


DAEMON_DIR = Path(__file__).resolve().parent
CONFIG_PATH = DAEMON_DIR / "config.json"
WEBUI_DIST = (DAEMON_DIR.parent / "webui" / "dist").resolve()

NUM_KEYS = 8
NUM_DIALS = 4
NUM_STRIP_REGIONS = 4


class BehaviorAssignment(BaseModel):
    behavior: str = "empty"
    config: dict[str, Any] = Field(default_factory=dict)


class DialAssignment(BaseModel):
    rotate: BehaviorAssignment = Field(default_factory=BehaviorAssignment)
    press: BehaviorAssignment = Field(default_factory=BehaviorAssignment)


class Layout(BaseModel):
    keys: dict[str, BehaviorAssignment] = Field(default_factory=dict)
    dials: dict[str, DialAssignment] = Field(default_factory=dict)
    strip: dict[str, BehaviorAssignment] = Field(default_factory=dict)


class BehaviorInfo(BaseModel):
    type_id: str
    display_name: str
    targets: list[str]
    config_schema: dict[str, Any]


class StateSnapshot(BaseModel):
    layout: Layout
    rendered: dict[str, str] = Field(default_factory=dict)


def default_layout() -> Layout:
    return Layout(
        keys={str(i): BehaviorAssignment() for i in range(NUM_KEYS)},
        dials={str(i): DialAssignment() for i in range(NUM_DIALS)},
        strip={str(i): BehaviorAssignment() for i in range(NUM_STRIP_REGIONS)},
    )


def _fill_defaults(layout: Layout) -> Layout:
    for i in range(NUM_KEYS):
        layout.keys.setdefault(str(i), BehaviorAssignment())
    for i in range(NUM_DIALS):
        layout.dials.setdefault(str(i), DialAssignment())
    for i in range(NUM_STRIP_REGIONS):
        layout.strip.setdefault(str(i), BehaviorAssignment())
    return layout


def load_layout() -> Layout:
    if not CONFIG_PATH.is_file():
        layout = default_layout()
        save_layout(layout)
        return layout
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return _fill_defaults(Layout.model_validate(data))
    except (json.JSONDecodeError, ValueError):
        CONFIG_PATH.replace(CONFIG_PATH.with_suffix(".json.bad"))
        layout = default_layout()
        save_layout(layout)
        return layout


def save_layout(layout: Layout) -> None:
    _fill_defaults(layout)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(layout.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(CONFIG_PATH)


def _validate_layout_behaviors(layout: Layout) -> list[str]:
    known = set(all_behaviors())
    errs: list[str] = []
    for k, a in layout.keys.items():
        if a.behavior not in known:
            errs.append(f"keys.{k}.behavior: unknown '{a.behavior}'")
    for d, dial in layout.dials.items():
        if dial.rotate.behavior not in known:
            errs.append(f"dials.{d}.rotate.behavior: unknown '{dial.rotate.behavior}'")
        if dial.press.behavior not in known:
            errs.append(f"dials.{d}.press.behavior: unknown '{dial.press.behavior}'")
    for s, a in layout.strip.items():
        if a.behavior not in known:
            errs.append(f"strip.{s}.behavior: unknown '{a.behavior}'")
    return errs


_runtime: DeckRuntime | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _runtime
    loop = asyncio.get_running_loop()
    _runtime = DeckRuntime(hub, loop)
    _runtime.start()
    _runtime.apply_layout(load_layout())
    try:
        yield
    finally:
        _runtime.stop()
        _runtime = None


app = FastAPI(title="redbridge-daemon", lifespan=lifespan)


@app.get("/api/ping")
def ping() -> dict[str, str]:
    return {"ok": "pong"}


@app.get("/api/behaviors", response_model=list[BehaviorInfo])
def list_behaviors() -> list[BehaviorInfo]:
    out = [
        BehaviorInfo(
            type_id=tid,
            display_name=cls.display_name,
            targets=sorted(t.value for t in cls.targets),
            config_schema=cls.config_schema,
        )
        for tid, cls in all_behaviors().items()
    ]
    out.sort(key=lambda b: b.display_name)
    return out


@app.get("/api/layout", response_model=Layout)
def get_layout() -> Layout:
    return load_layout()


@app.put("/api/layout", response_model=Layout)
def put_layout(layout: Layout) -> Layout:
    errors = _validate_layout_behaviors(layout)
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})
    save_layout(layout)
    if _runtime is not None:
        _runtime.apply_layout(layout)
    return layout


@app.get("/api/state", response_model=StateSnapshot)
def get_state() -> StateSnapshot:
    rendered: dict[str, str] = {}
    if _runtime is not None:
        for msg in _runtime.snapshot():
            rendered[msg["target"]] = msg["png_b64"]
    return StateSnapshot(layout=load_layout(), rendered=rendered)


@app.post("/hook/event")
async def hook_event(payload: dict[str, Any]) -> dict[str, str]:
    # Full dispatch lands in step 8. For now, just acknowledge.
    return {"ok": "accepted"}


class WSHub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def broadcast(self, msg: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for c in self._clients:
            try:
                await c.send_json(msg)
            except Exception:
                dead.append(c)
        for c in dead:
            self.disconnect(c)


hub = WSHub()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await hub.connect(ws)
    try:
        if _runtime is not None:
            for msg in _runtime.snapshot():
                await ws.send_json(msg)
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(ws)
    except Exception:
        hub.disconnect(ws)


if WEBUI_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(WEBUI_DIST), html=True), name="webui")
else:
    @app.get("/")
    def _root() -> dict[str, str]:
        return {
            "ok": "daemon running",
            "webui": f"build webui — expected at {WEBUI_DIST}",
        }
