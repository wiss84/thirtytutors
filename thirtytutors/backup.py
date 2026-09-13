"""Profile backup: packages one profile's profiles.json entry, its full
conversation/quiz history, and its voice_enrollment folder into a single
zip, and the reverse import. See memory.export_profile_conversations/
import_profile_conversations and quizzes.export_conversation_quizzes/
import_conversation_quizzes for the data-layer half of this - this module
is just the zip packaging plus the automatic-backup scheduling check on
top of it.

Deliberately NOT a physical restructuring of how data is stored day-to-day
(still one shared profiles.json / memory.db / voice_enrollment/, same as
always) - this is an on-demand extraction, built fresh from those shared
stores every time a backup runs.
"""

import io
import json
import shutil
import zipfile
from datetime import UTC, datetime

from . import memory
from .constants import DATA_DIR
from .profiles_store import get_profile_by_id, upsert_profile
from .speech_detection.enrollment import profile_enrollment_dir

# Where an automatic backup is written - no folder-picker UI for this (no
# native picker infrastructure exists elsewhere in the app yet, and this
# keeps the feature to what's actually needed: a periodic local copy, not
# a configurable destination). The Settings UI's "Open backups folder"
# button reuses the same OS-file-explorer-open pattern as the existing
# "Open data folder" button, so the location is still one click away.
AUTO_BACKUP_DIR = DATA_DIR / "backups" / "auto"

_MANIFEST_NAME = "manifest.json"
_ENROLLMENT_PREFIX = "voice_enrollment/"


def build_profile_backup_zip(profile_id: str) -> bytes:
    """Builds the full backup zip in memory and returns its bytes:
    manifest.json (the profile dict plus its nested conversations/quiz
    sessions - already fully JSON-serializable, since profiles.json and
    memory.export_profile_conversations's own output both already are)
    plus a straight file-for-file copy of the profile's voice_enrollment
    folder, if it has one. Used by both the manual Export button and
    maybe_run_auto_backup below - one export path, not two to keep in
    sync.

    profile_state (total_seconds_studied/last_active_date/current_streak/
    seen_milestones/last_auto_backup_at - see memory.py) lives in
    memory.db, not profiles.json, so it's folded into the exported profile
    dict explicitly here rather than already being part of `profile` -
    otherwise a restored backup would silently lose the student's
    streak/hours/milestone history even though everything else came back
    intact.
    """
    profile = get_profile_by_id(profile_id)
    if profile is None:
        raise ValueError(f"Profile {profile_id!r} not found.")

    manifest = {
        "format_version": 1,
        "exported_at": datetime.now(UTC).isoformat(),
        "profile": {**profile, **memory.get_profile_state(profile_id)},
        "conversations": memory.export_profile_conversations(profile_id),
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(_MANIFEST_NAME, json.dumps(manifest, indent=2))
        enrollment_dir = profile_enrollment_dir(profile_id)
        if enrollment_dir.exists():
            for path in enrollment_dir.rglob("*"):
                if path.is_file():
                    zf.write(path, _ENROLLMENT_PREFIX + str(path.relative_to(enrollment_dir)))
    return buf.getvalue()


def import_profile_backup_zip(zip_bytes: bytes) -> str:
    """Reverse of build_profile_backup_zip. Returns the imported profile's
    id.

    profiles.json entry is upserted - fully replaced if a profile with the
    same id already exists locally, appended otherwise (see
    profiles_store.upsert_profile). Conversations/quiz sessions are fully
    replaced per-conversation (memory.import_profile_conversations already
    deletes-then-reinserts, so importing the same backup twice never
    duplicates rows). The voice_enrollment folder is copied into place
    wholesale, overwriting any existing enrollment for that profile id
    entirely rather than merging mic-by-mic - a restored backup should
    behave identically to the machine it came from, not a partial mix of
    old and new enrollment data.

    The manifest's "profile" dict is the merged shape build_profile_backup_zip
    produces (profiles.json fields plus profile_state fields folded
    together) - the profile_state fields are popped back out here and
    written to memory.db via memory.set_profile_state, so profiles.json
    only ever receives its own fields. This also transparently upgrades a
    backup made before profile_state existed as a separate table: an older
    export's "profile" dict already carried these same field names
    directly (they used to live in profiles.json), so the same pop/restore
    logic still finds and migrates them correctly.
    """
    buf = io.BytesIO(zip_bytes)
    with zipfile.ZipFile(buf, "r") as zf:
        manifest = json.loads(zf.read(_MANIFEST_NAME))
        profile = manifest["profile"]
        profile_id = profile["id"]

        state = {
            "total_seconds_studied": profile.pop("total_seconds_studied", None),
            "last_active_date": profile.pop("last_active_date", None),
            "current_streak": profile.pop("current_streak", None),
            "seen_milestones": profile.pop("seen_milestones", None),
            "last_auto_backup_at": profile.pop("last_auto_backup_at", None),
        }

        upsert_profile(profile)
        memory.set_profile_state(profile_id, state)
        memory.import_profile_conversations(profile_id, manifest.get("conversations", []))

        enrollment_names = [n for n in zf.namelist() if n.startswith(_ENROLLMENT_PREFIX) and not n.endswith("/")]
        if enrollment_names:
            dest_root = profile_enrollment_dir(profile_id)
            if dest_root.exists():
                shutil.rmtree(dest_root, ignore_errors=True)
            dest_root.mkdir(parents=True, exist_ok=True)
            for name in enrollment_names:
                rel = name[len(_ENROLLMENT_PREFIX) :]
                dest_path = dest_root / rel
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                dest_path.write_bytes(zf.read(name))

    return profile_id


def _safe_filename_part(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in " -_" else "_" for c in (name or "")).strip()
    return cleaned or "profile"


def maybe_run_auto_backup(profile_id: str) -> bool:
    """Checked once per app session (see routes_api.py's dedicated
    check-auto-backup endpoint, called once from init.js when the learning
    page establishes its profile) - if this profile has auto-backup turned
    on and its configured interval has elapsed since last_auto_backup_at,
    runs the exact same export build_profile_backup_zip does for the
    manual button, writes it under AUTO_BACKUP_DIR, and stamps
    last_auto_backup_at. Returns whether a backup actually ran, purely
    informational (the frontend doesn't currently act on it).

    The app only runs while its window is open, so this can't be a true
    OS-level schedule (Task Scheduler/cron/launchd) without real added
    complexity - a backup can end up up to one full extra interval late if
    the app isn't opened exactly on schedule. Acceptable for a personal
    local backup; the Settings UI states this plainly rather than
    overselling it as a precise schedule.
    """
    profile = get_profile_by_id(profile_id)
    if profile is None or not profile.get("auto_backup_enabled"):
        return False

    interval_days = profile.get("auto_backup_interval_days") or 7
    last_raw = memory.get_profile_state(profile_id).get("last_auto_backup_at")
    if last_raw:
        try:
            last = datetime.fromisoformat(last_raw)
            if (datetime.now(UTC) - last).days < interval_days:
                return False
        except ValueError:
            pass  # malformed stored timestamp (shouldn't happen) - treat as due rather than getting stuck never backing up

    AUTO_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    dest_path = AUTO_BACKUP_DIR / f"ThirtyTutors-{_safe_filename_part(profile.get('name'))}-{stamp}.zip"
    dest_path.write_bytes(build_profile_backup_zip(profile_id))

    memory.set_last_auto_backup_at(profile_id, datetime.now(UTC).isoformat())
    return True
