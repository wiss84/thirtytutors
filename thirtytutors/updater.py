"""Shared version/update-check logic for the app version (PyPI) and the
avatar/voice/photo asset bundle (GitHub Release) - used by both cli.py's
`thirtytutors setup`/every-launch notice and the running web app's top-bar
update notification + Settings > Updates tab (routes_api.py), so there's
one place that knows how to check, download, and apply an update instead
of two copies drifting apart.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from importlib import metadata as importlib_metadata
from pathlib import Path

from .constants import (
    APP_VERSION,
    ASSETS_DIR,
    ASSETS_RELEASE_TAG,
    ASSETS_VERSION,
    DATA_DIR,
    MARKETING_ASSETS_RELEASE_TAG,
    MARKETING_ASSETS_VERSION,
    RELEASES_DIR,
)

PYPI_PROJECT = "thirtytutors"
GITHUB_REPO = "wiss84/thirtytutors"

# Each zip's contents are extracted flat into ASSETS_DIR/<key>/ - e.g.
# avatars.zip should contain the .glb files directly at its root, not
# nested inside an "avatar/" folder of their own.
ASSET_FILES = {
    "avatar": "avatars.zip",
    "voices": "voices.zip",
    "photos": "photos.zip",
}

# Landing-page video/gif assets - same one-zip-per-kind, flat-extraction
# convention as ASSET_FILES above , but downloaded and
# versioned independently (see MARKETING_ASSETS_VERSION in constants.py).
MARKETING_ASSET_FILES = {
    "marketing": "marketing.zip",
}


# ---------------------------------------------------------------------------
# App version (PyPI)
# ---------------------------------------------------------------------------


def _installed_app_version() -> str:
    try:
        return importlib_metadata.version(PYPI_PROJECT)
    except importlib_metadata.PackageNotFoundError:
        return APP_VERSION


def installed_app_version() -> str:
    return _installed_app_version()


def _version_tuple(v: str) -> tuple:
    """Best-effort numeric parse ("0.3.1" -> (0, 3, 1)). Ignores any
    pre-release/build suffix - this app's own releases are plain X.Y.Z, so
    that's the only case this needs to get right; a genuinely malformed
    string just sorts as (0,), which reads as "not newer" rather than
    crashing anything that calls this.
    """
    parts = []
    for chunk in v.strip().split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(latest: str, current: str) -> bool:
    return _version_tuple(latest) > _version_tuple(current)


def get_latest_pypi_version(timeout: float = 3.0) -> str | None:
    """None on any failure (offline, PyPI down, unexpected response) -
    callers must treat that as "couldn't check," never as "no update
    available."
    """
    url = f"https://pypi.org/pypi/{PYPI_PROJECT}/json"
    req = urllib.request.Request(url, headers={"User-Agent": "thirtytutors-update-check"})
    try:
        import json

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("info", {}).get("version")
    except Exception:
        return None


def check_app_update(timeout: float = 3.0) -> dict:
    current = _installed_app_version()
    latest = get_latest_pypi_version(timeout=timeout)
    return {
        "current": current,
        "latest": latest,
        "update_available": bool(latest) and is_newer(latest, current),
    }


def run_pip_upgrade() -> tuple[bool, str]:
    """Blocking - callers on the web app side must run this via
    asyncio.to_thread rather than await it directly on the event loop.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", PYPI_PROJECT],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            return False, (result.stderr or result.stdout or "pip exited non-zero with no output.")
        return True, result.stdout
    except Exception as e:
        return False, str(e)


def _free_port() -> int:
    """Asks the OS for an unused TCP port (bind to port 0, read back what
    it actually chose, release it immediately) rather than assuming one is
    free. Exists specifically for relaunch_app() below - see its docstring
    for why reusing the outgoing process's own port is the wrong thing to
    do here. A tiny residual race exists between this releasing the port
    and the relaunched process's own bind moments later (same as
    desktop.py's _wait_for_port_free, which is kept as a backstop for
    exactly that and for the normal, non-relaunch launch path), but
    picking a fresh port instead of deliberately colliding with one the
    outgoing process might still be holding removes the actual race this
    was hitting, rather than just narrowing its window.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def relaunch_app() -> None:
    """Starts a fresh, fully detached `thirtytutors` process. Detached (own
    process group/session on POSIX, DETACHED_PROCESS on Windows) so the
    new process survives this one exiting - otherwise it'd be a child of a
    process that's about to disappear, which on some platforms tears the
    child down too.

    On Windows, DETACHED_PROCESS means the new process gets NO console at
    all - if anything during its startup touches stdout/stderr before it's
    fully running (rich's Console() probes the terminal, click does too,
    any stray print()), that raises OSError: [WinError 6] The handle is
    invalid and the process dies before uvicorn ever binds the port -
    invisibly, since there's no console to show that error on. Redirecting stdout/stderr to a real log file
    gives every stream a valid handle to write to, fixing that crash, and
    leaves a trail to diagnose anything that still goes wrong on startup.

    That log file is also why PYTHONUTF8=1 is forced below: a Python
    stdout backed by a redirected file (rather than a real console) still
    defaults to the OS locale's codepage on Windows (cp1252, typically),
    not UTF-8 - and rich's own ✓/→ characters (cli.py's setup/update-notice
    output) immediately raise UnicodeEncodeError against that codepage,
    reproducing the exact same invisible-crash symptom one layer further
    in. PEP 540 UTF-8 mode makes the child's text I/O UTF-8 unconditionally
    regardless of locale, independent of what stream it's writing to.

    Also why this passes an explicit --port instead of letting the new
    process default to 8000: this function runs and spawns the new process
    WHILE the outgoing process (and its own server, still bound to 8000)
    is still fully alive - close_this_window() below, which is what
    actually lets the outgoing process exit, is only called by
    /api/restart-app AFTER this returns, and even a well-ordered close is
    not instant. The new process trying to bind 8000 immediately raced the
    outgoing one giving it up - uvicorn itself
    swallows that bind failure silently (logs it, then returns normally
    rather than raising) and desktop.py's window still opened regardless,
    pointed at a port nothing was listening on, relaunch.log showed "WinError 10048: only one usage of
    each socket address..."). A fresh OS-assigned port sidesteps the race
    entirely instead of narrowing it - nothing else in the app assumes
    port 8000 specifically, since the window URL and every frontend
    fetch() call are relative to whatever origin actually got used (see
    desktop.py/update.js).

    Does NOT close this process's own window or exit this process - see
    close_this_window() below, called separately by the /api/restart-app
    handler right after this.
    """
    creationflags = 0
    start_new_session = False
    log_file = None
    env = dict(os.environ, PYTHONUTF8="1")
    if sys.platform == "win32":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        log_file = open(DATA_DIR / "relaunch.log", "a", encoding="utf-8")
        # relaunch.log is append-only (a relaunch failing is exactly the
        # case where truncating it would be most likely to destroy the one
        # copy of evidence explaining why) - a dated separator line per
        # attempt is what actually makes an ever-growing file readable,
        # since uvicorn/rich's own log lines don't carry timestamps.
        # Flushed explicitly, and written before Popen() below rather than
        # after, so it can't land interleaved with or after the child's own
        # output to the same file.
        log_file.write(f"\n=== relaunch attempt: {datetime.now().isoformat(timespec='seconds')} ===\n")
        log_file.flush()
    else:
        start_new_session = True
    try:
        subprocess.Popen(
            [sys.executable, "-m", "thirtytutors", "--port", str(_free_port())],
            creationflags=creationflags,
            start_new_session=start_new_session,
            stdout=log_file,
            stderr=log_file,
            env=env,
            close_fds=True,
        )
    finally:
        # The child gets its own duplicated handle when passed directly to
        # Popen - closing the parent's copy here doesn't affect the child,
        # it just stops this (about to exit anyway) process holding it open.
        if log_file is not None:
            log_file.close()


def close_this_window() -> bool:
    """Closes THIS process's own pywebview window, which is what actually
    lets it exit: desktop.py's run() blocks on webview.start() until the
    window closes, then runs its own graceful server shutdown (see
    desktop.py's _shut_down_server_gracefully) before finally returning -
    so the process does still exit shortly after this call, just not
    instantly. webview.windows[0].destroy() is safe to call from a
    background thread (this runs from inside a FastAPI request handler,
    not the pywebview GUI thread) - pywebview dispatches it internally.

    Returns False (does nothing) if there's no window to close - e.g. if
    this module ever gets imported somewhere webview was never started
    (tests, or a hypothetical non-desktop server mode).
    """
    try:
        import webview

        if webview.windows:
            webview.windows[0].destroy()
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Assets (GitHub Release)
# ---------------------------------------------------------------------------

# Generic across both asset groups (core avatar/voice/photo AND landing-
# page marketing) rather than two near-identical copies of the same
# download/extract/progress-bar logic - the two groups only ever differ in
# WHICH files/tag/version/marker/label they use, never in HOW the download
# itself works, so that's the only thing parametrized here.


def _assets_marker_path() -> Path:
    return ASSETS_DIR / ".assets_version"


def _marketing_assets_marker_path() -> Path:
    return ASSETS_DIR / "marketing" / ".marketing_assets_version"


def get_local_assets_version() -> str | None:
    marker = _assets_marker_path()
    if not marker.is_file():
        return None
    return marker.read_text(encoding="utf-8").strip()


def get_local_marketing_assets_version() -> str | None:
    marker = _marketing_assets_marker_path()
    if not marker.is_file():
        return None
    return marker.read_text(encoding="utf-8").strip()


def check_assets_update() -> dict:
    """Compares against ASSETS_VERSION from the CURRENTLY INSTALLED code,
    not a remote check - there's no registry of asset-release versions to
    query the way PyPI is queried for the app version. This only catches
    "my assets are stale relative to the code I have"; if the *code* is
    also outdated, a newer ASSETS_VERSION won't be known until after that
    code update - which is exactly why cli.py's cmd_run re-checks this on
    every launch (see that module) rather than only once at setup time.
    """
    current = get_local_assets_version()
    return {
        "current": current,
        "latest": ASSETS_VERSION,
        "update_available": current != ASSETS_VERSION,
    }


def check_marketing_assets_update() -> dict:
    """Same idea as check_assets_update(), independent marker/version -
    see MARKETING_ASSETS_VERSION in constants.py for why."""
    current = get_local_marketing_assets_version()
    return {
        "current": current,
        "latest": MARKETING_ASSETS_VERSION,
        "update_available": current != MARKETING_ASSETS_VERSION,
    }


# Polled by GET /api/download-progress (routes_api.py) while a download is
# in flight, so the web UI can show a real, live-updating progress bar
# instead of a static "Downloading..." string for however long a large
# asset zip takes. Only ever meaningful during a download that was
# triggered from the RUNNING APP (Settings > Updates / the bell's "Update &
# Relaunch") - `thirtytutors setup` from a terminal already has rich's own
# progress bar for that (see below), and there's no web UI polling it in
# that context anyway. A plain module-level dict, not anything lock-
# guarded: only one download ever runs at a time in this app's actual
# usage (the frontend always awaits one step before starting the next -
# see update.js), and simple dict assignment is already atomic enough
# under the GIL for a polling reader that just wants the latest snapshot,
# not a precise value from any single instant.
_download_progress = {"filename": None, "downloaded": 0, "total": None}


def get_download_progress() -> dict:
    return dict(_download_progress)


def _reset_download_progress(filename: str, total: int | None) -> None:
    _download_progress["filename"] = filename
    _download_progress["downloaded"] = 0
    _download_progress["total"] = total


def _clear_download_progress() -> None:
    _download_progress["filename"] = None
    _download_progress["downloaded"] = 0
    _download_progress["total"] = None


def _download_with_progress(url: str, dest: Path, release_tag: str, console=None) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "thirtytutors-setup"})
    try:
        from rich.progress import (
            BarColumn,
            DownloadColumn,
            Progress,
            TimeRemainingColumn,
            TransferSpeedColumn,
        )

        with urllib.request.urlopen(req) as resp:
            total = int(resp.headers.get("Content-Length", 0)) or None
            _reset_download_progress(dest.name, total)
            with Progress(
                "[progress.description]{task.description}",
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=console,
            ) as progress:
                task = progress.add_task(dest.name, total=total)
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(1024 * 256)
                        if not chunk:
                            break
                        f.write(chunk)
                        progress.update(task, advance=len(chunk))
                        _download_progress["downloaded"] += len(chunk)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError(
                f"Could not download {dest.name} (404 Not Found) from:\n  {url}\n"
                f"The '{release_tag}' GitHub Release probably hasn't been published yet "
                "(or doesn't have this file attached)."
            ) from e
        raise
    except ImportError:
        if console:
            console.print(f"Downloading {dest.name} (no progress bar available)...")
        with urllib.request.urlopen(req) as resp:
            total = int(resp.headers.get("Content-Length", 0)) or None
            _reset_download_progress(dest.name, total)
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
                    _download_progress["downloaded"] += len(chunk)
    finally:
        _clear_download_progress()


def _download_and_extract_assets(
    asset_files: dict[str, str],
    release_tag: str,
    version: str,
    update_check: dict,
    marker_path: Path,
    label: str,
    force: bool,
    console=None,
) -> None:
    if not force and not update_check["update_available"]:
        if console:
            console.print(f"[dim]{label} already up to date - skipping download.[/dim]")
        return

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="thirtytutors-assets-") as tmp:
        tmp_path = Path(tmp)
        for kind, filename in asset_files.items():
            url = f"https://github.com/{GITHUB_REPO}/releases/download/{release_tag}/{filename}"
            zip_path = tmp_path / filename
            if console:
                console.print(f"Downloading {filename} from {url} ...")
            _download_with_progress(url, zip_path, release_tag, console)

            dest_dir = ASSETS_DIR / kind
            dest_dir.mkdir(parents=True, exist_ok=True)
            if console:
                console.print(f"Extracting {filename} ...")
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(dest_dir)

    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(version, encoding="utf-8")
    if console:
        console.print(f"[green]\u2713[/green] {label} ready.")


def download_and_extract_assets(force: bool = False, console=None) -> None:
    _download_and_extract_assets(
        ASSET_FILES,
        ASSETS_RELEASE_TAG,
        ASSETS_VERSION,
        check_assets_update(),
        _assets_marker_path(),
        "Avatar/voice/photo assets",
        force,
        console,
    )


def download_and_extract_marketing_assets(force: bool = False, console=None) -> None:
    """Landing-page video/gif assets - same shape as
    download_and_extract_assets above, raises on failure rather than
    swallowing it. Whether a failure here should actually block anything
    is a decision for the CALLER, not this function: cli.py's run_setup()
    wraps this call in its own try/except (a marketing-download failure
    shouldn't block a person from ever reaching the tutor over a page they
    may not even look at), but routes_api.py's interactive "Update
    Marketing Assets" endpoint deliberately does NOT swallow it - someone
    who explicitly clicked that button needs to see it failed, the same
    way /api/update-assets already surfaces a failure for the core
    bundle.
    """
    _download_and_extract_assets(
        MARKETING_ASSET_FILES,
        MARKETING_ASSETS_RELEASE_TAG,
        MARKETING_ASSETS_VERSION,
        check_marketing_assets_update(),
        _marketing_assets_marker_path(),
        "Marketing (landing-page) assets",
        force,
        console,
    )


# ---------------------------------------------------------------------------
# Release notes ("what's new")
# ---------------------------------------------------------------------------


def _whats_new_marker_path() -> Path:
    return DATA_DIR / ".last_seen_version"


def get_last_seen_version() -> str | None:
    marker = _whats_new_marker_path()
    if not marker.is_file():
        return None
    return marker.read_text(encoding="utf-8").strip()


def mark_version_seen(version: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _whats_new_marker_path().write_text(version, encoding="utf-8")


def check_whats_new() -> dict:
    """A brand new install (no marker yet) silently records the current
    version without reporting anything available - there's nothing "new"
    to announce about the version someone just installed for the first
    time. Only a version change relative to a previously recorded one
    counts as available.
    """
    current = _installed_app_version()
    last_seen = get_last_seen_version()
    if last_seen is None:
        mark_version_seen(current)
        return {"available": False, "version": current}
    return {"available": last_seen != current, "version": current}


def list_release_versions() -> list[str]:
    """Every version with a release-notes file on disk, newest first."""
    if not RELEASES_DIR.is_dir():
        return []
    versions = []
    for path in RELEASES_DIR.glob("v*_release.md"):
        version = path.name[len("v") : -len("_release.md")]
        versions.append(version)
    versions.sort(key=_version_tuple, reverse=True)
    return versions


def get_release_notes_markdown(version: str) -> str | None:
    path = RELEASES_DIR / f"v{version}_release.md"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def render_release_notes_html(version: str) -> str | None:
    raw = get_release_notes_markdown(version)
    if raw is None:
        return None
    import markdown

    return markdown.markdown(raw, extensions=["extra"])
