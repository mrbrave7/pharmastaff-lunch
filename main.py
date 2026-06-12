"""Entrypoint: runs the FastAPI Mini App server and the Telegram bot together.

uvicorn owns the event loop; the python-telegram-bot Application is started inside
the FastAPI lifespan and polls in the background on that same loop. One process,
one SQLite file, shared by the web app and the bot.

Run:  python main.py
"""
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import bot as botmod
import config
import db
import webapp

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.validate()
    await db.init()

    application = botmod.build_application()
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    app.state.application = application
    logger.info(
        "Bot polling + Mini App server started (WEBAPP_URL=%s)",
        config.WEBAPP_URL or "<not set — inline buttons only>",
    )

    try:
        yield
    finally:
        logger.info("Shutting down…")
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        await db.close()


app = FastAPI(lifespan=lifespan)
app.include_router(webapp.router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
# Menu photos live in MENU_IMAGE_DIR (project folder locally, or the persistent
# volume in production). Served here so the URL is stable regardless of location.
app.mount("/uploads", StaticFiles(directory=config.MENU_IMAGE_DIR), name="uploads")


@app.get("/")
async def index():
    return FileResponse(
        os.path.join(STATIC_DIR, "index.html"),
        headers={"Cache-Control": "no-store"},
    )


if __name__ == "__main__":
    # Fail fast with a friendly message before uvicorn spins up.
    config.validate()
    uvicorn.run(app, host=config.HOST, port=config.PORT)
