# backend/main.py
import warnings
warnings.filterwarnings("ignore")

from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path
from pydantic import BaseModel

from sqlmodel import Session as DBSession
from backend.storage.database import init_db, engine
from backend.storage.models import Session as SessionModel
from backend.api.sessions import router as sessions_router, segment_router
from backend.api.export import router as export_router
from backend.api.admin import router as admin_router
from backend.api.recordings import router as recordings_router
from backend.api.materials import router as materials_router
from backend.ws.handler import handle_ws
from backend.auth import (
    COOKIE_NAME, auth_configured, authenticate_credentials, create_login,
    get_authenticated_user, public_user, revoke_login, set_auth_cookie,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[Startup] Initializing database...")
    init_db()
    from backend.asr.model import get_model
    get_model()
    # Load the voiceprint model now rather than on the first classroom connection.
    from backend.speaker.embedding import get_embedder
    get_embedder()
    print("[Startup] Ready")
    yield
    print("[Shutdown] Server stopping")


app = FastAPI(title="korAsr", lifespan=lifespan)

app.include_router(sessions_router)
app.include_router(segment_router)
app.include_router(export_router)
app.include_router(admin_router)
app.include_router(recordings_router)
app.include_router(materials_router)


class LoginBody(BaseModel):
    username: str
    password: str


@app.middleware("http")
async def protect_api(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in {
        "/api/auth/status", "/api/auth/login", "/api/auth/logout",
    }:
        with DBSession(engine) as db:
            if not auth_configured(db):
                return JSONResponse({"detail": "Create the first administrator with python -m backend.admin create-user --username admin --admin"}, status_code=503)
            user = get_authenticated_user(request, db)
            if user is None:
                return JSONResponse({"detail": "Authentication required"}, status_code=401)
            request.state.user = user
    return await call_next(request)


@app.get("/api/auth/status")
async def auth_status(request: Request):
    with DBSession(engine) as db:
        configured = auth_configured(db)
        user = get_authenticated_user(request, db) if configured else None
        return {"configured": configured, "authenticated": user is not None,
                "user": public_user(user) if user else None}


@app.post("/api/auth/login")
async def auth_login(body: LoginBody, request: Request):
    with DBSession(engine) as db:
        if not auth_configured(db):
            raise HTTPException(503, "Create the first administrator before login")
        user = authenticate_credentials(
            db, body.username, body.password,
            request.client.host if request.client else "unknown",
        )
        token = create_login(db, user)
        user_data = public_user(user)
    response = JSONResponse({"authenticated": True, "user": user_data})
    set_auth_cookie(response, request, token)
    return response


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    with DBSession(engine) as db:
        revoke_login(db, request.cookies.get(COOKIE_NAME, ""))
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    with DBSession(engine) as db:
        user = get_authenticated_user(websocket, db)
        sess = db.get(SessionModel, session_id)
        if user is None or sess is None or sess.user_id != user.id:
            await websocket.close(code=1008, reason="Authentication or session ownership required")
            return
        websocket.state.user = user
    await handle_ws(websocket, session_id)


FRONTEND_DIST = Path("frontend/dist")
if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        return FileResponse(FRONTEND_DIST / "index.html")
else:
    @app.get("/")
    async def root():
        return {"status": "ok", "message": "Backend running. Start frontend: cd frontend && npm run dev"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=False)
