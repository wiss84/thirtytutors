"""Tests for desktop.py's run() and its shutdown coordination - not the
actual GUI window (opening a real pywebview window isn't practical in an
automated test) or a real running server, but the settings/call sequence
around them, and the should_exit + join logic in
_shut_down_server_gracefully that fixes the daemon-thread shutdown race
described in this module's own docstring (that race is what caused the
intermittent profiles.json truncation and "cannot schedule new futures
after shutdown" summarization failures) - exactly the kind of thing that
can silently regress without anyone noticing until a manual close-the-app
test.
"""

import pytest

from thirtytutors import desktop

pytestmark = pytest.mark.unit


class _FakeServer:
    """Stand-in for uvicorn.Server - just enough surface (should_exit, a
    no-op run()) for these tests, without ever binding a real socket.
    """

    def __init__(self):
        self.should_exit = False

    def run(self):
        pass


class _FakeThread:
    """Stand-in for threading.Thread - records what it was constructed
    with and what start()/join() were called with, without ever spawning a
    real OS thread.
    """

    def __init__(self, target=None, daemon=None):
        self.target = target
        self.daemon = daemon
        self.started = False
        self.joined_timeout = None
        self._alive = False

    def start(self):
        self.started = True

    def join(self, timeout=None):
        self.joined_timeout = timeout

    def is_alive(self):
        return self._alive


# --- run() call sequence ---


def test_run_enables_downloads_opens_a_window_and_shuts_down_gracefully(monkeypatch):
    calls = []
    fake_server = _FakeServer()
    fake_thread = _FakeThread()

    monkeypatch.setattr(desktop, "_build_uvicorn_server", lambda host, port: fake_server)
    monkeypatch.setattr(desktop.threading, "Thread", lambda **kw: (calls.append(("Thread", kw)), fake_thread)[1])
    monkeypatch.setattr(desktop.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(desktop.webview, "create_window", lambda *a, **k: calls.append(("create_window", a, k)))
    monkeypatch.setattr(desktop.webview, "start", lambda *a, **k: calls.append(("start", a, k)))
    monkeypatch.setattr(
        desktop, "_shut_down_server_gracefully", lambda server, thread: calls.append(("shutdown", server, thread))
    )
    desktop.webview.settings["ALLOW_DOWNLOADS"] = False

    desktop.run(host="127.0.0.1", port=8000)

    assert desktop.webview.settings["ALLOW_DOWNLOADS"] is True
    assert [c[0] for c in calls] == ["Thread", "create_window", "start", "shutdown"]
    assert fake_thread.started is True

    # The server thread runs server.run (not uvicorn.run directly), with
    # daemon=True as a safety net only - see _build_uvicorn_server/run's
    # own comments for why daemon=True alone was never the real fix.
    _, thread_kwargs = calls[0]
    assert thread_kwargs == {"target": fake_server.run, "daemon": True}

    _, args, _ = calls[1]  # create_window
    assert "127.0.0.1:8000" in args[1]

    # Graceful shutdown runs LAST - after webview.start() returns (the
    # window is already closed by then) - handed the exact same
    # server/thread pair the rest of run() used.
    assert calls[-1] == ("shutdown", fake_server, fake_thread)


# --- _shut_down_server_gracefully ---


def test_shut_down_server_gracefully_signals_should_exit_and_joins_with_bound():
    server = _FakeServer()
    thread = _FakeThread()

    desktop._shut_down_server_gracefully(server, thread)

    assert server.should_exit is True
    assert thread.joined_timeout == desktop.SERVER_JOIN_TIMEOUT_S


def test_shut_down_server_gracefully_gives_up_without_raising_if_thread_still_alive(capsys):
    server = _FakeServer()
    thread = _FakeThread()
    thread._alive = True  # simulates the join() timing out with work still in flight

    desktop._shut_down_server_gracefully(server, thread)  # must not raise

    assert "still running" in capsys.readouterr().out


# --- _build_uvicorn_server ---


def test_build_uvicorn_server_passes_graceful_timeout_when_supported():
    server = desktop._build_uvicorn_server("127.0.0.1", 8000)
    assert server.config.timeout_graceful_shutdown == desktop.SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_S


def test_build_uvicorn_server_falls_back_on_older_uvicorn_without_the_param(monkeypatch):
    """Simulates an installed uvicorn whose Config doesn't accept
    timeout_graceful_shutdown - _build_uvicorn_server must still return a
    working Server rather than crashing app startup over a newer-SDK-only
    field, same defensive posture as live_session.py's build_config.
    """
    real_config_cls = desktop.uvicorn.Config

    def _fake_config(*args, **kwargs):
        if "timeout_graceful_shutdown" in kwargs:
            raise TypeError("__init__() got an unexpected keyword argument 'timeout_graceful_shutdown'")
        return real_config_cls(*args, **kwargs)

    monkeypatch.setattr(desktop.uvicorn, "Config", _fake_config)

    server = desktop._build_uvicorn_server("127.0.0.1", 8000)
    assert isinstance(server, desktop.uvicorn.Server)
