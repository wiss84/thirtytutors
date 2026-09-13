"""
FastAPI app entrypoint: static/template setup, middleware, and router
wiring. Route logic itself lives in routes_pages.py (page shells),
routes_api.py (reference-data + profile/conversation REST), and
live_session.py (the Gemini Live API relay + /ws/session websocket) -
see those modules for behavior notes.

Run (after `pip install thirtytutors`):
    thirtytutors
Then open:
    http://127.0.0.1:8000/

For local development against a live source checkout, use `pip install -e .`
first so the thirtytutors command runs against this tree directly.
"""

import asyncio
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import live_session, memory, quizzes, routes_api, routes_pages, speech_detection
from .constants import ASSETS_DIR
from .profiles_store import migrate_legacy_model_name, migrate_legacy_profile_state

# StaticFiles doesn't know the .mjs extension, so it serves ES modules as
# text/plain - which browsers refuse to execute (the "disallowed MIME type"
# error). On some systems mimetypes also mis-resolves .js to application/json,
# so force both to JavaScript.
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/javascript", ".js")


@asynccontextmanager
async def _warm_up_speaker_models(app: FastAPI):
    """Loads Resemblyzer (the only speaker-verification backend - see
    requirements.txt for why ECAPA-TDNN/SpeechBrain were dropped) into
    memory once at app boot, rather than lazily on first Record/threshold-
    test click. Resemblyzer's own model cache persists under the OS-managed
    data directory (see constants.py's DATA_DIR, not a path inside the
    project tree) on first download, so this only pays the real download
    cost on a machine's very first run. Kicked off as a background task so
    it never blocks the app from actually starting up.
    """
    # Initialize DB at server boot
    memory.init_db()
    quizzes.init_db()

    # One-time (per-profile), idempotent - see migrate_legacy_profile_state's
    # own docstring for why this has to run AFTER memory.init_db() (needs
    # the profile_state table to already exist) and what it protects
    # against (existing users' stats silently resetting to zero on this
    # update).
    migrate_legacy_profile_state()

    # Same idempotent-startup-migration posture as above, for a different
    # problem: a conversation left over from before a model was retired
    # from MODEL_OPTIONS (see constants.py) would otherwise keep using it
    # forever, with no UI to fix it after the fact - see
    # migrate_legacy_model_name's own docstring.
    migrate_legacy_model_name()

    # Kick off background model warmup
    asyncio.create_task(asyncio.to_thread(speech_detection.warm_up))

    yield  # Hand control back to FastAPI so the server starts listening


app = FastAPI(lifespan=_warm_up_speaker_models)

app.include_router(routes_pages.router)
app.include_router(routes_api.router)
app.include_router(live_session.router)


@app.middleware("http")
async def _disable_static_caching(request: Request, call_next):
    """This app is only ever served to one local desktop window, not a
    CDN-fronted public site, so there's no upside to browser caching of the
    frontend - only downside, since desktop.py keeps a persistent WebView2
    profile across launches (so the microphone permission prompt doesn't
    reappear every restart), and that same persistence means the browser
    cache also survives restarts. Marking every non-API, non-websocket
    response as non-cacheable ensures frontend changes always show up on
    the next launch.
    """
    response = await call_next(request)
    path = request.url.path
    if not path.startswith("/api/") and path != "/ws/session":
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# html=True is unneeded - "/" is served by routes_pages.py, rendered via
# Jinja2 rather than a flat static index.html.
#
# Two different physical locations back the same URL space the frontend
# expects:
#  - /avatar, /voices, /photos, /marketing come from ASSETS_DIR (see
#    constants.py) - downloaded by `thirtytutors setup`/first run (cli.py),
#    not bundled in the pip package.
#  - Everything else (UI/styles, UI/scripts, vendor/, pcm-processor.js,
#    the platform icons) is bundled with the package under static/,
#    resolved relative to this file rather than the process's current
#    working directory. UI/avatar_test and UI/game_test additionally get
#    their own dedicated mount below (rather than relying on the catch-
#    all at the bottom), so files inside them can use the same shallow
#    relative import paths ("../vendor/...") regardless of how deep
#    they'd otherwise sit under UI/ via the catch-all.
STATIC_DIR = Path(__file__).parent / "static"


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Browsers auto-probe GET /favicon.ico on every page load regardless
    of the explicit <link rel="icon"> in index.html - without this route
    that probe just 404s (harmless, but noisy in the terminal).
    """
    return FileResponse(STATIC_DIR / "ThirtyTutors.ico")


# check_dir=False (below) only skips StaticFiles' directory check at
# MOUNT time, letting the app start even before `thirtytutors setup` has
# downloaded anything - it does NOT skip that same check on every
# individual REQUEST afterward. Starlette's check_config() runs on every
# request regardless, and raises a full RuntimeError (500, with a giant
# traceback logged per request) if the directory is still missing then -
# it does NOT degrade to a quiet 404 the way a missing individual FILE
# inside an already-existing directory does. A person opening the app
# before setup has ever downloaded anything (or, as happened here, before
# a brand new asset kind's directory has ever been created) would see
# that traceback spammed once per asset request - 60+ on a single landing-
# page load, between the tutor-card marquee and the two hero/dance videos.
# Pre-creating each directory (even empty) below means check_config()
# always finds something to stat, so a still-missing individual file just
# 404s normally and quietly instead.
for _kind in ("avatar", "voices", "photos", "marketing"):
    (ASSETS_DIR / _kind).mkdir(parents=True, exist_ok=True)

app.mount(
    "/avatar",
    StaticFiles(directory=str(ASSETS_DIR / "avatar"), check_dir=False),
    name="avatar_assets",
)
app.mount(
    "/voices",
    StaticFiles(directory=str(ASSETS_DIR / "voices"), check_dir=False),
    name="voice_assets",
)
app.mount(
    "/photos",
    StaticFiles(directory=str(ASSETS_DIR / "photos"), check_dir=False),
    name="photo_assets",
)
app.mount(
    "/marketing",
    StaticFiles(directory=str(ASSETS_DIR / "marketing"), check_dir=False),
    name="marketing_assets",
)
avatar_test_dir = STATIC_DIR / "UI" / "avatar_test"
if avatar_test_dir.exists():
    app.mount(
        "/avatar_test",
        StaticFiles(directory=str(avatar_test_dir)),
        name="avatar_test_assets",
    )
game_test_dir = STATIC_DIR / "UI" / "game_test"
if game_test_dir.exists():
    app.mount(
        "/game_test",
        StaticFiles(directory=str(game_test_dir)),
        name="game_test_assets",
    )
app.mount("/", StaticFiles(directory=str(STATIC_DIR)), name="static")
