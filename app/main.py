from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .database import init_db
from .routers import exclusions, files, folders, jobs, logs, settings, webhooks
from .services.scheduler import scan_scheduler
from .services.watcher import folder_watcher
from .ws_manager import ws_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await folder_watcher.start()
    await scan_scheduler.start()
    yield
    await folder_watcher.stop()
    await scan_scheduler.stop()


app = FastAPI(title="RARUnpacker", version="1.0.0", lifespan=lifespan)

# Routers
app.include_router(exclusions.router)
app.include_router(webhooks.router)
app.include_router(webhooks.mgmt_router)
app.include_router(files.router)
app.include_router(jobs.router)
app.include_router(folders.router)
app.include_router(settings.router)
app.include_router(logs.router)

# Static frontend
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/")
async def root():
    return FileResponse("app/static/index.html")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()   # keep-alive / ping
    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)
    except Exception:
        await ws_manager.disconnect(websocket)
