"""Unit/integration tests for profiles_store.py - the profiles.json layer
and the Gemini client factory. Note: delete_profile here only removes the
profile row itself; cascading its conversations/voice-enrollment data is
routes_api.py's job (remove_profile endpoint) - covered in
test_routes_api.py, not here.
"""

import pytest

from thirtytutors import memory, profiles_store

pytestmark = pytest.mark.integration


def test_load_profiles_returns_empty_list_when_file_missing():
    assert profiles_store.load_profiles() == []


def test_save_then_load_round_trips():
    profiles = [{"id": "a", "name": "Alice"}, {"id": "b", "name": "Bob"}]
    profiles_store.save_profiles(profiles)
    assert profiles_store.load_profiles() == profiles


def test_load_profiles_returns_empty_list_on_corrupt_json(tmp_path):
    profiles_store.PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
    profiles_store.PROFILES_FILE.write_text("{not valid json", encoding="utf-8")
    assert profiles_store.load_profiles() == []


def test_get_profile_by_id_found_and_missing():
    profiles_store.save_profiles([{"id": "a", "name": "Alice"}])
    assert profiles_store.get_profile_by_id("a")["name"] == "Alice"
    assert profiles_store.get_profile_by_id("nope") is None


def test_patch_profile_merges_fields_and_persists():
    profiles_store.save_profiles([{"id": "a", "name": "Alice", "native_language": "English"}])
    updated = profiles_store.patch_profile("a", {"native_language": "Spanish"})
    assert updated["native_language"] == "Spanish"
    assert updated["name"] == "Alice"  # untouched field survives the merge
    # And it's actually persisted, not just returned:
    assert profiles_store.get_profile_by_id("a")["native_language"] == "Spanish"


def test_patch_profile_returns_none_for_unknown_id():
    profiles_store.save_profiles([{"id": "a", "name": "Alice"}])
    assert profiles_store.patch_profile("nope", {"name": "x"}) is None


def test_delete_profile_removes_only_that_profile():
    profiles_store.save_profiles([{"id": "a", "name": "Alice"}, {"id": "b", "name": "Bob"}])
    assert profiles_store.delete_profile("a") is True
    remaining = profiles_store.load_profiles()
    assert [p["id"] for p in remaining] == ["b"]


def test_delete_profile_returns_false_for_unknown_id():
    profiles_store.save_profiles([{"id": "a", "name": "Alice"}])
    assert profiles_store.delete_profile("nope") is False


# --- Atomic writes / crash recovery (see save_profiles' own docstring for
# the interrupted-write bug these guard against) ---


def test_save_profiles_leaves_old_file_untouched_if_interrupted_before_replace(monkeypatch):
    """Simulates a process death while the temp file is being written -
    the one moment save_profiles is NOT crash-safe by construction (the
    write to the .tmp file itself isn't atomic; only the swap into place
    is). The destination file must come through completely untouched in
    that case, since os.replace() never runs.
    """
    profiles_store.save_profiles([{"id": "a", "name": "Alice"}])

    from pathlib import Path

    original_write_text = Path.write_text

    def _boom(self, *args, **kwargs):
        if self.suffix == ".tmp":
            raise OSError("simulated crash mid-write")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _boom)
    with pytest.raises(OSError):
        profiles_store.save_profiles([{"id": "a", "name": "Alice V2"}])

    # Deliberately NOT monkeypatch.undo() here: that undoes EVERY patch made
    # via this same monkeypatch fixture instance so far, in reverse order -
    # not just the write_text patch above. The autouse isolated_data_dir
    # fixture (conftest.py) also uses this identical fixture instance to
    # set profiles_store.PROFILES_FILE/DATA_DIR for this test, so calling
    # undo() here would silently revert THOSE too, pointing PROFILES_FILE
    # back at the real on-disk path for the assertion below - which is
    # exactly what happened the first time this test was written, and is
    # why it intermittently read real profile data instead of the tmp
    # fixture's. Unnecessary anyway: load_profiles() below only calls
    # read_text(), which _boom never touches, so nothing needs restoring
    # for this assertion to be valid - pytest's own fixture teardown undoes
    # the write_text patch for us once the test function returns.
    assert profiles_store.load_profiles() == [{"id": "a", "name": "Alice"}]


def test_load_profiles_recovers_from_backup_when_main_file_corrupted():
    profiles_store.save_profiles([{"id": "a", "name": "Alice"}])
    profiles_store.save_profiles([{"id": "a", "name": "Alice V2"}])  # second save backs up the first as .bak

    profiles_store.PROFILES_FILE.write_text("{not valid json", encoding="utf-8")

    assert profiles_store.load_profiles() == [{"id": "a", "name": "Alice"}]


def test_backup_is_never_overwritten_with_already_corrupt_content():
    profiles_store.save_profiles([{"id": "a", "name": "Alice"}])
    profiles_store.save_profiles([{"id": "a", "name": "Alice V2"}])  # .bak now holds "Alice"

    # Corrupt the main file directly (bypassing save_profiles), then save
    # again - the pre-save validity check must skip backing up a corrupted
    # file, so the last genuinely good backup isn't clobbered.
    profiles_store.PROFILES_FILE.write_text("{not valid json", encoding="utf-8")
    profiles_store.save_profiles([{"id": "a", "name": "Alice V3"}])

    import json

    backup_path = profiles_store.PROFILES_FILE.with_suffix(".json.bak")
    assert json.loads(backup_path.read_text(encoding="utf-8"))["profiles"] == [{"id": "a", "name": "Alice"}]


# --- migrate_legacy_profile_state (design_plans/issues.md #1: existing
# users' stats shouldn't silently reset to zero when profile_state moved
# out of profiles.json - see the function's own docstring) ---


def test_migrate_legacy_profile_state_moves_values_into_memory_db():
    profiles_store.save_profiles(
        [
            {
                "id": "p1",
                "name": "Alice",
                "total_seconds_studied": 3600,
                "last_active_date": "2026-01-01",
                "current_streak": 5,
                "seen_milestones": ["streak:3"],
                "last_auto_backup_at": "2026-01-01T00:00:00+00:00",
            }
        ]
    )

    profiles_store.migrate_legacy_profile_state()

    assert memory.get_profile_state("p1") == {
        "total_seconds_studied": 3600,
        "last_active_date": "2026-01-01",
        "current_streak": 5,
        "seen_milestones": ["streak:3"],
        "last_auto_backup_at": "2026-01-01T00:00:00+00:00",
    }


def test_migrate_legacy_profile_state_strips_legacy_fields_from_profiles_json():
    profiles_store.save_profiles([{"id": "p1", "name": "Alice", "total_seconds_studied": 3600, "current_streak": 5}])

    profiles_store.migrate_legacy_profile_state()

    profile = profiles_store.get_profile_by_id("p1")
    assert "total_seconds_studied" not in profile
    assert "current_streak" not in profile
    assert profile["name"] == "Alice"  # everything else untouched


def test_migrate_legacy_profile_state_is_idempotent_and_never_overwrites_newer_state():
    profiles_store.save_profiles([{"id": "p1", "name": "Alice", "total_seconds_studied": 3600}])

    profiles_store.migrate_legacy_profile_state()
    # Normal app usage since the migration - a real, newer total:
    memory.add_seconds_studied("p1", 100)

    # Running the migration again (e.g. a second app launch) must not
    # revert that newer total back to the stale profiles.json value - the
    # legacy fields are already gone from profiles.json anyway (stripped
    # by the first run), but this is the actual guarantee that matters:
    # profile_state_row_exists is true, so this run touches nothing.
    profiles_store.migrate_legacy_profile_state()

    assert memory.get_profile_state("p1")["total_seconds_studied"] == 3700


def test_migrate_legacy_profile_state_skips_profiles_with_no_legacy_fields():
    profiles_store.save_profiles([{"id": "p1", "name": "Alice"}])  # a profile created after this update - nothing to migrate

    profiles_store.migrate_legacy_profile_state()

    assert memory.profile_state_row_exists("p1") is False


def test_migrate_legacy_profile_state_does_not_overwrite_an_already_migrated_row():
    """Simulates a profile_state row that already exists (from a previous
    migration run, or normal use) alongside stale profiles.json fields
    that were never cleaned up for some reason - the memory.db row must
    win, never get clobbered by the stale JSON values.
    """
    profiles_store.save_profiles([{"id": "p1", "name": "Alice", "total_seconds_studied": 999}])
    memory.set_profile_state("p1", {"total_seconds_studied": 42})

    profiles_store.migrate_legacy_profile_state()

    assert memory.get_profile_state("p1")["total_seconds_studied"] == 42


# --- migrate_legacy_model_name ---


def test_migrate_legacy_model_name_updates_a_conversation_on_a_retired_model(make_profile, make_conversation):
    profile = make_profile()
    conv = make_conversation(profile["id"], model_name="gemini-2.5-flash-native-audio-latest")

    profiles_store.migrate_legacy_model_name()

    updated = memory.get_conversation(conv["id"])
    assert updated["config"]["model_name"] == profiles_store.DEFAULT_MODEL


def test_migrate_legacy_model_name_leaves_a_currently_valid_model_untouched(make_profile, make_conversation):
    profile = make_profile()
    conv = make_conversation(profile["id"], model_name=profiles_store.DEFAULT_MODEL)

    profiles_store.migrate_legacy_model_name()

    updated = memory.get_conversation(conv["id"])
    assert updated["config"]["model_name"] == profiles_store.DEFAULT_MODEL
    assert updated["updated_at"] == conv["updated_at"]  # untouched, not just unchanged in value


def test_migrate_legacy_model_name_leaves_a_conversation_with_no_model_name_untouched(make_profile):
    profile = make_profile()
    conv = memory.create_conversation(profile["id"], {"target_language": "Spanish"})  # no model_name key at all

    profiles_store.migrate_legacy_model_name()  # must not raise

    updated = memory.get_conversation(conv["id"])
    assert "model_name" not in updated["config"]


# --- get_client_for_key ---


def test_get_client_for_key_raises_without_a_key():
    with pytest.raises(ValueError):
        profiles_store.get_client_for_key(None)
    with pytest.raises(ValueError):
        profiles_store.get_client_for_key("   ")


def test_get_client_for_key_builds_a_client_given_a_key():
    client = profiles_store.get_client_for_key("fake-test-key")
    assert client is not None
