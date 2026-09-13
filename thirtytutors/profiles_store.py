"""Profile JSON storage (data/profiles.json) + the Gemini client factory
built from a profile's own API key. Split out of main.py so route modules
and live_session.py can share this without importing each other.
"""

import json
import os
import shutil

from google import genai

from . import memory
from .constants import DATA_DIR, DEFAULT_MODEL, MODEL_OPTIONS, PROFILES_FILE


def _backup_path():
    """profiles.json.bak's path, derived from the CURRENT value of
    PROFILES_FILE every time this is called - never cached at import time.
    Tests monkeypatch profiles_store.PROFILES_FILE per-test for isolation;
    a module-level `_PROFILES_BACKUP_FILE = PROFILES_FILE.with_suffix(...)`
    computed once at import would freeze in whatever PROFILES_FILE was at
    import time (the real on-disk path) and silently ignore any later
    monkeypatching - which is exactly what happened here before this fix:
    every test that exercised save_profiles was copying its fake test data
    into the real machine's actual profiles.json.bak instead of a test's
    isolated tmp path, and load_profiles's corruption-recovery fallback
    was reading real backup data into what should've been an isolated
    test.
    """
    return PROFILES_FILE.with_suffix(".json.bak")


def _read_profiles_json(path) -> list[dict] | None:
    """Returns the parsed profiles list from `path`, or None if it doesn't
    exist or fails to parse. Never raises.
    """
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("profiles", [])
    except (json.JSONDecodeError, OSError):
        return None


def load_profiles() -> list[dict]:
    profiles = _read_profiles_json(PROFILES_FILE)
    if profiles is not None:
        return profiles
    if PROFILES_FILE.exists():
        print("[profiles_store] profiles.json is corrupted or unreadable - trying profiles.json.bak")
    backup_profiles = _read_profiles_json(_backup_path())
    if backup_profiles is not None:
        print(f"[profiles_store] recovered {len(backup_profiles)} profile(s) from profiles.json.bak")
        return backup_profiles
    return []


def save_profiles(profiles: list[dict]) -> None:
    """Writes profiles.json atomically: the new content is written to a
    temp file first, then swapped into place with os.replace(), which is
    atomic on both POSIX and Windows when source and destination are on the
    same volume (guaranteed here - same directory). Without this, a process
    killed mid-write (app force-closed, OS/power loss, a locking antivirus)
    during the old write_text()-based save could leave profiles.json
    truncated or completely empty - write_text() opens the file in 'w'
    mode, which truncates it to 0 bytes immediately, before any new content
    is written.

    Before swapping in the new content, the CURRENT on-disk file is copied
    to profiles.json.bak - but only if it still parses as valid JSON, so a
    file that's already corrupted (e.g. left over from before this fix)
    never overwrites a previously-good backup. load_profiles() falls back
    to this backup if profiles.json itself fails to parse, so a corrupted
    file self-heals from the last known-good save instead of silently
    being treated as zero profiles.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if _read_profiles_json(PROFILES_FILE) is not None:
        try:
            shutil.copy2(PROFILES_FILE, _backup_path())
        except OSError as e:
            print(f"[profiles_store] could not update profiles.json.bak: {type(e).__name__}: {e}")

    tmp_path = PROFILES_FILE.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps({"profiles": profiles}, indent=2), encoding="utf-8")
    os.replace(tmp_path, PROFILES_FILE)


def get_profile_by_id(profile_id: str) -> dict | None:
    for p in load_profiles():
        if p["id"] == profile_id:
            return p
    return None


def patch_profile(profile_id: str, fields: dict) -> dict | None:
    profiles = load_profiles()
    for p in profiles:
        if p["id"] == profile_id:
            p.update(fields)
            save_profiles(profiles)
            return p
    return None


def delete_profile(profile_id: str) -> bool:
    profiles = load_profiles()
    for i, p in enumerate(profiles):
        if p["id"] == profile_id:
            profiles.pop(i)
            save_profiles(profiles)
            return True
    return False


def upsert_profile(profile: dict) -> None:
    """Replaces an existing profile entirely if its id already exists
    (import re-running over the same profile, or restoring a backup onto a
    profile that's still present locally), otherwise appends it as new.
    Unlike patch_profile, this doesn't merge fields into an existing entry -
    the given dict fully replaces whatever was there.
    """
    profiles = load_profiles()
    for i, p in enumerate(profiles):
        if p["id"] == profile["id"]:
            profiles[i] = profile
            save_profiles(profiles)
            return
    profiles.append(profile)
    save_profiles(profiles)


_LEGACY_PROFILE_STATE_FIELDS = (
    "total_seconds_studied",
    "last_active_date",
    "current_streak",
    "seen_milestones",
    "last_auto_backup_at",
)


def migrate_legacy_profile_state() -> None:
    """One-time migration for profiles created before profile_state moved
    from profiles.json into memory.db (see memory.py's own module
    docstring for why). Called once at every app startup (see main.py's
    lifespan, right after memory.init_db() so the profile_state table
    definitely exists first) and fully idempotent: after the first run,
    every profile either already has a memory.db profile_state row
    (migrated just now, or created normally by ordinary use since this
    update - memory.profile_state_row_exists doesn't distinguish the two,
    and doesn't need to) or never had any legacy fields to migrate in the
    first place, so nothing below has any further effect on later
    launches.

    Without this, an existing user updating to this version would see
    their real, already-earned hours-studied/streak/milestones silently
    reset to zero on the stats tab - not a crash, not data corruption (the
    stale fields just sit inert in profiles.json, harmlessly ignored by
    every current read path), but a real, visible regression for anyone
    who'd built up a genuine streak. For any profile that still has ANY of
    the legacy fields sitting in profiles.json AND has no profile_state
    row yet, this copies those values into memory.db verbatim (via
    memory.set_profile_state) and strips the legacy fields out of
    profiles.json - so a person's history survives, and profiles.json
    doesn't carry dead fields forward indefinitely for someone reading it
    later to trip over.
    """
    profiles = load_profiles()
    changed = False
    for profile in profiles:
        profile_id = profile.get("id")
        if not profile_id:
            continue
        if not any(field in profile for field in _LEGACY_PROFILE_STATE_FIELDS):
            continue  # never had legacy fields (a profile created after this update) - nothing to do
        if memory.profile_state_row_exists(profile_id):
            continue  # already migrated, or already has real state from normal use since - never overwrite either with stale profiles.json data
        state = {field: profile.pop(field, None) for field in _LEGACY_PROFILE_STATE_FIELDS}
        memory.set_profile_state(profile_id, state)
        changed = True
        print(f"[profiles_store] migrated legacy profile_state for profile={profile_id!r} into memory.db")
    if changed:
        save_profiles(profiles)


def migrate_legacy_model_name() -> None:
    """One-time-per-stale-value migration: updates any conversation whose
    stored config still references a model that's no longer in
    MODEL_OPTIONS (e.g. gemini-2.5-flash-native-audio-latest, removed
    after most of the duplicate/repeated-response issues logged in
    design_plans/issues.md turned out to happen on it specifically) to the
    current DEFAULT_MODEL instead. Called once at every app startup (see
    main.py's lifespan, right after migrate_legacy_profile_state above).

    Without this, a conversation created before a model was removed would
    silently keep using it forever: MODEL_OPTIONS only gates the UI picker
    and the reconnect fallback choice (see live_session.py's
    fallback_model selection) - it was never a validation constraint on
    what actually gets stored per-conversation - and there's no UI to
    change an existing conversation's model after creation at all (see
    conversations.js's own module docstring: "no live-editing UI for
    them"). A removed model would otherwise have no path back to a current
    one except this.

    Idempotent: a conversation already on a currently-valid model_name (or
    with no model_name recorded at all) is left untouched, so this is
    cheap to run unconditionally on every startup.
    """
    valid_ids = {m["id"] for m in MODEL_OPTIONS}
    for profile in load_profiles():
        for conv in memory.list_conversations(profile["id"]):
            model_name = (conv.get("config") or {}).get("model_name")
            if not model_name or model_name in valid_ids:
                continue
            new_config = dict(conv["config"])
            new_config["model_name"] = DEFAULT_MODEL
            memory.update_conversation(conv["id"], config=new_config)
            print(f"[profiles_store] migrated conversation={conv['id']!r} off retired model {model_name!r} to {DEFAULT_MODEL!r}")


def get_client_for_key(api_key: str | None) -> genai.Client:
    """Builds a Gemini client from a profile's own API key. Each profile
    carries its own key (see constants.PROFILE_EDITABLE_FIELDS) - there's
    no shared or .env fallback, so a profile without one simply can't open
    a session or run summarization; callers should catch ValueError and
    surface it.
    """
    api_key = (api_key or "").strip()
    if not api_key:
        raise ValueError("No Gemini API key is set for this profile.")
    return genai.Client(api_key=api_key)
