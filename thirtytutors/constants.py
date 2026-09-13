"""Static configuration: voice/model option lists, defaults, and file
paths shared across the app. No logic lives here - just data other
modules import.

Tutor prompt text and tool schemas do NOT live here anymore - see
tutor_instructions.py (everything sent to the model as system_instruction)
and tutor_tools.py (the set_mood/start_quiz tool schemas) instead. This
file keeps DEFAULT_DIFFICULTY only, since routes/profile code needs a
plain config default independent of any instruction text.
"""

import shutil
from pathlib import Path

from platformdirs import user_data_dir

DEFAULT_DIFFICULTY = "intermediate"

DEFAULT_VOICE = "Kore"
DEFAULT_NATIVE_LANGUAGE = "English"
DEFAULT_TARGET_LANGUAGE = "Polish"

# Non-realtime text model used only for periodic rolling-summary folding
# (memory.py / summarize_conversation in summarization.py) - cheap
# free-tier text calls, separate from the Live API models used for the
# actual conversation. gemini-2.5-flash's free tier is capped at 20 RPD,
# too low for a summarization call firing every ~15 turns across active
# conversations; gemini-3.1-flash-lite gives 500 RPD instead and is plenty
# for this task.
SUMMARY_MODEL = "gemini-3.1-flash-lite"

# Official voice names + descriptors are from Google's docs
# (ai.google.dev/gemini-api/docs/speech-generation), which label voices by
# tone/character. Gender is not an official Google label - this mapping is
# a manual categorization for the UI's gender-first picker, not a claim
# Google makes. pitch is a supplementary manual categorization (same
# spirit as gender), shown alongside descriptor on the avatar-select page.
VOICE_OPTIONS = [
    {
        "name": "Zephyr",
        "descriptor": "Bright",
        "gender": "Female",
        "pitch": "High",
        "alias": "Mila",
    },
    {
        "name": "Puck",
        "descriptor": "Upbeat",
        "gender": "Male",
        "pitch": "Mid-range",
        "alias": "Max",
    },
    {
        "name": "Charon",
        "descriptor": "Informative",
        "gender": "Male",
        "pitch": "Mid-to-low",
        "alias": "Leo",
    },
    {
        "name": "Kore",
        "descriptor": "Firm",
        "gender": "Female",
        "pitch": "Mid-to-high",
        "alias": "Chloe",
    },
    {
        "name": "Fenrir",
        "descriptor": "Excitable",
        "gender": "Male",
        "pitch": "Mid-range",
        "alias": "Felix",
    },
    {
        "name": "Leda",
        "descriptor": "Youthful",
        "gender": "Female",
        "pitch": "High",
        "alias": "Ava",
    },
    {
        "name": "Orus",
        "descriptor": "Firm",
        "gender": "Male",
        "pitch": "Mid-to-low",
        "alias": "Miles",
    },
    {
        "name": "Aoede",
        "descriptor": "Breezy",
        "gender": "Female",
        "pitch": "Mid-range",
        "alias": "Zoe",
    },
    {
        "name": "Callirrhoe",
        "descriptor": "Easy-going",
        "gender": "Female",
        "pitch": "Mid-to-high",
        "alias": "Elena",
    },
    {
        "name": "Autonoe",
        "descriptor": "Bright",
        "gender": "Female",
        "pitch": "High",
        "alias": "Maya",
    },
    {
        "name": "Enceladus",
        "descriptor": "Breathy",
        "gender": "Male",
        "pitch": "Mid-range",
        "alias": "Hugo",
    },
    {
        "name": "Iapetus",
        "descriptor": "Clear",
        "gender": "Male",
        "pitch": "Mid-to-low",
        "alias": "Jasper",
    },
    {
        "name": "Umbriel",
        "descriptor": "Easy-going",
        "gender": "Male",
        "pitch": "Mid-range",
        "alias": "Oscar",
    },
    {
        "name": "Algieba",
        "descriptor": "Smooth",
        "gender": "Female",
        "pitch": "Mid-to-low",
        "alias": "Isla",
        "api_voice_name": "Zephyr",
    },
    {
        "name": "Despina",
        "descriptor": "Smooth",
        "gender": "Female",
        "pitch": "Mid-range",
        "alias": "Nina",
    },
    {
        "name": "Erinome",
        "descriptor": "Clear",
        "gender": "Female",
        "pitch": "Mid-to-high",
        "alias": "Lana",
    },
    {
        "name": "Algenib",
        "descriptor": "Gravelly",
        "gender": "Male",
        "pitch": "Low",
        "alias": "Theo",
    },
    {
        "name": "Rasalgethi",
        "descriptor": "Informative",
        "gender": "Male",
        "pitch": "Mid-range",
        "alias": "Milo",
    },
    {
        "name": "Laomedeia",
        "descriptor": "Upbeat",
        "gender": "Female",
        "pitch": "Mid-range",
        "alias": "Jade",
    },
    {
        "name": "Achernar",
        "descriptor": "Soft",
        "gender": "Female",
        "pitch": "Mid-range",
        "alias": "Ruby",
    },
    {
        "name": "Alnilam",
        "descriptor": "Firm",
        "gender": "Male",
        "pitch": "Mid-to-low",
        "alias": "Ezra",
    },
    {
        "name": "Schedar",
        "descriptor": "Even",
        "gender": "Male",
        "pitch": "Mid-to-low",
        "alias": "Kai",
    },
    {
        "name": "Gacrux",
        "descriptor": "Mature",
        "gender": "Female",
        "pitch": "Mid-to-low",
        "alias": "Holly",
    },
    {
        "name": "Pulcherrima",
        "descriptor": "Forward",
        "gender": "Female",
        "pitch": "High",
        "alias": "Stella",
        "api_voice_name": "Leda",
    },
    {
        "name": "Achird",
        "descriptor": "Friendly",
        "gender": "Male",
        "pitch": "Mid-to-high",
        "alias": "Finn",
    },
    {
        "name": "Zubenelgenubi",
        "descriptor": "Casual",
        "gender": "Male",
        "pitch": "Low",
        "alias": "Nico",
    },
    {
        "name": "Vindemiatrix",
        "descriptor": "Gentle",
        "gender": "Female",
        "pitch": "Low",
        "alias": "Wren",
    },
    {
        "name": "Sadachbia",
        "descriptor": "Lively",
        "gender": "Male",
        "pitch": "Low",
        "alias": "Dean",
    },
    {
        "name": "Sadaltager",
        "descriptor": "Knowledgeable",
        "gender": "Male",
        "pitch": "Mid-range",
        "alias": "Brett",
    },
    {
        "name": "Sulafat",
        "descriptor": "Warm",
        "gender": "Female",
        "pitch": "Mid-to-high",
        "alias": "Piper",
    },
]

# Voice name used when calling the Google Live API. Most entries use their
# own `name`, but a few aliases map to a different underlying Google voice
# so the avatar's photo and local sample stay intact while the API still
# receives a valid voice identifier.
VOICE_NAME_TO_API = {v["name"]: v.get("api_voice_name") or v["name"] for v in VOICE_OPTIONS}


def get_api_voice_name(voice_name: str) -> str:
    return VOICE_NAME_TO_API.get(voice_name, voice_name)


# Live API model choices. Rate limits are what's visible on the free tier as
# of mid-2026 and can change - shown in the UI so the choice is informed.
#
# gemini-2.5-flash-native-audio-latest was removed entirely (not just
# deprioritized) after most of the duplicate/repeated-response issues
# logged in design_plans/issues.md turned out to happen on it specifically
# - see that file for the actual incidents. With only one entry here,
# live_session.py's fallback_model selection naturally computes to None
# (nothing else left to fall back to), so this also stops it from ever
# being used as a silent fallback, not just as a direct UI choice. See
# profiles_store.migrate_legacy_model_name for what happens to
# conversations that were already created while it was still an option.
MODEL_OPTIONS = [
    {
        "id": "gemini-3.1-flash-live-preview",
        "label": "Gemini 3 Flash Live",
        "rate_limit_note": "65K TPM",
        "supports_affective_dialog": False,
    },
]
DEFAULT_MODEL = MODEL_OPTIONS[0]["id"]

# Single source of truth for the package's version - pyproject.toml reads
# this dynamically at build time (see its own
# [tool.setuptools.dynamic] section), so bumping this one line is the only
# thing a release needs; nothing else has to be kept in sync by hand.
# routes_api.get_app_info (the Settings modal's About tab) prefers reading
# the INSTALLED package's own metadata via importlib.metadata instead of
# importing this directly, falling back to this constant only if the
# package isn't recognized as installed at all (e.g. running straight fromgit
# a source checkout without ever having been pip-installed).
APP_VERSION = "1.2.0"

# OS-appropriate per-user data directory (profiles.json, memory.db,
# voice_enrollment/) instead of storing user data inside the package tree
# itself - a `pip install --upgrade` reinstalls/overwrites the package's
# own files, and site-packages isn't guaranteed writable anyway.
# appauthor=False keeps this flat (%LOCALAPPDATA%\ThirtyTutors on Windows)
# rather than platformdirs' default doubled-up
# %LOCALAPPDATA%\ThirtyTutors\ThirtyTutors.
_OLD_DATA_DIR = Path(__file__).parent / "data"
DATA_DIR = Path(user_data_dir("ThirtyTutors", appauthor=False))


def _migrate_legacy_data_dir() -> None:
    """One-time move of any data sitting next to this package (see
    _OLD_DATA_DIR above) into the OS-managed DATA_DIR, for installs that
    predate that location. Safe to call on every startup: a fresh install
    has no _OLD_DATA_DIR at all, and a fully-migrated install has an
    _OLD_DATA_DIR that's empty (or absent), so this is a no-op past the
    first run either way. Per-item existence checks make it resumable if a
    prior run was interrupted partway through.
    """
    if not _OLD_DATA_DIR.is_dir():
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for item in _OLD_DATA_DIR.iterdir():
        target = DATA_DIR / item.name
        if target.exists():
            continue
        try:
            shutil.move(str(item), str(target))
        except OSError:
            pass  # leave it in the old location - next startup will retry


_migrate_legacy_data_dir()

PROFILES_FILE = DATA_DIR / "profiles.json"

# Avatar .glb models, voice .wav samples, and tile .webp photos are NOT
# bundled in the pip package (they're ~450MB - see README) - they're
# downloaded once by `thirtytutors setup`/first run (see cli.py's bootstrap)
# from a GitHub Release into this OS-managed location instead, alongside
# the rest of DATA_DIR. main.py mounts /avatar, /voices, /photos from here.
# The landing-page's marketing video/gif assets live here too, under
# ASSETS_DIR/marketing (main.py mounts /marketing) - same reasoning, own
# independent version/tag (see MARKETING_ASSETS_VERSION below).
ASSETS_DIR = DATA_DIR / "assets"

# Bumped independently of APP_VERSION (see cli.py) - these assets rarely
# change, so a code-only release shouldn't force everyone to re-download
# ~450MB for nothing. cli.py compares this against a marker file left in
# ASSETS_DIR after a successful download, and only re-downloads on mismatch.
ASSETS_VERSION = "1"

# GitHub Release tag the three asset zips (avatars.zip/voices.zip/photos.zip)
# are attached to - see cli.py's _ASSET_FILES for the exact download URLs
# built from this.
ASSETS_RELEASE_TAG = "assets-v1"

# Landing-page (marketing) video/gif assets - same download-on-setup
# pattern as the avatar/voice/photo bundle above (see ASSETS_DIR), but
# versioned and tagged completely independently of it rather than folded
# into ASSET_FILES/ASSETS_VERSION. Those three are core, ~450MB, and
# rarely change; this one is landing-page-only, much smaller, and likely
# to get iterated on its own schedule - sharing one version number would
# mean every marketing-asset tweak forces existing users to re-download
# the entire 450MB core bundle too, just to get a few new MB of video. See
# design_plans/workflow/RELEASING_MARKETING_ASSETS.md.
MARKETING_ASSETS_VERSION = "1"
MARKETING_ASSETS_RELEASE_TAG = "marketing-v1"

# Hand-written release notes, one file per version - see
# releases/vX.Y.Z_release.md. Bundled as package data (pyproject.toml)
# so an installed copy always carries its own notes for every version up
# to itself, with no network call needed to display them.
RELEASES_DIR = Path(__file__).resolve().parent / "releases"

# native_language and model_name aren't just schema leftovers - avatarSelect.js
# reads them off the profile as the default native_language/model_name when
# adding a new language to an EXISTING profile, and native_language is also
# directly editable from the Settings modal's General tab (settings.js). Every
# conversation still stores its own config independently once created (see
# memory.py) - these two are only ever read as a starting point for a new one.
#
# voice_name/target_language/resumption_handle/resumption_config really are
# unused: no code path reads them off a profile (every conversation picks its
# own voice fresh via /avatar-select, and session resumption is tracked per
# conversation - see memory.py/live_session.py - not per profile). Dropped
# from this list entirely.
PROFILE_EDITABLE_FIELDS = (
    "mic_device_id",
    "mic_label",
    "voice_gender",
    "name",
    "native_language",
    "model_name",
    "active_conversation_id",
    "api_key",
    "mic_calibrations",
    "default_difficulty",
    "langfuse_public_key",
    "langfuse_secret_key",
    "langfuse_base_url",
    "auto_backup_enabled",
    "auto_backup_interval_days",
)

# Live API connect retries. Classification (is_transient_error) and
# retryDelay parsing live in retry.py, shared with summarize_conversation's
# retry.call_with_retry in summarization.py - this loop stays separate only
# because it manages an async context manager (see _connect_live_with_retries
# in live_session.py).
RETRY_ATTEMPTS = 4  # additional attempts after the first, so 5 tries total
RETRY_BASE_DELAY = 1.5  # seconds, doubles each retry (or uses Google's suggested retryDelay if present)

# Voice-based speaker verification (hands-free background-voice filtering).
# Resemblyzer only (see requirements.txt for why ECAPA-TDNN/SpeechBrain
# were evaluated and dropped).
SPEAKER_VERIFICATION_BACKEND = "resemblyzer"

# Cosine-similarity cutoff for "this is the enrolled speaker." This global
# default is now mostly a fallback/baseline for the hands-free-setup page's
# threshold-calibration test itself (see THRESHOLD_TEST_SENTENCES below) -
# every profile that completes that test gets its OWN calibrated threshold
# instead, stored as profile["mic_speaker_threshold"] and used at runtime
# in preference to this constant. 0.8 deliberately sits above what real
# speech scored in testing (genuine speech clustered 0.60-0.75 on one real
# setup), so that during the test itself every predefined sentence reads as
# "rejected" against it - the test isn't pass/fail, it's just collecting
# real similarity scores, and the LOWEST of those becomes the profile's
# actual threshold.
SPEAKER_VERIFICATION_THRESHOLD = 0.8

VOICE_ENROLLMENT_DIR = DATA_DIR / "voice_enrollment"
SPEECH_DETECTION_DIR = DATA_DIR / "speech_detection"

# Read aloud once per profile during voice enrollment. Kept short (~5s
# each) and phonetically varied enough across the 3 samples for a stable
# averaged reference embedding.
ENROLLMENT_SENTENCES = [
    "The quick brown fox jumps over the lazy dog near the river.",
    "I would like to practice speaking a little more clearly today.",
    "Yesterday we walked to the market and bought some fresh bread.",
]

# Read aloud during the hands-free-setup page's threshold-calibration test,
# one at a time with a pause between each - each reading gets scored
# against the profile's enrolled reference for that mic (speech_detection.score).
# Deliberately short, natural phrases (closer to how someone actually talks
# to the tutor than the longer ENROLLMENT_SENTENCES above), so the
# calibrated threshold reflects realistic in-session speech rather than
# careful enrollment-style reading.
THRESHOLD_TEST_SENTENCES = [
    "Hello.",
    "How are you doing today?",
    "I'm here to learn and practice languages.",
    "I would love to keep learning.",
    "Thank you, have a great day.",
]

# The threshold test also records ONE clip of this - deliberately not
# speech - right after the 5 sentences above, and scores it the same way,
# so the setup page can place the calibrated threshold at the midpoint
# between the weakest genuine-speech reading and actual measured noise on
# this specific mic, rather than guessing from speech samples alone.
THRESHOLD_TEST_NOISE_PROMPT = "Stay quiet, or make some noise without speaking (tap the desk, cough, rustle papers)..."

# Hands-free mode: the mic stays open continuously (no push-to-talk), so
# incoming audio is chopped into rolling windows and each window is
# speaker-verified before being forwarded to Gemini. 1.6s matches
# Resemblyzer's own internal partial-utterance window (VoiceEncoder slices
# audio into 1.6s partials internally), so this is close to the minimum
# that's still a good fit for the active backend - shrinking further would
# reduce speak-to-response delay but hurt embedding quality.
HANDSFREE_WINDOW_SECONDS = 1.6
HANDSFREE_WINDOW_BYTES = int(16000 * HANDSFREE_WINDOW_SECONDS) * 2  # 16kHz, int16 = 2 bytes/sample

# RMS (on float32 [-1,1] samples) below this is treated as silence rather
# than "someone else is talking" - used to decide when to close out a turn
# (send activity_end) versus just dropping a window without ending the
# turn, and also to trim silence padding before speaker-embedding (see
# speech_detection/audio_utils.py). This is only the fallback default -
# each profile calibrates its own value per-mic via the hands-free-setup
# page's "Calibrate mic" step, stored in profile["mic_calibrations"][mic_key]
# (see live_session.py's hf_silence_threshold / hf_similarity_threshold for
# how a specific mic's entry is looked up at runtime).
HANDSFREE_SILENCE_RMS_THRESHOLD = 0.01

# Key used for a profile's built-in/OS-default microphone in
# profile["mic_calibrations"] (a dict keyed by mic label, since device IDs
# aren't guaranteed stable across browser sessions/reboots but labels are
# what's already used for mic matching elsewhere - see loadMicsForProfile).
# "Default microphone" is never a real device label, so it can't collide.
DEFAULT_MIC_CALIBRATION_KEY = "__default__"
