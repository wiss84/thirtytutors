"""Reference-data endpoints (voices/models/avatars) + the profile and
conversation REST CRUD API. Split out of main.py.
"""

import asyncio
import base64
import os
import re
import subprocess
import sys
import time
import uuid
import zipfile
from importlib import metadata as importlib_metadata
from pathlib import Path
from urllib.parse import quote

import numpy as np
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import Response

from . import backup, memory, quizzes, scenarios, speech_detection, stats, updater
from .constants import (
    APP_VERSION,
    ASSETS_DIR,
    DATA_DIR,
    DEFAULT_DIFFICULTY,
    DEFAULT_MIC_CALIBRATION_KEY,
    DEFAULT_MODEL,
    DEFAULT_NATIVE_LANGUAGE,
    DEFAULT_TARGET_LANGUAGE,
    DEFAULT_VOICE,
    ENROLLMENT_SENTENCES,
    MODEL_OPTIONS,
    PROFILE_EDITABLE_FIELDS,
    SPEAKER_VERIFICATION_BACKEND,
    THRESHOLD_TEST_NOISE_PROMPT,
    THRESHOLD_TEST_SENTENCES,
    VOICE_OPTIONS,
)
from .export_docx import build_notes_docx
from .languages import LANGUAGES
from .profiles_store import (
    delete_profile,
    get_profile_by_id,
    load_profiles,
    patch_profile,
    save_profiles,
)

router = APIRouter()


# --- Reference data endpoints ---


@router.get("/api/voices")
def get_voices():
    return {"voices": VOICE_OPTIONS, "default": DEFAULT_VOICE}


@router.get("/api/languages")
def get_languages():
    """Autocomplete suggestions for the native/target language text inputs
    on /get-started, /avatar-select, and the Settings modal's General tab
    (see languageAutocomplete.js) - a curated list, not a hard allowlist;
    see languages.py's own module docstring for why those inputs stay
    plain free text either way.
    """
    return {"languages": LANGUAGES}


@router.get("/api/models")
def get_models():
    return {"models": MODEL_OPTIONS, "default": DEFAULT_MODEL}


@router.get("/api/scenarios")
def get_scenarios():
    return {
        "scenarios": scenarios.SCENARIO_OPTIONS,
        "default": scenarios.DEFAULT_SCENARIO,
    }


@router.get("/api/app-info")
def get_app_info():
    """Backs the Settings modal's About tab (see settings.js). Prefers the
    INSTALLED package's own metadata (what pip/importlib actually knows,
    derived from constants.APP_VERSION at build time - see pyproject.toml's
    [tool.setuptools.dynamic] section) over importing APP_VERSION directly,
    since that's the more robust "what version is this actually running"
    check. Falls back to the constant only if the package was never really
    installed at all (e.g. running straight from a source checkout).
    """
    try:
        version = importlib_metadata.version("thirtytutors")
    except importlib_metadata.PackageNotFoundError:
        version = APP_VERSION
    return {
        "version": version,
        "credits": ["Gemini Live API", "Resemblyzer", "TalkingHead"],
    }


@router.get("/api/avatars")
def get_available_avatars():
    """Which voices currently have a real 3D avatar file, for the
    avatar-select page to gate which grid tiles are clickable vs
    "Coming soon" - avatars are being made one at a time, so this list
    grows over time without any code change needed. Reads from ASSETS_DIR
    (see constants.py), not the package's own bundled static/ - avatar
    .glb files are downloaded separately (see cli.py), not shipped in the
    pip package.
    """
    avatar_dir = ASSETS_DIR / "avatar"
    if not avatar_dir.is_dir():
        return {"available": []}
    available = sorted(p.name[: -len("_th.glb")] for p in avatar_dir.glob("*_th.glb"))
    return {"available": available}


def _resolve_packaged_open_target(path: Path) -> tuple[str, dict]:
    """See this function's previous docstring for the full redirection
    explanation. Returns (resolved_path, debug) - debug records exactly
    which step succeeded/failed, since a silent fallback here gives no
    way to tell "not a packaged build" apart from "should have worked but
    didn't" from the outside.
    """
    debug = {"platform": sys.platform, "original_path": str(path)}
    if sys.platform != "win32":
        debug["result"] = "not windows"
        return str(path), debug
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        length = ctypes.c_uint32(0)
        first_call_result = kernel32.GetCurrentPackageFamilyName(ctypes.byref(length), None)
        debug["first_call_result"] = first_call_result
        debug["first_call_length"] = length.value
        if length.value == 0:
            debug["result"] = "not a packaged process (length 0 on first call)"
            return str(path), debug
        buf = ctypes.create_unicode_buffer(length.value)
        second_call_result = kernel32.GetCurrentPackageFamilyName(ctypes.byref(length), buf)
        debug["second_call_result"] = second_call_result
        if second_call_result != 0:
            debug["result"] = f"second call failed, code {second_call_result}"
            return str(path), debug
        package_family_name = buf.value
        debug["package_family_name"] = package_family_name
    except (AttributeError, OSError) as e:
        debug["result"] = f"exception calling GetCurrentPackageFamilyName: {type(e).__name__}: {e}"
        return str(path), debug

    local_appdata = os.environ.get("LOCALAPPDATA")
    debug["local_appdata"] = local_appdata
    if not local_appdata:
        debug["result"] = "no LOCALAPPDATA env var"
        return str(path), debug
    try:
        relative = path.relative_to(local_appdata)
    except ValueError as e:
        debug["result"] = f"path not under LOCALAPPDATA: {e}"
        return str(path), debug
    real_path = Path(local_appdata) / "Packages" / package_family_name / "LocalCache" / "Local" / relative
    debug["computed_real_path"] = str(real_path)
    debug["computed_real_path_is_dir"] = real_path.is_dir()
    if real_path.is_dir():
        debug["result"] = "resolved successfully"
        return str(real_path), debug
    debug["result"] = "computed real path doesn't exist - falling back to original"
    return str(path), debug


@router.post("/api/open-data-folder")
def open_data_folder():
    """Opens the app's data directory (profiles, conversations, voice
    enrollment, downloaded assets) in the OS file explorer.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path, debug = _resolve_packaged_open_target(DATA_DIR)
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.run(["open", path], check=False)
    else:
        subprocess.run(["xdg-open", path], check=False)
    return {"opened": True, "path": path, "debug": debug}


@router.post("/api/open-backups-folder")
def open_backups_folder():
    """Opens the automatic-backup destination (see backup.AUTO_BACKUP_DIR)
    in the OS file explorer - same pattern as open_data_folder above, the
    Settings modal's Backup section's way of showing where auto-backups
    land without a full custom save-location picker.
    """
    backup.AUTO_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    path, debug = _resolve_packaged_open_target(backup.AUTO_BACKUP_DIR)
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.run(["open", path], check=False)
    else:
        subprocess.run(["xdg-open", path], check=False)
    return {"opened": True, "path": path, "debug": debug}


# --- Update check / apply (top-bar notification, profile dropdown,
# Settings > Updates tab - see update.js and settings.js) ---

_update_status_cache = {"ts": 0.0, "data": None}
_UPDATE_STATUS_CACHE_SECONDS = (
    300  # PyPI is a network round trip; every page load re-checking would be needlessly chatty for a local desktop app
)


@router.get("/api/update-status")
async def get_update_status(force: bool = False):
    """Backs the top-bar bell, the profile dropdown's persistent update
    button, and the Settings > Updates tab - all three read this same
    endpoint rather than each doing their own check. Cached for a few
    minutes; pass ?force=true to bypass (used by the Settings tab's own
    "Check for Updates" button, not the silent on-load check every page
    does).
    """
    now = time.time()
    cached = _update_status_cache["data"]
    if not force and cached is not None and (now - _update_status_cache["ts"]) < _UPDATE_STATUS_CACHE_SECONDS:
        return cached

    app_info = await asyncio.to_thread(updater.check_app_update)
    assets_info = updater.check_assets_update()  # local file comparison only, no network - fine directly on the event loop
    marketing_info = updater.check_marketing_assets_update()  # same - local only
    data = {"app": app_info, "assets": assets_info, "marketing": marketing_info}
    _update_status_cache["ts"] = now
    _update_status_cache["data"] = data
    return data


@router.post("/api/update-app")
async def update_app_endpoint():
    """Runs `pip install --upgrade thirtytutors`, off the event loop -
    reports success/failure but does NOT restart the app itself; the
    frontend calls /api/restart-app separately once this succeeds, so a
    failed upgrade never leaves the app mid-restart.
    """
    success, output = await asyncio.to_thread(updater.run_pip_upgrade)
    if not success:
        raise HTTPException(500, f"Update failed: {output[-2000:]}")
    _update_status_cache["data"] = None  # force a fresh check next time - the installed version just changed
    return {"success": True}


@router.post("/api/update-assets")
async def update_assets_endpoint():
    """Re-downloads the avatar/voice/photo bundle. Unlike an app-code
    update, this needs no relaunch - StaticFiles (see main.py) serves
    these straight off disk, so a fresh page load already picks up the
    new files.
    """
    try:
        await asyncio.to_thread(updater.download_and_extract_assets, True, None)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    _update_status_cache["data"] = None
    return {"success": True}


@router.post("/api/update-marketing-assets")
async def update_marketing_assets_endpoint():
    """Re-downloads the landing-page video/gif bundle. Unlike
    update_assets_endpoint above, the frontend DOES follow this with a
    relaunch (see update.js's describeUpdate) even though nothing here
    technically requires one either - /landing is only ever organically
    visited right after each app launch, so relaunching is the only way
    the refresh actually gets seen rather than sitting silently correct on
    disk until the person happens to click Home again.
    """
    try:
        await asyncio.to_thread(updater.download_and_extract_marketing_assets, True, None)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    _update_status_cache["data"] = None
    return {"success": True}


@router.get("/api/download-progress")
async def download_progress_endpoint():
    """Polled by the frontend (see update.js) while any of the three
    endpoints above is in flight, to drive a live progress bar instead of
    a static "Downloading..." string for however long a large asset zip
    takes - see updater.get_download_progress's own docstring. Local dict
    read, no thread needed.
    """
    return updater.get_download_progress()


@router.post("/api/restart-app")
async def restart_app_endpoint():
    """Spawns a fresh, fully detached `thirtytutors` process, then closes
    THIS process's own pywebview window - which is what actually lets
    this process exit (desktop.py's run() blocks on webview.start() until
    the window closes, then returns; nothing else keeps the process alive
    after that - see updater.close_this_window's own docstring). The new
    window opens on its own; there's no dead leftover window to close by
    hand.
    """
    updater.relaunch_app()
    closed = updater.close_this_window()
    return {"restarting": True, "window_closed": closed}


@router.get("/api/whats-new-status")
async def get_whats_new_status():
    """Backs the top-bar bell's "what's new" row - whether the currently
    installed version differs from the last one the app recorded as seen.
    Local file comparison only, no network - safe directly on the event
    loop.
    """
    return updater.check_whats_new()


@router.get("/api/release-notes/{version}")
async def get_release_notes(version: str):
    html = updater.render_release_notes_html(version)
    if html is None:
        raise HTTPException(404, f"No release notes found for version {version}")
    return {"version": version, "html": html}


# --- Profiles REST API ---


@router.get("/api/profiles")
def list_profiles():
    return {"profiles": [{"id": p["id"], "name": p["name"]} for p in load_profiles()]}


@router.post("/api/profiles")
async def create_profile(request: Request):
    payload = await request.json()
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Name is required")
    api_key = (payload.get("api_key") or "").strip() or None

    profile = {
        "id": str(uuid.uuid4()),
        "name": name,
        "api_key": api_key,
        "langfuse_public_key": None,
        "langfuse_secret_key": None,
        "langfuse_base_url": None,
        "mic_device_id": None,
        "mic_label": None,
        "voice_gender": next((v["gender"] for v in VOICE_OPTIONS if v["name"] == DEFAULT_VOICE), "Female"),
        # Starting point only - avatarSelect.js reads this back as the
        # default native_language/model_name when adding a new language to
        # this profile later, and native_language is also directly editable
        # from the Settings modal's General tab (settings.js). Every
        # conversation still stores its own independent copy once created
        # (see memory.py) - these two are never read again after that.
        "native_language": DEFAULT_NATIVE_LANGUAGE,
        "model_name": DEFAULT_MODEL,
        # Starting point for the difficulty toggle next time this profile
        # adds a new language (see avatarSelect.js) - editable from the
        # Settings modal's Learning tab (settings.js).
        "default_difficulty": DEFAULT_DIFFICULTY,
        "active_conversation_id": None,
        # Keyed by mic label (see constants.DEFAULT_MIC_CALIBRATION_KEY for
        # the default-mic sentinel) - each entry:
        # {"silence_rms_threshold": float, "speaker_threshold": float,
        #  "calibrated": bool, "tested": bool}. Per-mic so switching mics
        # never overwrites another mic's calibration - see handsfreeSetup.js.
        "mic_calibrations": {},
        # Backup (see backup.py) - auto_backup_interval_days only takes
        # effect once auto_backup_enabled is true. Stats-tab bookkeeping
        # (total_seconds_studied, last_active_date, current_streak,
        # seen_milestones) and last_auto_backup_at used to live here too,
        # but now live in memory.db's profile_state table (see memory.py) -
        # SQLite's transactional writes are far more crash-resistant than
        # rewriting this whole JSON file, which matters since those are the
        # fields updated automatically and most frequently, including right
        # as the app is closing (see live_session.py's ws_session `finally`
        # block). A brand new profile simply has no profile_state row yet -
        # memory.get_profile_state returns the all-zero defaults for that
        # case, so no placeholder fields are needed here.
        "auto_backup_enabled": False,
        "auto_backup_interval_days": 7,
    }
    profiles = load_profiles()
    profiles.append(profile)
    save_profiles(profiles)
    return profile


@router.get("/api/profiles/{profile_id}")
def get_profile(profile_id: str):
    profile = get_profile_by_id(profile_id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    return profile


@router.put("/api/profiles/{profile_id}")
async def update_profile(profile_id: str, request: Request):
    payload = await request.json()
    fields = {k: v for k, v in payload.items() if k in PROFILE_EDITABLE_FIELDS}
    updated = patch_profile(profile_id, fields)
    if updated is None:
        raise HTTPException(404, "Profile not found")
    return updated


@router.delete("/api/profiles/{profile_id}")
def remove_profile(profile_id: str):
    if not delete_profile(profile_id):
        raise HTTPException(404, "Profile not found")
    for conv in memory.list_conversations(profile_id):
        memory.delete_conversation(conv["id"])
    memory.delete_profile_state(profile_id)
    speech_detection.delete_enrollment(profile_id)
    return {"deleted": True}


# --- Profile backup (export/import) - see backup.py for the actual zip
# packaging; these are thin HTTP wrappers around it. Reached only through
# the running app's own UI (Settings modal's Data controls tab), never a
# standalone CLI path - init_db() is guaranteed to have already run by the
# time any of these can be reached, since it's called unconditionally from
# main.py's lifespan hook on every server start. ---


@router.get("/api/profiles/{profile_id}/export")
def export_profile_endpoint(profile_id: str):
    """Downloads a full backup zip for one profile - profile identity,
    every conversation/quiz session under it, and its voice enrollment.
    Same Content-Disposition pattern as the notes docx export just below
    (ASCII fallback + percent-encoded filename* - see that endpoint's own
    comment for why a plain UTF-8 filename can't go straight into an HTTP
    header).
    """
    profile = get_profile_by_id(profile_id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    zip_bytes = backup.build_profile_backup_zip(profile_id)
    stamp = time.strftime("%Y%m%d")
    display_name = f"ThirtyTutors-{profile.get('name') or 'profile'}-{stamp}.zip"
    ascii_fallback = re.sub(r"[^\x20-\x7e]", "_", display_name)
    headers = {
        "Content-Disposition": f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(display_name)}",
    }
    return Response(content=zip_bytes, media_type="application/zip", headers=headers)


@router.post("/api/profiles/import")
async def import_profile_endpoint(file: UploadFile = File(...)):
    """Restores a profile from a backup zip (see export_profile_endpoint
    above / backup.py). If a profile with the same id already exists
    locally, it's fully replaced - not merged - per backup.py's own
    documented behavior.
    """
    zip_bytes = await file.read()
    try:
        profile_id = backup.import_profile_backup_zip(zip_bytes)
    except (KeyError, ValueError, zipfile.BadZipFile) as e:
        raise HTTPException(400, f"Could not read this backup file: {type(e).__name__}: {e}")
    profile = get_profile_by_id(profile_id)
    return {"imported": True, "profile": profile}


@router.post("/api/profiles/{profile_id}/check-auto-backup")
def check_auto_backup_endpoint(profile_id: str):
    """Called once per app session (see init.js) - runs
    backup.maybe_run_auto_backup, which itself no-ops unless this profile
    has auto-backup on AND its configured interval has actually elapsed.
    """
    if get_profile_by_id(profile_id) is None:
        raise HTTPException(404, "Profile not found")
    ran = backup.maybe_run_auto_backup(profile_id)
    return {"ran": ran}


@router.get("/api/profiles/{profile_id}/stats")
def get_profile_stats_endpoint(profile_id: str):
    """Backs the Settings modal's Stats tab (see statsPane.js) - per-
    language breakdown plus profile-wide totals (see stats.py), with the
    stats-tab fields it doesn't otherwise expose (total_seconds_studied/
    current_streak/last_active_date - see memory.py's profile_state table)
    folded straight into the same response so the frontend only needs one
    fetch.
    """
    profile = get_profile_by_id(profile_id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    data = stats.get_profile_stats(profile_id)
    state = memory.get_profile_state(profile_id)
    return {
        **data,
        "total_seconds_studied": state["total_seconds_studied"],
        "current_streak": state["current_streak"],
        "last_active_date": state["last_active_date"],
    }


@router.get("/api/profiles/{profile_id}/milestone-status")
def get_milestone_status_endpoint(profile_id: str):
    """Backs the top-bar notification bell's milestone rows (see
    update.js) - every vocab/streak tier crossed since this was last
    checked. Marks them seen as a side effect of this GET itself (see
    stats.get_new_milestones's own docstring for why), so this returns
    the same newly-crossed set at most once.
    """
    if get_profile_by_id(profile_id) is None:
        raise HTTPException(404, "Profile not found")
    return {"new_milestones": stats.get_new_milestones(profile_id)}


@router.get("/api/profiles/{profile_id}/mic-status")
def get_mic_status(profile_id: str):
    """One row per mic this profile has ever calibrated - backs the
    Settings modal's Voice & hands-free tab (see settings.js). Calibrated/
    tested come straight off profile.mic_calibrations; enrolled is checked
    separately since it's stored on disk per speech_detection, not on the
    profile.
    """
    profile = get_profile_by_id(profile_id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    calibrations = profile.get("mic_calibrations") or {}
    mics = []
    for mic_key, cal in calibrations.items():
        mics.append(
            {
                "mic_key": mic_key,
                "label": "Default microphone" if mic_key == DEFAULT_MIC_CALIBRATION_KEY else mic_key,
                "calibrated": bool(cal.get("calibrated")),
                "tested": bool(cal.get("tested")),
                "enrolled": speech_detection.has_enrollment(profile_id, mic_key),
            }
        )
    return {"mics": mics}


# --- Voice enrollment (speaker verification for hands-free mode) ---


@router.get("/api/voice-enrollment-sentences")
def get_voice_enrollment_sentences():
    return {"sentences": ENROLLMENT_SENTENCES}


@router.get("/api/speech-detection-status")
def get_speech_detection_status():
    """Lets the frontend gate the Record/threshold-test buttons on whether
    the speaker-verification model has actually finished loading -
    warm_up() (see main.py's startup hook) can take a real amount of time
    on a machine's very first run (downloading model weights), and without
    this a click during that window would just hang or fail with no
    explanation.
    """
    return {
        "backend": SPEAKER_VERIFICATION_BACKEND,
        "status": speech_detection.get_status(),
    }


@router.get("/api/profiles/{profile_id}/voice-enrollment")
def get_voice_enrollment_status(profile_id: str, mic_key: str):
    if get_profile_by_id(profile_id) is None:
        raise HTTPException(404, "Profile not found")
    return {"enrolled": speech_detection.has_enrollment(profile_id, mic_key)}


@router.post("/api/profiles/{profile_id}/voice-enrollment")
async def submit_voice_enrollment(profile_id: str, request: Request):
    if get_profile_by_id(profile_id) is None:
        raise HTTPException(404, "Profile not found")
    payload = await request.json()
    mic_key = payload.get("mic_key")
    if not mic_key:
        raise HTTPException(400, "mic_key is required.")
    clips_b64 = payload.get("samples") or []
    if not clips_b64:
        raise HTTPException(400, "At least one recorded sample is required.")

    samples = []
    for clip in clips_b64:
        pcm_bytes = base64.b64decode(clip)
        int16 = np.frombuffer(pcm_bytes, dtype=np.int16)
        samples.append(int16.astype(np.float32) / 32768.0)

    try:
        # Embedding is CPU/torch work - run off the event loop so it
        # doesn't block other requests (and any open Live sessions) while
        # it runs, same reasoning as summarize_conversation's own
        # asyncio.to_thread usage in live_session.py.
        await asyncio.to_thread(speech_detection.enroll_profile, profile_id, mic_key, samples)
    except (OSError, RuntimeError, ValueError) as e:
        raise HTTPException(500, f"Voice enrollment failed: {type(e).__name__}: {e}")
    return {"enrolled": True}


@router.delete("/api/profiles/{profile_id}/voice-enrollment")
def remove_voice_enrollment(profile_id: str, mic_key: str):
    if get_profile_by_id(profile_id) is None:
        raise HTTPException(404, "Profile not found")
    speech_detection.delete_enrollment(profile_id, mic_key)
    return {"enrolled": False}


# --- Threshold-calibration test (hands-free-setup page) ---


@router.get("/api/threshold-test-sentences")
def get_threshold_test_sentences():
    return {
        "sentences": THRESHOLD_TEST_SENTENCES,
        "noise_prompt": THRESHOLD_TEST_NOISE_PROMPT,
    }


@router.post("/api/profiles/{profile_id}/voice-enrollment-test")
async def test_voice_enrollment_sample(profile_id: str, request: Request):
    """Scores one recorded clip against this mic's enrolled reference - no
    pass/fail threshold applied here, just the raw similarity number. Used
    both for the 5 THRESHOLD_TEST_SENTENCES readings and for the one
    background-noise capture the hands-free-setup page also collects -
    scoring doesn't care what the clip actually contains, it just measures
    similarity to the reference either way.
    """
    if get_profile_by_id(profile_id) is None:
        raise HTTPException(404, "Profile not found")

    payload = await request.json()
    mic_key = payload.get("mic_key")
    if not mic_key:
        raise HTTPException(400, "mic_key is required.")
    if not speech_detection.has_enrollment(profile_id, mic_key):
        raise HTTPException(400, "This profile hasn't completed voice enrollment for this mic yet.")

    clip_b64 = payload.get("sample")
    if not clip_b64:
        raise HTTPException(400, "A recorded sample is required.")

    pcm_bytes = base64.b64decode(clip_b64)
    int16 = np.frombuffer(pcm_bytes, dtype=np.int16)
    audio = int16.astype(np.float32) / 32768.0

    try:
        score = await asyncio.to_thread(speech_detection.score, profile_id, mic_key, audio)
    except (OSError, RuntimeError, ValueError) as e:
        raise HTTPException(500, f"Scoring failed: {type(e).__name__}: {e}")
    return {"score": score}


# --- Conversations REST API (per profile) ---


@router.get("/api/profiles/{profile_id}/conversations")
def list_conversations_endpoint(profile_id: str):
    profile = get_profile_by_id(profile_id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    return {
        "conversations": memory.list_conversations(profile_id),
        "active_conversation_id": profile.get("active_conversation_id"),
    }


@router.post("/api/profiles/{profile_id}/conversations")
async def create_conversation_endpoint(profile_id: str, request: Request):
    profile = get_profile_by_id(profile_id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    payload = await request.json()
    config = {
        "voice_name": payload.get("voice_name") or profile.get("voice_name") or DEFAULT_VOICE,
        "native_language": payload.get("native_language") or profile.get("native_language") or DEFAULT_NATIVE_LANGUAGE,
        "target_language": payload.get("target_language") or profile.get("target_language") or DEFAULT_TARGET_LANGUAGE,
        "model_name": payload.get("model_name") or profile.get("model_name") or DEFAULT_MODEL,
        "scenario": payload.get("scenario") or scenarios.DEFAULT_SCENARIO,
        "difficulty": payload.get("difficulty") or profile.get("default_difficulty") or DEFAULT_DIFFICULTY,
    }
    name = (payload.get("name") or "").strip() or None
    conv = memory.create_conversation(profile_id, config, name=name)
    patch_profile(profile_id, {"active_conversation_id": conv["id"]})
    return conv


@router.get("/api/profiles/{profile_id}/conversations/{conversation_id}")
def get_conversation_endpoint(profile_id: str, conversation_id: str):
    conv = memory.get_conversation(conversation_id)
    if conv is None or conv["profile_id"] != profile_id:
        raise HTTPException(404, "Conversation not found")
    turns = memory.get_turns(conversation_id)
    summary = memory.get_summary(conversation_id)
    voice_name = (conv.get("config") or {}).get("voice_name") or DEFAULT_VOICE
    tutor_name = next(
        (v.get("alias") or v["name"] for v in VOICE_OPTIONS if v["name"] == voice_name),
        voice_name,
    )
    return {
        **conv,
        "turns": turns,
        "summary": summary["summary"] if summary else None,
        "tutor_name": tutor_name,
    }


@router.put("/api/profiles/{profile_id}/conversations/{conversation_id}")
async def update_conversation_endpoint(profile_id: str, conversation_id: str, request: Request):
    conv = memory.get_conversation(conversation_id)
    if conv is None or conv["profile_id"] != profile_id:
        raise HTTPException(404, "Conversation not found")
    payload = await request.json()

    name = payload.get("name")
    if isinstance(name, str):
        name = name.strip() or None

    config = None
    if any(
        k in payload
        for k in (
            "voice_name",
            "native_language",
            "target_language",
            "model_name",
            "difficulty",
        )
    ):
        config = dict(conv["config"])
        for k in (
            "voice_name",
            "native_language",
            "target_language",
            "model_name",
            "difficulty",
        ):
            if payload.get(k):
                config[k] = payload[k]

    updated = memory.update_conversation(conversation_id, name=name, config=config)
    return updated


@router.delete("/api/profiles/{profile_id}/conversations/{conversation_id}")
def delete_conversation_endpoint(profile_id: str, conversation_id: str):
    conv = memory.get_conversation(conversation_id)
    if conv is None or conv["profile_id"] != profile_id:
        raise HTTPException(404, "Conversation not found")
    memory.delete_conversation(conversation_id)

    profile = get_profile_by_id(profile_id)
    if profile and profile.get("active_conversation_id") == conversation_id:
        remaining = memory.list_conversations(profile_id)
        patch_profile(
            profile_id,
            {"active_conversation_id": remaining[0]["id"] if remaining else None},
        )
    return {"deleted": True}


@router.get("/api/profiles/{profile_id}/conversations/{conversation_id}/notes")
def get_conversation_notes_endpoint(profile_id: str, conversation_id: str):
    conv = memory.get_conversation(conversation_id)
    if conv is None or conv["profile_id"] != profile_id:
        raise HTTPException(404, "Conversation not found")
    return {
        "vocab_mistakes": memory.get_vocab_mistakes(conversation_id),
        "lesson_log": memory.get_lesson_log(conversation_id),
    }


@router.get("/api/profiles/{profile_id}/conversations/{conversation_id}/notes/export.docx")
def export_conversation_notes_docx(profile_id: str, conversation_id: str):
    """Word export of the same content the notes modal shows - see
    export_docx.py. Returned as a plain attachment download.
    """
    profile = get_profile_by_id(profile_id)
    conv = memory.get_conversation(conversation_id)
    if profile is None or conv is None or conv["profile_id"] != profile_id:
        raise HTTPException(404, "Conversation not found")

    language_label = conv.get("name") or (conv.get("config") or {}).get("target_language") or "Conversation"
    docx_bytes = build_notes_docx(
        profile.get("name") or "",
        language_label,
        memory.get_vocab_mistakes(conversation_id),
        memory.get_lesson_log(conversation_id),
    )
    display_name = f"{language_label} - Learnt Notes.docx".replace("/", "-")
    # Conversation names routinely contain non-ASCII characters (every
    # auto-named conversation has a middle dot - see
    # memory.default_conversation_name), and HTTP header values are Latin-1,
    # not UTF-8 - passing one straight into Content-Disposition produces a
    # byte an HTTP client can't safely decode back as UTF-8 (caught by the
    # test suite as a UnicodeDecodeError, but the real failure mode for an
    # actual user is a broken/garbled downloaded filename). RFC 6266 covers
    # exactly this: an ASCII-only `filename` fallback plus a percent-encoded
    # `filename*` for clients that support it - every modern browser does.
    ascii_fallback = re.sub(r"[^\x20-\x7e]", "_", display_name)
    headers = {
        "Content-Disposition": f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(display_name)}",
    }
    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers=headers,
    )


# --- Test Yourself ("quiz mode") - a deterministic replay of a
# conversation's already-answered quiz items, no tutor/model involvement.
# See quizzes.get_reviewable_quiz_items for the data-source/dedup logic;
# these endpoints are thin wrappers around it plus the same start/answer/
# finalize quiz-session functions the live tutor's start_quiz tool already
# uses (see live_session.py), so a review run is a genuine quiz_sessions
# row like any other rather than a parallel, untracked path.

_REVIEWABLE_QUIZ_RANGES = ("all", "today", "week", "month")


def _get_owned_conversation(profile_id: str, conversation_id: str) -> dict:
    conv = memory.get_conversation(conversation_id)
    if conv is None or conv["profile_id"] != profile_id:
        raise HTTPException(404, "Conversation not found")
    return conv


@router.get("/api/profiles/{profile_id}/conversations/{conversation_id}/reviewable-quiz")
def get_reviewable_quiz_endpoint(profile_id: str, conversation_id: str, range: str = "all"):
    _get_owned_conversation(profile_id, conversation_id)
    if range not in _REVIEWABLE_QUIZ_RANGES:
        raise HTTPException(400, f"range must be one of: {', '.join(_REVIEWABLE_QUIZ_RANGES)}")
    items = quizzes.get_reviewable_quiz_items(conversation_id, range)
    return {"items": items, "range": range}


@router.post("/api/profiles/{profile_id}/conversations/{conversation_id}/reviewable-quiz/start")
async def start_reviewable_quiz_endpoint(profile_id: str, conversation_id: str, request: Request):
    _get_owned_conversation(profile_id, conversation_id)
    payload = await request.json()
    items = payload.get("items") or []
    if not items:
        raise HTTPException(400, "items is required and must be non-empty.")
    # The client sends back the exact (already deduplicated, already
    # shuffled) set it fetched and is about to show - this session's
    # payload should reflect what the student actually sees, the same way
    # a tutor-generated session's payload does.
    quiz_id = quizzes.start_quiz_session(conversation_id, "review", {"items": items})
    return {"quiz_id": quiz_id}


@router.post("/api/profiles/{profile_id}/conversations/{conversation_id}/reviewable-quiz/{quiz_id}/answer")
async def answer_reviewable_quiz_endpoint(profile_id: str, conversation_id: str, quiz_id: str, request: Request):
    _get_owned_conversation(profile_id, conversation_id)
    payload = await request.json()
    quizzes.record_item_answer(
        quiz_id,
        item_index=payload["item_index"],
        target_term=payload["target_term"],
        prompt_or_text=payload["prompt_or_text"],
        correct_answer=payload["correct_answer"],
        student_answer=payload.get("student_answer"),
        is_correct=bool(payload["is_correct"]),
    )
    return {"recorded": True}


@router.post("/api/profiles/{profile_id}/conversations/{conversation_id}/reviewable-quiz/{quiz_id}/finish")
def finish_reviewable_quiz_endpoint(profile_id: str, conversation_id: str, quiz_id: str, status: str = "completed"):
    _get_owned_conversation(profile_id, conversation_id)
    if status not in ("completed", "skipped"):
        raise HTTPException(400, "status must be 'completed' or 'skipped'.")
    quizzes.finalize_quiz_session(quiz_id, status=status)
    return {"finalized": True, "status": status}


@router.put("/api/profiles/{profile_id}/active-conversation")
async def set_active_conversation_endpoint(profile_id: str, request: Request):
    profile = get_profile_by_id(profile_id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    payload = await request.json()
    conversation_id = payload.get("conversation_id")
    conv = memory.get_conversation(conversation_id) if conversation_id else None
    if conv is None or conv["profile_id"] != profile_id:
        raise HTTPException(404, "Conversation not found")
    patch_profile(profile_id, {"active_conversation_id": conversation_id})
    return {"active_conversation_id": conversation_id}
