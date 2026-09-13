"""
Desktop wrapper - runs the FastAPI server in a background thread and opens
it in a native app window via pywebview, instead of a browser tab. Called
from cli.py's `thirtytutors` command (see run() below); the __main__ block
exists only for running this file directly against a live source checkout
during development (`python -m thirtytutors.desktop`).

Shutdown: the server thread is a daemon thread (see run()), which by
itself means it has no coordinated shutdown at all - the instant the main
thread has nothing left to execute (webview.start() returning once the
window closes), Python begins interpreter finalization regardless of
whatever that thread's own in-flight work still is, and that finalization
specifically disables machinery asyncio.to_thread depends on. In practice
that surfaced as two different-looking bugs that were really the same root
cause: profiles.json occasionally left truncated (fixed separately, at the
data layer, by making profiles_store.py's writes atomic - see that
module), and live_session.py's ws_session `finally` block occasionally
failing with "cannot schedule new futures after shutdown" when it tried to
run a background summarization call. _shut_down_server_gracefully below is
what actually fixes the root cause: it makes run() explicitly wait (with a
bounded timeout) for the server thread's in-flight work to finish before
the process is allowed to exit, instead of racing an unpredictable
interpreter teardown.

Notes:
- private_mode=False keeps the WebView2 profile (cookies, permissions like
  microphone access) persistent across app launches. pywebview defaults to
  an ephemeral profile.
- Window size is set generously wide, since the sidebar + main layout needs
  real horizontal room - it's resizable, so this is just a sane default.
- icon is a webview.start()-level setting (applies to the app/window icon,
  not per-window) - support varies a bit by platform/pywebview version, so
  if it doesn't show up in the title bar/taskbar, that's a version quirk to
  look into rather than a sign something else is broken.
"""

import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn
import webview

from .main import app  # reuse the exact same FastAPI app defined in main.py

# pywebview's icon= param wants a .ico on Windows and a .icns on macOS (per
# its own docs) - a bare .ico silently does nothing on Mac rather than
# erroring, which would otherwise look like a mysterious missing icon
# rather than the platform mismatch it actually is.
_ICON_NAME = "ThirtyTutors.icns" if sys.platform == "darwin" else "ThirtyTutors.ico"
ICON_PATH = Path(__file__).parent / "static" / _ICON_NAME

# Passed to uvicorn as timeout_graceful_shutdown - the max time IT will
# spend waiting for in-flight connections (chiefly the /ws/session
# websocket for whatever Live conversation was open) to finish on their
# own once should_exit is set, before forcibly tearing them down and
# proceeding to the ASGI lifespan shutdown event. Generous enough to give
# live_session.py's ws_session `finally` block (a couple of fast local
# SQLite writes, plus a best-effort Gemini call for the final summary
# fold - see that module) a genuine chance to complete normally, not just
# the bare minimum.
SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_S = 45.0

# Upper bound on how long _shut_down_server_gracefully will block run()
# waiting for the server thread to actually finish, before giving up and
# letting the process exit anyway. Deliberately larger than
# SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_S above, which only bounds ONE piece of
# the shutdown sequence (uvicorn's own "wait for connections to close"
# loop) - this is the true worst-case ceiling on the whole sequence,
# including the ASGI lifespan shutdown event and server.run()'s own
# asyncio teardown that run after that loop, so closing the window can
# never hang the app indefinitely even if something else goes wrong.
SERVER_JOIN_TIMEOUT_S = 60.0


def _wait_for_port_free(host: str, port: int, timeout: float = 10.0) -> bool:
    """Polls whether (host, port) can actually be bound, returning True as
    soon as it can (immediately, on a normal launch where nothing else is
    using it). Exists specifically for the Update & Relaunch race: the new
    process can start trying to bind before the OLD process - still mid-
    shutdown via close_this_window() - has released the port ; relaunch.log showed "WinError 10048: only one
    usage of each socket address..."). uvicorn itself swallows a bind
    failure silently - logs it, then returns normally rather than raising
    (see Server.startup() catching the OSError) - so run() below had no
    way to notice anything went wrong and opened the window regardless.
    Waiting for the port to be genuinely free before ever starting uvicorn
    sidesteps that entirely, rather than trying to detect/recover from a
    failure uvicorn already hid.

    Deliberately does NOT set SO_REUSEADDR on the probe socket - on
    Windows that can let a bind "succeed" while another socket is still
    actively listening on the same address, which is exactly the false
    positive this needs to avoid.
    """
    deadline = time.monotonic() + timeout
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, port))
                return True
            except OSError:
                pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.3)


def _build_uvicorn_server(host: str, port: int) -> uvicorn.Server:
    """Builds the Server object run() uses, instead of the uvicorn.run()
    convenience function - only a Server object (not uvicorn.run's fire-
    and-forget wrapper) exposes should_exit, which is what lets
    _shut_down_server_gracefully below trigger a graceful shutdown from a
    different thread than the one running server.run(). Safe to run
    server.run() itself off the main thread (uvicorn's own
    install_signal_handlers() checks threading.current_thread() and
    silently skips registering OS signal handlers - which can only ever be
    done from the main thread - rather than erroring, when it isn't) - not
    relevant here anyway, since should_exit is set directly rather than via
    a signal.

    timeout_graceful_shutdown is passed inside a try/except since it's a
    newer Config field that may not exist on an older installed uvicorn
    (same defensive pattern as live_session.py's build_config uses for
    newer google-genai SDK fields) - without it, uvicorn just waits
    indefinitely for connections to close on its own instead of forcing
    them after SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_S, but
    _shut_down_server_gracefully's own outer join(timeout=...) still bounds
    the wait regardless.
    """
    try:
        config = uvicorn.Config(
            app, host=host, port=port, log_level="info", timeout_graceful_shutdown=SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_S
        )
    except TypeError:
        print("[desktop] installed uvicorn doesn't support timeout_graceful_shutdown - continuing without it.")
        config = uvicorn.Config(app, host=host, port=port, log_level="info")
    return uvicorn.Server(config)


def _shut_down_server_gracefully(server: uvicorn.Server, server_thread: threading.Thread) -> None:
    """Called from run() once the pywebview window has actually closed -
    signals uvicorn to shut down gracefully (stop accepting new
    connections, then wait up to SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_S for
    existing ones to finish on their own - see _build_uvicorn_server) and
    blocks until the server thread is done, or SERVER_JOIN_TIMEOUT_S
    elapses, whichever comes first.

    This is the actual fix, not the timeout values themselves - see this
    module's own docstring for the full reasoning. Blocking HERE, in the
    main thread, after the window is already gone but before run() itself
    returns, is what gives the server thread's in-flight work (chiefly
    live_session.py's ws_session `finally` block) a genuine, safe window to
    run to completion instead of racing an unpredictable interpreter
    teardown the moment run() has nothing left to execute.

    Best-effort past SERVER_JOIN_TIMEOUT_S: if the thread still hasn't
    finished by then (a stuck task, a hung network call outliving even
    uvicorn's own internal timeout), this gives up and lets the process
    exit anyway rather than making the app impossible to close.
    """
    server.should_exit = True
    server_thread.join(timeout=SERVER_JOIN_TIMEOUT_S)
    if server_thread.is_alive():
        print(f"[desktop] server thread still running after {SERVER_JOIN_TIMEOUT_S:.0f}s - exiting anyway.")


def run(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Starts the FastAPI server in a background thread, then opens the
    desktop window pointed at it. Blocks until the window is closed
    (webview.start() is blocking) - this is the actual entry point cli.py's
    `thirtytutors` command calls after the first-run bootstrap (if any) has
    already completed. Also blocks a bit LONGER than that, past
    webview.start() returning - see _shut_down_server_gracefully.
    """
    webview.settings["ALLOW_DOWNLOADS"] = True  # off by default in pywebview - without this, downloads silently do nothing

    if not _wait_for_port_free(host, port):
        print(f"[desktop] {host}:{port} still in use after waiting - starting anyway, uvicorn will report the real error")

    server = _build_uvicorn_server(host, port)
    # daemon=True as a safety net only (so a bug in the shutdown
    # coordination below still can't make the process un-killable) - the
    # actual coordination is the explicit should_exit + join() in
    # _shut_down_server_gracefully, not this flag; see this module's own
    # docstring for why relying on daemon semantics alone was the bug.
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    # Give uvicorn a moment to bind before pointing the window at it.
    time.sleep(1.5)

    # Window size is set generously wide, since the sidebar + main layout
    webview.create_window(
        "ThirtyTutors",
        # A cache-busting query string forces a fresh fetch on every launch,
        # regardless of anything already sitting in the persistent WebView2
        # profile's cache (see the private_mode=False note above).
        #
        # Points at /landing homepage on every launch - the last-active profile stays "logged
        # in" regardless (see localStorage.tutorProfileId, read by
        # profileMenu.js's top-bar button on every page)
        f"http://{host}:{port}/landing?v={int(time.time())}",
        width=1360,
        height=860,
        min_size=(1100, 700),
        resizable=True,
    )
    webview.start(
        private_mode=False,
        icon=str(ICON_PATH) if ICON_PATH.exists() else None,
        debug=False,  # Turn on for debugging.
    )

    # The window is fully closed at this point - either the user closed it,
    # or updater.close_this_window() destroyed it programmatically as part
    # of Update & Relaunch (both go through the identical webview.start()
    # return path). See _shut_down_server_gracefully's own docstring for
    # why this call, not just letting run() return immediately, is what
    # actually matters here.
    _shut_down_server_gracefully(server, server_thread)


if __name__ == "__main__":
    run()
