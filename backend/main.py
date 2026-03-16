"""
F1IQ Backend — FastAPI application entry point.

Serves:
  • REST API under /api/*
  • WebSocket at /ws/timing  (pushes timing data every 2s)
  • Static files + HTML dashboard at /
"""
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Set

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .routers import live, history, standings, weekend, llm, debrief

load_dotenv(os.path.join(os.path.dirname(__file__), "../.env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger("f1iq")

# ── WebSocket connection manager ─────────────────

class ConnectionManager:
    def __init__(self):
        self.active: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)
        logger.info(f"WS connected  (total={len(self.active)})")

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)
        logger.info(f"WS disconnected (total={len(self.active)})")

    async def broadcast(self, data: dict):
        if not self.active:
            return
        payload = json.dumps(data)
        dead = set()
        for ws in self.active:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self.active.discard(ws)


manager = ConnectionManager()


# ── Background timing broadcaster ────────────────

async def _broadcast_timing():
    """Push timing to all WebSocket clients at a modest cadence."""
    from .routers.live import build_live_timing

    while True:
        try:
            if manager.active:
                data = await build_live_timing()
                data["type"] = "timing"
                data["server_time"] = datetime.utcnow().isoformat() + "Z"
                await manager.broadcast(data)
        except Exception as e:
            logger.warning(f"Broadcast error: {e}")
        await asyncio.sleep(5)


# ── Lifespan ──────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_broadcast_timing())
    logger.info("F1IQ started — WebSocket broadcaster running")
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    logger.info("F1IQ shutdown")


# ── App ───────────────────────────────────────────

app = FastAPI(
    title="F1IQ Intelligence Platform",
    description="Live F1 data: timing, strategy, predictions, debrief",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(live.router)
app.include_router(history.router)
app.include_router(standings.router)
app.include_router(weekend.router)
app.include_router(llm.router)
app.include_router(debrief.router)


# ── WebSocket endpoint ────────────────────────────

@app.websocket("/ws/timing")
async def ws_timing(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive; data is pushed by broadcaster
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# ── Static files & frontend ───────────────────────

_frontend_dir = os.path.join(os.path.dirname(__file__), "../frontend")
_static_dir = os.path.join(_frontend_dir, "static")
_template_dir = os.path.join(_frontend_dir, "templates")

if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


@app.get("/", include_in_schema=False)
async def serve_dashboard():
    index = os.path.join(_template_dir, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return {"message": "F1IQ API running — frontend not found"}


@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


# ── __init__.py placeholders ─────────────────────
# (created below)
