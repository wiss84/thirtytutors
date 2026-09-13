"""Server-rendered page shells (static/UI, composed via Jinja2). Split out
of main.py - each route just renders one page/*.html.
"""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from . import updater

# Frontend is composed via Jinja2 from static/UI (index.html layout +
# pages/ + styles/ + scripts/) instead of one flat index.html/app.js/
# style.css, so pages/styles/scripts stay short and descriptively named
# rather than growing into large monolithic files. Static assets under
# static/UI/styles and static/UI/scripts, plus vendor/, are bundled with
# the package and served as plain files by the StaticFiles mount in
# main.py; avatar/voices/photos come from the separately-downloaded
# ASSETS_DIR instead (see main.py and constants.py) - only the page shell
# itself is server-rendered. Resolved relative to this file rather than
# the process's current working directory, same reasoning as main.py's
# STATIC_DIR - a pip-installed app can't assume it's run from a repo root.
templates = Jinja2Templates(directory=str(Path(__file__).parent / "static" / "UI"))

router = APIRouter()


@router.get("/")
async def serve_learning_page(request: Request):
    # `request` is passed as TemplateResponse's first positional argument,
    # not inside a context dict - Starlette's current calling convention.
    return templates.TemplateResponse(request, "pages/learning.html")


@router.get("/landing")
async def serve_landing_page(request: Request):
    return templates.TemplateResponse(request, "pages/landing.html")


@router.get("/get-started")
async def serve_get_started_page(request: Request):
    return templates.TemplateResponse(request, "pages/get_started.html")


@router.get("/avatar-select")
async def serve_avatar_select_page(request: Request):
    return templates.TemplateResponse(request, "pages/avatar_select.html")


@router.get("/profiles")
async def serve_profiles_page(request: Request):
    return templates.TemplateResponse(request, "pages/profiles.html")


@router.get("/handsfree-setup")
async def serve_handsfree_setup_page(request: Request):
    return templates.TemplateResponse(request, "pages/handsfree_setup.html")


@router.get("/quiz-mode")
async def serve_quiz_mode_page(request: Request):
    return templates.TemplateResponse(request, "pages/quiz_mode.html")


@router.get("/whats-new")
async def serve_whats_new_page(request: Request, version: str | None = None):
    installed_version = updater.installed_app_version()
    updater.mark_version_seen(installed_version)

    shown_version = version or installed_version
    html = updater.render_release_notes_html(shown_version)
    versions = updater.list_release_versions()

    return templates.TemplateResponse(
        request,
        "pages/whats_new.html",
        {
            "shown_version": shown_version,
            "release_html": html,
            "versions": versions,
        },
    )


# Dev-only sandbox pages for the 3D room/character controller work
# under static/UI/game_test (see PROGRESS_AND_NEXT_STEPS.md there) -
# deliberately separate paths from the /game_test StaticFiles mount
# registered in main.py, rather than sub-paths of it, so an explicit
# route here never has to rely on registration order against that mount
# to be found. Templates referenced below are still resolved against
# this module's existing `templates` root (static/UI), same as every
# other route.
@router.get("/game-test-scene")
async def serve_game_test_scene_page(request: Request):
    return templates.TemplateResponse(request, "game_test/scene.html")


@router.get("/game-test-room-inspect")
async def serve_game_test_room_inspect_page(request: Request):
    return templates.TemplateResponse(request, "game_test/room_inspect.html")
