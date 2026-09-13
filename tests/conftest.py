"""
conftest.py - shared pytest fixtures and isolation.

Available to every test module in this directory without importing.
"""

from __future__ import annotations

import numpy as np
import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "unit: fast, pure-logic tests (no I/O)")
    config.addinivalue_line(
        "markers",
        "integration: tests that use the filesystem, SQLite, or a mocked websocket session",
    )


# ---------------------------------------------------------------------------
# Data-directory isolation
# ---------------------------------------------------------------------------
# constants.py resolves DATA_DIR via platformdirs (the real, OS-managed
# ThirtyTutors data directory) - tests must never touch that. memory.py,
# quizzes.py, and profiles_store.py each did `from .constants import
# DATA_DIR` (and their own derived paths), which copies the *value* into
# their own module namespace at import time - patching constants.DATA_DIR
# alone would not affect memory.DATA_DIR/quizzes.DATA_DIR/
# profiles_store.DATA_DIR, so each is patched individually below. Autouse:
# every test gets a fresh, empty data dir with no action needed on its part.


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    from thirtytutors import constants, memory, profiles_store, quizzes
    from thirtytutors.speech_detection import enrollment

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    monkeypatch.setattr(constants, "DATA_DIR", data_dir)
    monkeypatch.setattr(memory, "DATA_DIR", data_dir)
    monkeypatch.setattr(memory, "DB_FILE", data_dir / "memory.db")
    monkeypatch.setattr(quizzes, "DATA_DIR", data_dir)
    monkeypatch.setattr(quizzes, "DB_FILE", data_dir / "memory.db")
    monkeypatch.setattr(profiles_store, "DATA_DIR", data_dir)
    monkeypatch.setattr(profiles_store, "PROFILES_FILE", data_dir / "profiles.json")
    monkeypatch.setattr(constants, "VOICE_ENROLLMENT_DIR", data_dir / "voice_enrollment")
    monkeypatch.setattr(enrollment, "VOICE_ENROLLMENT_DIR", data_dir / "voice_enrollment")

    memory.init_db()
    quizzes.init_db()
    yield data_dir


# ---------------------------------------------------------------------------
# Langfuse tracing isolation
# ---------------------------------------------------------------------------
# observability.py is fully opt-in based on LANGFUSE_PUBLIC_KEY/
# LANGFUSE_SECRET_KEY - without this fixture, a real key pair happening to
# be set in whatever shell runs the test suite (increasingly likely now
# that this project actually uses Langfuse for real) could make tests
# silently attempt real network calls. Autouse and unconditional: tests
# that want tracing "enabled" behavior monkeypatch observability.ENABLED
# directly instead (see test_observability.py) rather than relying on real
# env vars ever being present during a test run.


@pytest.fixture(autouse=True)
def isolated_observability(monkeypatch):
    from thirtytutors import observability

    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
    observability.set_profile_keys(None, None)
    monkeypatch.setattr(observability, "_client", None)
    monkeypatch.setattr(observability, "_broken", False)


# ---------------------------------------------------------------------------
# Speaker-verification backend seam
# ---------------------------------------------------------------------------
# resemblyzer_backend.get_model()/embed() are the ONLY things in
# speech_detection that touch the real (heavy: torch/resemblyzer) ML stack,
# and both are imported lazily inside functions rather than at module load
# time - so nothing in speech_detection needs those packages installed at
# all as long as this one seam is mocked. fake_embed is a small, cheap,
# fully deterministic stand-in: same audio content in -> same vector out,
# different content -> a different vector - enough to exercise the
# cosine-similarity gating logic meaningfully (identical audio scores
# ~1.0, different audio doesn't) without a real model.


def _fake_embed(audio: np.ndarray) -> np.ndarray:
    if len(audio) == 0:
        return np.zeros(4, dtype=np.float32)
    # Length is scaled down rather than used raw - cosine similarity only
    # cares about vector *direction*, and a raw sample count (thousands)
    # completely swamps the other features (all roughly within [-1, 1]),
    # making any two clips of similar length look artificially near-
    # identical regardless of their actual content. Bit an earlier version
    # of this fixture for real: a same-length-different-content comparison
    # that should have scored low came back above a 0.999 threshold purely
    # because both vectors were dominated by the same huge length term.
    return np.array(
        [
            float(np.mean(audio)),
            float(np.std(audio)),
            float(np.max(np.abs(audio))),
            float(len(audio)) / 10000.0,
        ],
        dtype=np.float32,
    )


@pytest.fixture
def fake_speaker_backend(monkeypatch):
    from thirtytutors.speech_detection import resemblyzer_backend

    monkeypatch.setattr(resemblyzer_backend, "embed", _fake_embed)
    monkeypatch.setattr(resemblyzer_backend, "get_model", lambda: object())
    return _fake_embed


# ---------------------------------------------------------------------------
# Profile / conversation helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def make_profile(isolated_data_dir):
    """Creates a profile directly via profiles_store (bypassing the HTTP
    layer - routes_api.py's own create-profile behavior is covered
    separately in test_routes_api.py) and returns it. Callers can override
    any field via kwargs.
    """
    from thirtytutors.profiles_store import load_profiles, save_profiles

    def _make(**overrides):
        import uuid

        profile = {
            "id": str(uuid.uuid4()),
            "name": "Test Profile",
            "api_key": None,
            "mic_device_id": None,
            "mic_label": None,
            "voice_name": "Kore",
            "voice_gender": "Female",
            "native_language": "English",
            "target_language": "Polish",
            "model_name": "gemini-3.1-flash-live-preview",
            "default_difficulty": "intermediate",
            "active_conversation_id": None,
            "mic_calibrations": {},
        }
        profile.update(overrides)
        profiles = load_profiles()
        profiles.append(profile)
        save_profiles(profiles)
        return profile

    return _make


@pytest.fixture
def make_conversation(isolated_data_dir):
    """Creates a conversation directly via memory.py. Callers can override
    any config field via kwargs (merged into a sensible default config).
    """
    from thirtytutors import memory

    def _make(profile_id: str, name: str | None = None, **config_overrides):
        config = {
            "voice_name": "Kore",
            "native_language": "English",
            "target_language": "Polish",
            "model_name": "gemini-3.1-flash-live-preview",
            "scenario": "free_learning",
            "difficulty": "intermediate",
        }
        config.update(config_overrides)
        return memory.create_conversation(profile_id, config, name=name)

    return _make
