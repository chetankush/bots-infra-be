from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text

from app.api import admin, chat, whatsapp
from app.db.session import engine
from app.logging import configure, get_logger
from app.settings import get_settings

configure()
log = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.rag.embed import warmup
    from app.rag.rerank import warmup as warmup_reranker

    try:
        warmup()  # load the ONNX model before the first request pays for it
        warmup_reranker()
        log.info("embedder_ready", model=get_settings().embedding_model)
    except Exception as exc:
        log.warning("embedder_warmup_failed", error=str(exc))
    yield
    await engine.dispose()


app = FastAPI(title="FirstVoid Engine", version="0.1.0", lifespan=lifespan)

# The widget is embedded on client sites; per-tenant origin checks happen in
# resolve_tenant(). CORS here stays permissive on purpose - the allowlist is the real gate.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(admin.router)
app.include_router(whatsapp.router)


@app.get("/healthz")
async def healthz():
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"ok": True, "db": "up"}
    except Exception as exc:
        return JSONResponse({"ok": False, "db": str(exc)}, status_code=503)


@app.get("/", include_in_schema=False)
@app.get("/console", include_in_schema=False)
async def console():
    """Zero-build ops console: onboard, crawl, chat, inspect traces, leads, usage."""
    return FileResponse("app/static/console.html", media_type="text/html")


@app.get("/widget.js")
async def widget():
    """Dev convenience. In production this is served from Cloudflare, not this box."""
    return FileResponse("widget/dist/widget.js", media_type="application/javascript")


@app.get("/demo")
async def demo():
    return FileResponse("widget/demo.html", media_type="text/html")
