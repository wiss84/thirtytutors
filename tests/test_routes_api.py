"""Integration tests for routes_api.py's REST endpoints, run against a
minimal FastAPI app built from just routes_api.router - not the full
backend.main:app, which also mounts StaticFiles(directory="static") and
would fail to import unless run with the real project root as cwd. This is
the right scope for these tests regardless: they're exercising the API
layer, not the page/static-file serving.
"""

import base64
import struct

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from thirtytutors import memory, routes_api

pytestmark = pytest.mark.integration


@pytest.fixture
def client(isolated_data_dir):
    app = FastAPI()
    app.include_router(routes_api.router)
    return TestClient(app)


def _pcm_bytes(n_samples: int, amplitude: int = 8000) -> str:
    """A short constant-tone PCM16 clip, base64-encoded - good enough for
    the voice-enrollment endpoints, which don't care about actual speech
    content once resemblyzer_backend.embed is mocked (see
    fake_speaker_backend in conftest.py)."""
    samples = [amplitude] * n_samples
    return base64.b64encode(struct.pack(f"<{n_samples}h", *samples)).decode()


# --- Reference data endpoints ---


def test_reference_data_endpoints_return_expected_shape(client):
    assert "voices" in client.get("/api/voices").json()
    assert "models" in client.get("/api/models").json()
    assert "scenarios" in client.get("/api/scenarios").json()
    assert "languages" in client.get("/api/languages").json()

    app_info = client.get("/api/app-info").json()
    assert "version" in app_info
    assert isinstance(app_info["credits"], list)


def test_languages_endpoint_returns_a_sorted_list_of_unique_names(client):
    languages = client.get("/api/languages").json()["languages"]
    assert len(languages) > 20  # a genuinely useful list, not a stub
    assert all(isinstance(name, str) and name for name in languages)
    assert len(languages) == len(set(languages))  # no duplicates
    assert languages == sorted(languages)


# --- Profiles CRUD ---


def test_create_profile_requires_a_name(client):
    resp = client.post("/api/profiles", json={"name": "  "})
    assert resp.status_code == 400


def test_create_profile_returns_full_profile_with_defaults(client):
    resp = client.post("/api/profiles", json={"name": "Wissam"})
    assert resp.status_code == 200
    profile = resp.json()
    assert profile["name"] == "Wissam"
    assert profile["default_difficulty"]
    assert profile["mic_calibrations"] == {}
    assert profile["active_conversation_id"] is None


def test_list_profiles_returns_id_and_name_only(client):
    client.post("/api/profiles", json={"name": "Wissam"})
    resp = client.get("/api/profiles")
    assert resp.status_code == 200
    profiles = resp.json()["profiles"]
    assert len(profiles) == 1
    assert set(profiles[0].keys()) == {"id", "name"}


def test_get_profile_404_for_unknown_id(client):
    assert client.get("/api/profiles/does-not-exist").status_code == 404


def test_update_profile_only_touches_editable_fields(client):
    profile = client.post("/api/profiles", json={"name": "Wissam"}).json()
    resp = client.put(
        f"/api/profiles/{profile['id']}",
        json={
            "native_language": "French",
            "this_field_does_not_exist": "should be silently ignored",
        },
    )
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["native_language"] == "French"
    assert "this_field_does_not_exist" not in updated


def test_update_profile_404_for_unknown_id(client):
    resp = client.put("/api/profiles/does-not-exist", json={"name": "x"})
    assert resp.status_code == 404


def test_delete_profile_cascades_conversations_and_enrollment(client, fake_speaker_backend):
    profile = client.post("/api/profiles", json={"name": "Wissam"}).json()
    conv = client.post(
        f"/api/profiles/{profile['id']}/conversations",
        json={"target_language": "Spanish"},
    ).json()

    sample = _pcm_bytes(320 * 20)
    enroll_resp = client.post(
        f"/api/profiles/{profile['id']}/voice-enrollment",
        json={"mic_key": "mic-a", "samples": [sample]},
    )
    assert enroll_resp.status_code == 200

    resp = client.delete(f"/api/profiles/{profile['id']}")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True}

    assert client.get(f"/api/profiles/{profile['id']}").status_code == 404
    assert memory.get_conversation(conv["id"]) is None
    assert (
        client.get(
            f"/api/profiles/{profile['id']}/voice-enrollment",
            params={"mic_key": "mic-a"},
        ).status_code
        == 404
    )


def test_delete_profile_404_for_unknown_id(client):
    assert client.delete("/api/profiles/does-not-exist").status_code == 404


# --- Mic status ---


def test_mic_status_reflects_calibration_and_enrollment(client, fake_speaker_backend):
    profile = client.post("/api/profiles", json={"name": "Wissam"}).json()
    client.put(
        f"/api/profiles/{profile['id']}",
        json={
            "mic_calibrations": {"mic-a": {"calibrated": True, "tested": False}},
        },
    )

    resp = client.get(f"/api/profiles/{profile['id']}/mic-status")
    assert resp.status_code == 200
    mics = resp.json()["mics"]
    assert len(mics) == 1
    assert mics[0]["mic_key"] == "mic-a"
    assert mics[0]["calibrated"] is True
    assert mics[0]["tested"] is False
    assert mics[0]["enrolled"] is False  # not enrolled yet

    client.post(
        f"/api/profiles/{profile['id']}/voice-enrollment",
        json={"mic_key": "mic-a", "samples": [_pcm_bytes(320 * 20)]},
    )
    mics = client.get(f"/api/profiles/{profile['id']}/mic-status").json()["mics"]
    assert mics[0]["enrolled"] is True


# --- Voice enrollment ---


def test_voice_enrollment_full_round_trip(client, fake_speaker_backend):
    profile = client.post("/api/profiles", json={"name": "Wissam"}).json()

    assert client.get(f"/api/profiles/{profile['id']}/voice-enrollment", params={"mic_key": "mic-a"}).json() == {
        "enrolled": False
    }

    resp = client.post(
        f"/api/profiles/{profile['id']}/voice-enrollment",
        json={
            "mic_key": "mic-a",
            "samples": [_pcm_bytes(320 * 20)],
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"enrolled": True}
    assert client.get(f"/api/profiles/{profile['id']}/voice-enrollment", params={"mic_key": "mic-a"}).json() == {"enrolled": True}

    del_resp = client.delete(f"/api/profiles/{profile['id']}/voice-enrollment", params={"mic_key": "mic-a"})
    assert del_resp.status_code == 200
    assert client.get(f"/api/profiles/{profile['id']}/voice-enrollment", params={"mic_key": "mic-a"}).json() == {
        "enrolled": False
    }


def test_voice_enrollment_requires_at_least_one_sample(client, fake_speaker_backend):
    profile = client.post("/api/profiles", json={"name": "Wissam"}).json()
    resp = client.post(
        f"/api/profiles/{profile['id']}/voice-enrollment",
        json={"mic_key": "mic-a", "samples": []},
    )
    assert resp.status_code == 400


def test_voice_enrollment_test_requires_prior_enrollment(client, fake_speaker_backend):
    profile = client.post("/api/profiles", json={"name": "Wissam"}).json()
    resp = client.post(
        f"/api/profiles/{profile['id']}/voice-enrollment-test",
        json={
            "mic_key": "mic-a",
            "sample": _pcm_bytes(320 * 5),
        },
    )
    assert resp.status_code == 400


def test_voice_enrollment_test_scores_against_reference(client, fake_speaker_backend):
    profile = client.post("/api/profiles", json={"name": "Wissam"}).json()
    sample = _pcm_bytes(320 * 20)
    client.post(
        f"/api/profiles/{profile['id']}/voice-enrollment",
        json={"mic_key": "mic-a", "samples": [sample]},
    )

    resp = client.post(
        f"/api/profiles/{profile['id']}/voice-enrollment-test",
        json={"mic_key": "mic-a", "sample": sample},
    )
    assert resp.status_code == 200
    # Same clip enrolled and tested -> identical fake embedding -> perfect score.
    assert resp.json()["score"] == pytest.approx(1.0)


# --- Conversations CRUD ---


def test_create_conversation_falls_back_to_profile_defaults(client):
    profile = client.post("/api/profiles", json={"name": "Wissam"}).json()
    client.put(f"/api/profiles/{profile['id']}", json={"default_difficulty": "advanced"})

    conv = client.post(
        f"/api/profiles/{profile['id']}/conversations",
        json={"target_language": "Spanish"},
    ).json()
    assert conv["config"]["difficulty"] == "advanced"
    assert conv["config"]["target_language"] == "Spanish"

    profile_after = client.get(f"/api/profiles/{profile['id']}").json()
    assert profile_after["active_conversation_id"] == conv["id"]


def test_create_conversation_404_for_unknown_profile(client):
    resp = client.post(
        "/api/profiles/does-not-exist/conversations",
        json={"target_language": "Spanish"},
    )
    assert resp.status_code == 404


def test_list_conversations_includes_active_conversation_id(client):
    profile = client.post("/api/profiles", json={"name": "Wissam"}).json()
    conv = client.post(
        f"/api/profiles/{profile['id']}/conversations",
        json={"target_language": "Spanish"},
    ).json()

    resp = client.get(f"/api/profiles/{profile['id']}/conversations")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["conversations"]) == 1
    assert body["active_conversation_id"] == conv["id"]


def test_get_conversation_includes_turns_summary_and_tutor_name(client):
    profile = client.post("/api/profiles", json={"name": "Wissam"}).json()
    conv = client.post(
        f"/api/profiles/{profile['id']}/conversations",
        json={"target_language": "Spanish", "voice_name": "Kore"},
    ).json()
    memory.insert_turn(conv["id"], "user", "hola")

    resp = client.get(f"/api/profiles/{profile['id']}/conversations/{conv['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["turns"]) == 1
    assert body["summary"] is None
    assert body["tutor_name"]  # resolved from voices data, non-empty


def test_get_conversation_404_when_profile_mismatched(client):
    profile_a = client.post("/api/profiles", json={"name": "A"}).json()
    profile_b = client.post("/api/profiles", json={"name": "B"}).json()
    conv = client.post(
        f"/api/profiles/{profile_a['id']}/conversations",
        json={"target_language": "Spanish"},
    ).json()

    resp = client.get(f"/api/profiles/{profile_b['id']}/conversations/{conv['id']}")
    assert resp.status_code == 404


def test_update_conversation_difficulty(client):
    """Regression test for issues.md #6 - the Settings modal's Learning tab
    difficulty toggle patches the ACTIVE conversation's own difficulty,
    not just the profile's future default."""
    profile = client.post("/api/profiles", json={"name": "Wissam"}).json()
    conv = client.post(
        f"/api/profiles/{profile['id']}/conversations",
        json={"target_language": "Spanish", "difficulty": "beginner"},
    ).json()

    resp = client.put(
        f"/api/profiles/{profile['id']}/conversations/{conv['id']}",
        json={"difficulty": "advanced"},
    )
    assert resp.status_code == 200
    assert resp.json()["config"]["difficulty"] == "advanced"

    reloaded = memory.get_conversation(conv["id"])
    assert reloaded["config"]["difficulty"] == "advanced"
    # Untouched fields survive the partial update:
    assert reloaded["config"]["target_language"] == "Spanish"


def test_delete_conversation_reassigns_active_conversation(client):
    profile = client.post("/api/profiles", json={"name": "Wissam"}).json()
    conv1 = client.post(
        f"/api/profiles/{profile['id']}/conversations",
        json={"target_language": "Spanish"},
    ).json()
    conv2 = client.post(
        f"/api/profiles/{profile['id']}/conversations",
        json={"target_language": "French"},
    ).json()
    # conv2 is now active (most recently created)

    client.delete(f"/api/profiles/{profile['id']}/conversations/{conv2['id']}")
    profile_after = client.get(f"/api/profiles/{profile['id']}").json()
    assert profile_after["active_conversation_id"] == conv1["id"]


def test_delete_last_conversation_clears_active_conversation(client):
    profile = client.post("/api/profiles", json={"name": "Wissam"}).json()
    conv = client.post(
        f"/api/profiles/{profile['id']}/conversations",
        json={"target_language": "Spanish"},
    ).json()

    client.delete(f"/api/profiles/{profile['id']}/conversations/{conv['id']}")
    profile_after = client.get(f"/api/profiles/{profile['id']}").json()
    assert profile_after["active_conversation_id"] is None


def test_set_active_conversation_rejects_mismatched_profile(client):
    profile_a = client.post("/api/profiles", json={"name": "A"}).json()
    profile_b = client.post("/api/profiles", json={"name": "B"}).json()
    conv = client.post(
        f"/api/profiles/{profile_a['id']}/conversations",
        json={"target_language": "Spanish"},
    ).json()

    resp = client.put(
        f"/api/profiles/{profile_b['id']}/active-conversation",
        json={"conversation_id": conv["id"]},
    )
    assert resp.status_code == 404


# --- Notes + export ---


def test_notes_endpoint_reflects_vocab_and_lesson_log(client):
    profile = client.post("/api/profiles", json={"name": "Wissam"}).json()
    conv = client.post(
        f"/api/profiles/{profile['id']}/conversations",
        json={"target_language": "Spanish"},
    ).json()

    resp = client.get(f"/api/profiles/{profile['id']}/conversations/{conv['id']}/notes")
    assert resp.json() == {"vocab_mistakes": [], "lesson_log": []}

    memory.upsert_vocab_mistake(conv["id"], "ser vs estar")
    memory.append_lesson_log(conv["id"], "covered greetings")

    resp = client.get(f"/api/profiles/{profile['id']}/conversations/{conv['id']}/notes")
    body = resp.json()
    assert len(body["vocab_mistakes"]) == 1
    assert len(body["lesson_log"]) == 1


def test_export_docx_returns_a_word_document(client):
    profile = client.post("/api/profiles", json={"name": "Wissam"}).json()
    conv = client.post(
        f"/api/profiles/{profile['id']}/conversations",
        json={"target_language": "Spanish"},
    ).json()
    memory.upsert_vocab_mistake(conv["id"], "ser vs estar")

    resp = client.get(f"/api/profiles/{profile['id']}/conversations/{conv['id']}/notes/export.docx")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert "attachment" in resp.headers["content-disposition"]
    assert len(resp.content) > 0


def test_export_docx_404_for_unknown_conversation(client):
    profile = client.post("/api/profiles", json={"name": "Wissam"}).json()
    resp = client.get(f"/api/profiles/{profile['id']}/conversations/does-not-exist/notes/export.docx")
    assert resp.status_code == 404
