"""Gemini Live API relay: config building, connect-retry handling, and the
/ws/session websocket route. Split out of main.py.

Turn signaling (push-to-talk):
    client sends "init"          -> session config, must be the first WS message
    client sends "start_turn"    -> server sends activity_start to Gemini
    client streams "audio_chunk" -> forwarded as realtime audio input
    client sends "turn_complete" -> server sends activity_end to Gemini

Turn signaling (hands-free mode):
    client sends "handsfree_start"        -> mic is live; server resets its window buffer
    client streams "handsfree_chunk"      -> raw PCM, continuous (no explicit turn boundary from the client)
    client sends "handsfree_stop"         -> mic muted; server closes out any open turn

    The server itself decides turn boundaries here, since the client has no
    natural start/stop signal when the mic never closes. Incoming audio is
    accumulated into rolling ~2s windows (HANDSFREE_WINDOW_BYTES); each
    window is speaker-verified (speech_detection.verify) against the
    profile's enrolled voice before being forwarded to Gemini - this can't
    piggyback on Gemini's own turn-complete signal, since the decision to
    forward has to happen before Gemini ever sees the audio. A window with
    real energy that fails verification (someone else talking) is dropped
    silently without ending an in-progress turn; a window at/below
    HANDSFREE_SILENCE_RMS_THRESHOLD is treated as a pause and closes the
    turn (activity_end) if one was open. See handle_handsfree_window below.

Both sides of the transcript come directly from Gemini's own Live API:
input_audio_transcription for the student's side, output_audio_transcription
for the tutor's - see build_config below.

Session memory has two layers now:

1. Within a single open Live API connection, the model retains full
   conversation context automatically - no extra work needed. What breaks
   continuity is a reconnect (changing voice/language/model, hitting the
   session time limit, restarting the app, or switching to a different
   conversation), since each reconnect opens a technically new session on
   Google's side. The Live API's session-resumption handle lets a reconnect
   pick up the same context - it's stored per *conversation* now (see
   memory.py), not per profile, since a profile can hold several
   conversations, each with its own voice/language/model and its own
   resumable session.

2. Because a resumption handle only survives a finite window and dies the
   moment voice/model/language change, it alone isn't durable memory. Every
   transcribed turn is also persisted to SQLite (memory.py) and periodically
   folded into a short rolling summary. When a conversation's session starts
   fresh (no handle, or handle rejected), that summary is injected into the
   system instruction so the tutor still "remembers" earlier turns instead
   of a cold start.

Mood: the tutor's avatar expression is driven two ways - a mood_change
message forwarded here whenever Gemini calls the set_mood tool (silent,
model's discretion, driven by tutor_instructions.CONVERSATIONAL_RULES rule
5), and a client-side-only idle-timeout 'sleep' state the frontend manages
entirely on its own (armIdleSleepTimer in audio.js) - this server never
sends or knows about 'sleep'.

Retry behavior: Gemini's preview models occasionally return a transient
"500 INTERNAL", "503 UNAVAILABLE", or (less often) a 429 rate-limit error
that succeeds if you just try again - see retry.py for the shared
classification/backoff logic. The initial Live session connection retries
up to RETRY_ATTEMPTS additional times with exponential backoff (or Google's
own suggested retryDelay when present) before giving up. Errors that aren't
recognizably transient (bad request, auth, unknown model, etc.) fail
immediately instead of being retried pointlessly.

go_away and mid-session errors: several different signals all mean "this
Gemini session is gone, get a working one back" - go_away is Google's own
advance notice (session nearing its time/context limit, with a time_left
grace period); an outright drop (1011, a dead resumption handle, or
otherwise) is an abrupt close with no warning. All are funneled into the
same reconnect path in ws_session's main loop: tear down the old live_cm,
re-fetch the conversation's resumption_handle/summary/taught vocab/trouble
spots fresh from storage (session_resumption_update events and
summarize_conversation both keep writing newer versions of these
throughout an open connection - see _refresh_conversation_memory_context),
connect a fresh session (resumed via that freshly-read handle when
possible), replay any audio chunks that were mid-turn when the old session
ended, and send a new session_status - all without ever closing the
BROWSER's own websocket, so the person never has to notice or reconnect
manually, and never has to repeat anything they were mid-sentence on when
it happened. go_away additionally waits for the current turn to finish
(via go_away_pending in live_to_browser) before reconnecting, since Google
gives a time_left warning rather than closing immediately; an abrupt drop
has no such warning and reconnects right away, replaying whatever audio
was buffered mid-turn when the old session died. A go_away reconnect tries
the SAME model first (nothing about it indicates that model is unhealthy),
with the other configured model as a safety net if that fails - a
dead-resumption-handle error (is_dead_resumption_handle_error, typically a
1008 close) gets the identical same-model treatment, since that's a signal
about the session/handle, not the model's health; any OTHER mid-session
error tries the OTHER model first, with the original as its own safety
net. Recognizing "the Gemini session died" no longer depends on matching a
literal close-code string in the exception text - any exception surfacing
from the live-session tasks (other than the browser's own
WebSocketDisconnect) is treated as reconnectable, so a differently-worded
close from a future SDK version doesn't silently fall through to just
ending the whole browser session with no attempt to recover. A short guard
(RAPID_RECONNECT_LIMIT reconnects within RAPID_RECONNECT_WINDOW_S) stops a
pathological fast-reconnect loop from hammering the API indefinitely;
ordinary go_away cycles over a long study session are minutes apart and
never come close to tripping it.
"""

import asyncio
import base64
import re
import time
import traceback
from datetime import date

import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types

from . import memory, observability, quizzes, retry, scenarios, speech_detection
from .constants import (
    DEFAULT_DIFFICULTY,
    DEFAULT_MIC_CALIBRATION_KEY,
    DEFAULT_MODEL,
    DEFAULT_NATIVE_LANGUAGE,
    DEFAULT_TARGET_LANGUAGE,
    DEFAULT_VOICE,
    HANDSFREE_SILENCE_RMS_THRESHOLD,
    HANDSFREE_WINDOW_BYTES,
    MODEL_OPTIONS,
    RETRY_ATTEMPTS,
    RETRY_BASE_DELAY,
    SPEAKER_VERIFICATION_THRESHOLD,
    VOICE_OPTIONS,
    get_api_voice_name,
)
from .profiles_store import get_client_for_key, get_profile_by_id, patch_profile
from .summarization import summarize_conversation
from .tutor_instructions import build_system_instruction
from .tutor_tools import MOOD_TOOL, build_quiz_tool

router = APIRouter()

# How many reconnects (go_away or a mid-session error) within
# RAPID_RECONNECT_WINDOW_S of each other are tolerated before ws_session
# gives up on this browser session instead of continuing to retry - guards
# against a tight reconnect loop (e.g. a persistent quota/config problem
# that makes every fresh connection fail the same way moments after it
# succeeds) hammering the API indefinitely. An ordinary go_away cycle over
# a long study session recurs every several minutes, nowhere near this
# window, so it never trips.
RAPID_RECONNECT_WINDOW_S = 5.0
RAPID_RECONNECT_LIMIT = 3

# How long (seconds) live_to_browser's watchdog waits, once a turn has
# been sent to Gemini (activity_end), before treating the silence as
# worth telling the person about ("still waiting") vs treating it as an
# outright stall worth giving up on and forcing a reconnect (see
# LiveSessionStalled below). Distinct from RETRY_ATTEMPTS/RETRY_BASE_DELAY
# above, which govern retrying the CONNECT itself - this is about a
# connection that looks healthy but has simply gone silent mid-
# conversation, which Google's own eventual close (1008/1011/etc, often
# 30-60s later per observed behavior) would otherwise be the only thing
# to ever surface to the person, with nothing shown in the meantime.
WAITING_LONG_S = 12.0
STALL_GIVEUP_S = 45.0
# How often the watchdog polls while no message has arrived from Gemini -
# small enough that WAITING_LONG_S/STALL_GIVEUP_S fire close to on time,
# large enough not to spin needlessly.
WATCHDOG_POLL_INTERVAL_S = 2.0

# Message types that carry voice input - dropped without any response while
# a quiz is open (quiz_state["active"]) rather than forwarded to Gemini, per
# the permanent no-mid-quiz-interruption decision. turn_complete is
# included alongside start_turn/audio_chunk/handsfree_* even though it
# carries no audio itself, since honoring one without the other could send
# an activity_end with no matching activity_start.
_VOICE_MESSAGE_TYPES = frozenset(
    {"start_turn", "audio_chunk", "turn_complete", "handsfree_start", "handsfree_chunk", "handsfree_stop"}
)


def _quiz_results_summary(items: list[dict]) -> str:
    """Builds the bracketed, non-spoken text turn injected into the live
    session once a quiz is done (see quiz_done handling in ws_session) -
    tutor_instructions.GUARDRAILS tells the tutor to react to a message
    shaped like this conversationally rather than read it aloud."""
    total = len(items)
    correct = sum(1 for i in items if i["is_correct"])
    summary = f"[Quiz results: {correct}/{total} correct."
    missed = [i for i in items if not i["is_correct"]]
    if missed:
        missed_desc = ", ".join(f"'{i['target_term']}' (wrote '{i['student_answer'] or ''}')" for i in missed)
        summary += f" Missed: {missed_desc}."
    return summary + "]"


_BLANK_RE = re.compile(r"\{\d+\}")


def _validate_quiz_items(items: list[dict]) -> None:
    """Defense-in-depth only - QUIZ_TOOL's schema already makes
    correct_answers required for every item (see tutor_tools.py's
    normalized item shape), so this should rarely fire in practice. a fill_blank_dragdrop item whose
    correct_answers length doesn't match its blank count, so a schema-
    level failure is still visible in the console/Langfuse instead of
    silently reaching the student as a broken, unwinnable slide.
    """
    for idx, item in enumerate(items):
        if not isinstance(item, dict) or item.get("item_type") != "fill_blank_dragdrop":
            continue
        blank_count = len(_BLANK_RE.findall(item.get("text_with_blanks") or ""))
        answer_count = len(item.get("correct_answers") or [])
        if blank_count != answer_count:
            print(
                f"[start_quiz] item {idx} blank/answer count mismatch: "
                f"{blank_count} blanks in text_with_blanks vs {answer_count} correct_answers - {item!r}"
            )


def get_active_conversation(profile: dict) -> dict | None:
    """Returns the profile's active conversation, or None if it has none
    yet. No auto-creation - every conversation now comes from an explicit
    choice on /avatar-select (an avatar/voice and a target language the
    user picked), so a profile with none just means the user hasn't
    started a language yet, not something to paper over with a synthetic
    "Default" conversation.
    """
    profile_id = profile["id"]
    convs = memory.list_conversations(profile_id)
    if not convs:
        return None
    active_id = profile.get("active_conversation_id")
    match = next((c for c in convs if c["id"] == active_id), None)
    return match or convs[0]


def build_config(
    profile: dict,
    conv_config: dict,
    model_name: str,
    resumption_handle: str | None = None,
    summary_text: str | None = None,
    review_terms: list[str] | None = None,
    taught_vocab: list[str] | None = None,
) -> types.LiveConnectConfig:
    name = profile.get("name") or "the student"
    native_language = conv_config.get("native_language") or DEFAULT_NATIVE_LANGUAGE
    target_language = conv_config.get("target_language") or DEFAULT_TARGET_LANGUAGE
    voice_name = conv_config.get("voice_name") or DEFAULT_VOICE
    tutor_name = next(
        (v.get("alias") or v["name"] for v in VOICE_OPTIONS if v["name"] == voice_name),
        voice_name,
    )

    scenario_id = conv_config.get("scenario") or scenarios.DEFAULT_SCENARIO
    scenario_template = scenarios.SCENARIO_TEMPLATES.get(scenario_id, scenarios.SCENARIO_TEMPLATES[scenarios.DEFAULT_SCENARIO])
    difficulty = conv_config.get("difficulty") or DEFAULT_DIFFICULTY

    system_instruction = build_system_instruction(
        scenario_template,
        name=name,
        native_language=native_language,
        target_language=target_language,
        tutor_name=tutor_name,
        difficulty=difficulty,
        summary_text=summary_text,
        review_terms=review_terms,
        taught_vocab=taught_vocab,
    )

    # Plain (non-transparent) resumption. transparent=True (Google's
    # documented mechanism for exactly the symptom in
    # design_plans/issues.md #1) is a real field in the shared SDK schema,
    # but confirmed empirically - not just theorized - to be rejected
    # outright on this app's actual API surface: Gemini Developer API mode
    # (a plain API key via genai.Client(api_key=...), which is what this
    # app always uses) raises "transparent parameter is only supported in
    # Gemini Enterprise Agent Platform mode, not in Gemini Developer API
    # mode" the moment a connection is attempted. That's a hard platform
    # incompatibility, not a version-support gap a try/except could work
    # around - so it's not attempted at all here, permanently, rather than
    # left as a call that will fail identically on every connection every
    # time (see design_plans/issues.md #1 for exactly that failure mode:
    # every connect attempt raising the same ValueError, both models,
    # looking from the outside like an infinite reconnect loop).
    #
    # ResumableSender below still does everything else transparent mode
    # would have needed client-side (indexing, buffering, replay-through-
    # tracked-methods) - none of that depended on transparent=True actually
    # working. Its own prune() already treats a missing
    # last_consumed_client_message_index as a safe no-op; that's now simply
    # the PERMANENT state for this app (the index can never arrive) rather
    # than an occasional edge case, and everything falls back to the
    # coarser, already-correct turn-boundary replay-or-discard decision in
    # ws_session's mid-response-drop handling - which is exactly what was
    # observed working with this parameter removed.
    session_resumption_config = types.SessionResumptionConfig(handle=resumption_handle)

    kwargs = {
        "response_modalities": ["AUDIO"],
        "system_instruction": system_instruction,
        "tools": [MOOD_TOOL, build_quiz_tool(native_language=native_language, target_language=target_language)],
        "input_audio_transcription": types.AudioTranscriptionConfig(),
        "output_audio_transcription": types.AudioTranscriptionConfig(),
        "speech_config": types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=get_api_voice_name(conv_config.get("voice_name") or DEFAULT_VOICE)
                )
            )
        ),
        "realtime_input_config": types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)
        ),
        "session_resumption": session_resumption_config,
    }

    # Extends how long a Live session can run before Google forcibly ends it
    # (a go_away near the default time/context limit) by compressing older
    # context instead of just truncating - added defensively (not baked into
    # kwargs above) since it's a newer field that may not exist on every
    # installed SDK version, same reasoning as enable_affective_dialog below.
    # Worth having regardless of whether it's the actual cause of any given
    # "the tutor tried to end the session" report, since a session that's
    # further from its length limit gives the model less reason to wrap up.
    try:
        kwargs["context_window_compression"] = types.ContextWindowCompressionConfig(sliding_window=types.SlidingWindow())
    except (AttributeError, TypeError):
        print("[build_config] context_window_compression not supported by installed SDK - continuing without it.")

    model_info = next((m for m in MODEL_OPTIONS if m["id"] == model_name), None)
    if model_info and model_info.get("supports_affective_dialog"):
        try:
            return types.LiveConnectConfig(**kwargs, enable_affective_dialog=True)
        except TypeError:
            # Preview-API field name mismatch on this google-genai version -
            # degrade gracefully rather than break the whole session. Falls
            # through to the hardened plain return below, which has its own
            # safety net for context_window_compression.
            print("[build_config] enable_affective_dialog not supported by installed SDK - continuing without it.")

    try:
        return types.LiveConnectConfig(**kwargs)
    except TypeError:
        # Belt-and-suspenders for context_window_compression: the try/except
        # above only catches the classes themselves not existing - if they DO
        # exist but LiveConnectConfig itself doesn't accept the field yet (a
        # partial-support SDK version), this is where that would surface.
        # Drop it and retry once rather than crash session creation entirely.
        kwargs.pop("context_window_compression", None)
        print(
            "[build_config] context_window_compression rejected by LiveConnectConfig on this SDK version - continuing without it."
        )
        return types.LiveConnectConfig(**kwargs)


def is_dead_resumption_handle_error(e: Exception) -> bool:
    """A stored session_resumption handle can outlive its validity on
    Google's side (expiry, server-side eviction, etc.). When that happens
    the Live API rejects the connection outright with a 1008 close code -
    retrying with the same handle will just fail the same way every time,
    so this needs to be detected separately from ordinary transient errors.

    Matches any 1008 close, not just the documented 'BidiGenerateContent
    session not found' wording - a less specific '1008: The operation was
    aborted.' message can also indicate a dead handle, and retrying that
    unchanged fails identically every attempt either way.
    """
    return "1008" in str(e)


class LiveSessionStalled(Exception):
    """Raised by live_to_browser's watchdog when a turn was sent to Gemini
    (activity_end) but nothing at all comes back within STALL_GIVEUP_S -
    surfaces through the exact same task-exception reconnect path as any
    other mid-session error (see ws_session's main loop) instead of
    silently waiting on Gemini's own eventual close, which can otherwise
    take far longer with zero indication to the person that anything is
    wrong.
    """


async def _connect_live_with_retries(client: genai.Client, model_name: str, config: types.LiveConnectConfig):
    """Opens a Live API session, retrying transient connect failures.

    Returns (live_cm, live_session, handle_was_dropped): the caller is
    responsible for calling live_cm.__aexit__ when done (can't use a normal
    `async with` here since the retry loop needs to wrap just the connection
    attempt, not the whole conversation), and handle_was_dropped tells the
    caller whether a resumption handle that looked valid going in was
    rejected as dead by Google and silently swapped out for a fresh session,
    so it can clear that dead handle from storage and report status honestly.
    """
    last_exc: Exception | None = None
    handle_was_dropped = False
    for attempt in range(RETRY_ATTEMPTS + 1):
        live_cm = client.aio.live.connect(model=model_name, config=config)
        try:
            live_session = await live_cm.__aenter__()
            return live_cm, live_session, handle_was_dropped
        except Exception as e:
            last_exc = e
            if is_dead_resumption_handle_error(e) and config.session_resumption and config.session_resumption.handle:
                print("[ws_session] session_resumption handle rejected (expired/unknown) - retrying fresh, without it.")
                config.session_resumption.handle = None
                handle_was_dropped = True
                continue
            if not retry.is_transient_error(e) or attempt == RETRY_ATTEMPTS:
                raise
            delay = retry.parse_retry_delay(str(e)) or (RETRY_BASE_DELAY * (2**attempt))
            print(f"[ws_session] connect attempt {attempt + 1} failed ({type(e).__name__}) - retrying in {delay:.1f}s...")
            await asyncio.sleep(delay)
    raise last_exc  # unreachable, keeps type checkers happy


class ResumableSender:
    """Wraps a Live session's outbound sends with the per-connection
    message-index bookkeeping transparent session resumption depends on
    (see build_config's session_resumption_config, and
    design_plans/issues.md #1 for the symptom this exists to help with).
    Google's docs say the index must count EVERY message sent to the
    model, in order, starting at 1, and reset to 1 on the first message of
    each new connection - get that numbering wrong and
    last_consumed_client_message_index (reported back in
    session_resumption_update - see live_to_browser) won't line up with
    what the server actually counted, which could silently make pruning
    DROP audio that genuinely still needed replaying. So every send method
    below counts, even the ones that aren't buffered.

    Only send_audio's payload is ever buffered for replay - the one thing
    genuinely worth recovering after a drop is the student's own speech;
    activity_start/activity_end/tool responses/injected quiz-result
    content still consume an index each (to keep the running count
    correct) but are never themselves replayed - resending a stray
    activity_start or a two-turns-ago tool response out of context on a
    fresh connection doesn't make sense the way resending unconsumed
    speech does.

    One instance per browser WebSocket session (created once in
    ws_session, before its first connect), reattached to each successive
    Gemini connection via attach() rather than recreated - the buffered
    audio has to survive a reconnect (that's the whole point), while the
    index numbering has to restart. A label ('push-to-talk' or
    'hands-free') tags each buffered chunk so callers can still ask for,
    clear, or replay just one source's audio - both can't be usefully
    interleaved into a single ordered replay, and keeping them separate
    also preserves the existing per-source logging/telemetry
    (flush_turn_buffer_to_memory's audio_chunks_sent,
    replay_unconsumed's own log line) unchanged.
    """

    def __init__(self):
        self._session = None
        self._next_index = 1
        self._buffer: list[tuple[int, bytes, str]] = []  # (index, raw pcm bytes, label)

    def attach(self, live_session) -> None:
        """Points this sender at a (newly connected) session and resets the
        index counter to 1, per protocol. Deliberately does NOT touch the
        buffer - it may still hold not-yet-consumed audio from the
        connection this replaces, which is exactly what a caller replays
        onto the session just attached (see replay_unconsumed below).
        """
        self._session = live_session
        self._next_index = 1

    def _next(self) -> int:
        index = self._next_index
        self._next_index += 1
        return index

    async def send_activity_start(self) -> None:
        self._next()
        await self._session.send_realtime_input(activity_start=types.ActivityStart())

    async def send_activity_end(self) -> None:
        self._next()
        await self._session.send_realtime_input(activity_end=types.ActivityEnd())

    async def send_audio(self, pcm_bytes: bytes, label: str) -> None:
        index = self._next()
        self._buffer.append((index, pcm_bytes, label))
        await self._session.send_realtime_input(audio=types.Blob(data=pcm_bytes, mime_type="audio/pcm;rate=16000"))

    async def send_tool_response(self, **kwargs) -> None:
        self._next()
        await self._session.send_tool_response(**kwargs)

    async def send_client_content(self, **kwargs) -> None:
        self._next()
        await self._session.send_client_content(**kwargs)

    def prune(self, last_consumed_index: int | None) -> None:
        """Drops buffered audio the server has confirmed it already
        received (index <= last_consumed_index) - called from
        live_to_browser whenever a session_resumption_update arrives. A
        None index is a no-op, which in practice is now this app's
        PERMANENT state: last_consumed_client_message_index is only ever
        populated when transparent=True on SessionResumptionConfig, and
        that field is confirmed rejected outright on Gemini Developer API
        mode (see build_config's own comment - a genuine platform
        incompatibility, not a maybe-someday SDK gap). Leaving the buffer
        untouched here means the existing turn-boundary replay-or-discard
        decision (_connect_live_session_with_fallback/replay_unconsumed) is
        exactly what runs - this class was always meant to only ever REFINE
        that decision when real index data is available, never override or
        degrade it when it isn't, and for this app that's simply the
        permanent, only mode it ever runs in.
        """
        if last_consumed_index is None:
            return
        self._buffer = [(i, b, lbl) for (i, b, lbl) in self._buffer if i > last_consumed_index]

    def clear(self, label: str | None = None) -> int:
        """Drops buffered audio - all of it if label is None (a turn/
        window that completed normally: activity_end was sent and there's
        nothing left to ever replay for it, matching the unconditional
        clear both callers used before this class existed), or just one
        label's (start_turn beginning a fresh push-to-talk turn, which
        should only discard ITS OWN stale carryover, never hands-free's).
        Returns how many chunks were actually dropped, so callers that
        need a count for telemetry (flush_turn_buffer_to_memory's
        audio_chunks_sent) don't need a separate buffered_audio() call
        first just to measure it before clearing.
        """
        if label is None:
            count = len(self._buffer)
            self._buffer.clear()
            return count
        keep = [(i, b, lbl) for (i, b, lbl) in self._buffer if lbl != label]
        count = len(self._buffer) - len(keep)
        self._buffer = keep
        return count

    def buffered_audio(self, label: str) -> list[bytes]:
        """Whatever of this label's audio is still buffered, oldest first."""
        return [b for (_, b, lbl) in self._buffer if lbl == label]

    async def replay_unconsumed(self, label: str) -> None:
        """Re-sends whatever's left buffered for `label` (student audio
        that either never reached a completed turn on the connection that
        just died, or - when the server's own last_consumed_client_
        message_index was available - is specifically the tail the server
        confirmed it hadn't processed yet, see prune()) onto whichever
        session is currently attached. Without this, that speech is
        silently lost: the old session never got a turn_complete for it,
        so nothing was ever transcribed, and the student would have to
        notice and repeat themselves.

        Goes through THIS sender's own send_activity_start/send_audio/
        send_activity_end (not the raw underlying session) deliberately -
        a replay is itself real content sent to the new connection, so it
        needs the same index bookkeeping as anything else, and gets
        re-buffered under the new connection's own numbering as a natural
        side effect. That closes a real gap the old direct-to-session
        replay had: a second drop happening shortly after a replay, before
        any fresh audio came in to repopulate the (previously bare) list
        buffer, would have had nothing left to replay a second time -
        here, the replayed audio IS the new buffer going forward.

        Best-effort: a failure here is logged, not raised - the new
        session is otherwise healthy and worth keeping even if this one
        replay didn't land, and the student can just speak again for that
        turn.
        """
        chunks = self.buffered_audio(label)
        if not chunks:
            return
        self.clear(
            label
        )  # about to resend everything through send_audio, which re-buffers it fresh - clear first so that doesn't double up
        try:
            await self.send_activity_start()
            for chunk in chunks:
                await self.send_audio(chunk, label)
            await self.send_activity_end()
            print(f"[ws_session] replayed {len(chunks)} buffered {label} audio chunks onto the new session")
        except Exception as e:  # noqa: BLE001
            print(f"[ws_session] {label} audio replay failed: {type(e).__name__}: {e}")
            self.clear(
                label
            )  # the partial resend's own re-buffering (from whichever send_audio calls DID land) shouldn't linger for a future replay attempt either - same failure posture as the old chunks.clear() in the finally branch this replaces


async def _connect_live_session_with_fallback(
    client: genai.Client,
    profile: dict,
    conv: dict | None,
    conv_config: dict,
    model_name: str,
    fallback_model: str | None,
    resumption_handle: str | None,
    summary_text: str | None,
    review_terms: list[str] | None,
    taught_vocab: list[str] | None = None,
    sender: "ResumableSender | None" = None,
) -> dict:
    """Connects a Live API session for `model_name`, trying `fallback_model`
    (if any) as a second attempt when the first one fails outright. Shared
    by both the very first connection ws_session makes and every later
    mid-session reconnect (go_away, a dropped connection), so there's
    exactly one place that knows how to "get - or get back - a working Live
    session" instead of near-duplicate logic drifting apart across the two
    call sites.

    On success, `sender` (if given - None on the very first connect, where
    there's nothing yet to replay) is attach()ed to the freshly connected
    session, and whatever audio it still has buffered for each source
    (push-to-talk, hands-free) is replayed onto it via
    ResumableSender.replay_unconsumed - see that method for why replay goes
    through the sender itself rather than the raw session.

    Returns a dict: live_cm, live_session, handle_was_dropped, model_name,
    fallback_model, conv_config, config_identity, resumption_handle. The
    caller should reassign its own locals from these keys, since a fallback
    swap changes model_name/fallback_model/conv_config/config_identity
    together, and a dead stored handle clears resumption_handle/
    handle_was_dropped.

    Raises the underlying connection exception - from the fallback attempt
    if one was tried, otherwise from the primary - if every option is
    exhausted. The caller decides how to report that to the browser.

    Wrapped in a "session_connect" Langfuse generation (see observability.py)
    whenever tracing is enabled - the only place the actual system
    instruction sent to Gemini (memory summary + taught vocab + trouble
    spots, all assembled by build_system_instruction) becomes visible
    outside the process, since it's otherwise built and used here without
    ever being logged anywhere. Fires on the very first connect AND on
    every later go_away/error reconnect, since both go through this
    function - so a person can inspect exactly what context was re-injected
    at each reconnect, not just at session start.
    """
    config = build_config(
        profile,
        conv_config,
        model_name,
        resumption_handle=resumption_handle,
        summary_text=summary_text,
        review_terms=review_terms,
        taught_vocab=taught_vocab,
    )
    with observability.span(
        "session_connect",
        as_type="generation",
        input=config.system_instruction,
        model=model_name,
        metadata={
            "has_summary": bool(summary_text),
            "taught_vocab_count": len(taught_vocab or []),
            "review_terms_count": len(review_terms or []),
            "resuming": bool(resumption_handle),
        },
        session_id=(conv or {}).get("id"),
        user_id=profile.get("id"),
    ) as gen:
        used_fallback = False
        try:
            live_cm, live_session, handle_was_dropped = await _connect_live_with_retries(client, model_name, config)
        except Exception as first_exc:
            if not fallback_model:
                observability.update(gen, output={"connected": False, "error": f"{type(first_exc).__name__}: {first_exc}"})
                raise
            print(
                f"[ws_session] connect to {model_name!r} failed ({type(first_exc).__name__}: {first_exc}) "
                f"- trying fallback model {fallback_model!r}."
            )
            model_name, fallback_model = fallback_model, model_name
            conv_config = dict(conv_config)
            conv_config["model_name"] = model_name
            if conv is not None:
                memory.update_conversation(conv["id"], config=conv_config)
            config = build_config(
                profile,
                conv_config,
                model_name,
                resumption_handle=resumption_handle,
                summary_text=summary_text,
                review_terms=review_terms,
                taught_vocab=taught_vocab,
            )
            try:
                live_cm, live_session, handle_was_dropped = await _connect_live_with_retries(client, model_name, config)
                used_fallback = True
            except Exception as second_exc:
                observability.update(
                    gen,
                    output={
                        "connected": False,
                        "error": f"{type(second_exc).__name__}: {second_exc}",
                        "fallback_attempted": True,
                    },
                )
                raise
        observability.update(
            gen,
            output={
                "connected": True,
                "model": model_name,
                "used_fallback": used_fallback,
                "resumption_handle_dropped": handle_was_dropped,
            },
        )

    if handle_was_dropped:
        resumption_handle = None
        if conv is not None:
            memory.clear_resumption(conv["id"])

    if sender is not None:
        sender.attach(live_session)
        await sender.replay_unconsumed("push-to-talk")
        await sender.replay_unconsumed("hands-free")

    return {
        "live_cm": live_cm,
        "live_session": live_session,
        "handle_was_dropped": handle_was_dropped,
        "model_name": model_name,
        "fallback_model": fallback_model,
        "conv_config": conv_config,
        "config_identity": dict(conv_config),
        "resumption_handle": resumption_handle,
    }


def _connect_failure_payload(exc: Exception, fallback_message: str) -> dict:
    """Picks the message shown to the user for a connect failure that's
    exhausted every retry/fallback - a friendly, specific message for the
    two classifiable cases (no internet; genuinely out of free-tier quota),
    the raw exception text otherwise. Shared by both call sites below so
    the classification logic (retry.is_network_error /
    retry.is_rate_limit_error) only lives in one place.
    """
    if retry.is_network_error(exc):
        return {
            "type": "error",
            "kind": "network",
            "message": "Couldn't reach the tutor - check your internet connection and try again.",
        }
    if retry.is_rate_limit_error(exc):
        return {
            "type": "error",
            "kind": "rate_limit",
            "message": "You've hit the free-tier quota limit - try again in a bit.",
        }
    return {"type": "error", "message": fallback_message}


def _refresh_conversation_memory_context(conv: dict, config_identity: dict) -> tuple:
    """Re-fetches everything storage-backed that a (re)connect needs: the
    conversation row itself, plus the rolling summary/taught vocab/trouble
    spots. Used both for the very first connect and before every later
    reconnect (go_away, a mid-session error), so the two can never drift
    into using two different notions of "current".

    This matters specifically for reconnects: session_resumption_update
    events arrive throughout an open Live connection and are written
    straight to storage (memory.set_resumption), but nothing updates a
    caller's own already-in-hand `conv` dict to match - without re-fetching
    it here, a reconnect would keep presenting whatever resumption_handle
    was true back when THIS caller last read it (at session start, or at
    the previous reconnect), not the newest one Google has actually issued
    since. Likewise summarize_conversation runs as a background task
    mid-session and can produce a fresher summary/vocab list before a
    later reconnect happens - reusing a stale copy of those means a
    reconnect's system instruction can omit things the tutor only just
    taught moments ago.

    Also re-applies the "config changed since the stored handle" check
    (config_identity here should be the CURRENT config, from before
    whatever model swap this reconnect might be about to make) - a
    reconnect that's about to switch models is exactly a case where this
    can now be true when it wasn't before, and should behave the same way
    it does at session start: drop the stale handle, start fresh instead
    of resuming into a session built for different settings.

    Returns (conv, resumption_handle, summary_text, review_terms, taught_vocab).
    """
    conv = memory.get_conversation(conv["id"])
    if conv.get("resumption_config") != config_identity:
        if conv.get("resumption_handle") is not None:
            print("[ws_session] config changed since last session on this conversation - starting fresh instead of resuming.")
        memory.clear_resumption(conv["id"])
        conv = memory.get_conversation(conv["id"])
    resumption_handle = conv.get("resumption_handle")
    print(
        f"[ws_session] resumption lookup for conversation={conv['id']!r}: stored_handle={'<none>' if resumption_handle is None else resumption_handle[:12] + '...'}"
    )
    summary_row = memory.get_summary(conv["id"])
    summary_text = summary_row["summary"] if summary_row else None
    review_terms = memory.get_review_candidates(conv["id"])
    taught_vocab = memory.get_taught_vocab(conv["id"])
    return conv, resumption_handle, summary_text, review_terms, taught_vocab


def _record_active_day(profile: dict) -> None:
    """Updates last_active_date/current_streak (Settings modal Stats tab -
    see stats.py) once per calendar day this profile opens a Live session.
    Backed by memory.record_active_day (memory.db's profile_state table,
    not profiles.json - see memory.py's own module docstring for why),
    which does the gap/streak-reset math atomically in one connection. Uses
    the server's own local date, not UTC - this is a self-hosted app
    running on the student's own machine, so server-local time already IS
    the student's local time, and "today"/streaks should mean that, not a
    UTC day boundary that could flip mid-evening for them.

    No no-profile-yet fallback session (profile["id"] is None) ever reaches
    here - see its only call site in ws_session.
    """
    today = date.today().isoformat()
    state = memory.record_active_day(profile["id"], today)
    profile["last_active_date"] = state["last_active_date"]
    profile["current_streak"] = state["current_streak"]


@router.websocket("/ws/session")
async def ws_session(websocket: WebSocket):
    await websocket.accept()

    try:
        init_msg = await websocket.receive_json()
    except WebSocketDisconnect:
        return

    if init_msg.get("type") != "init":
        await websocket.send_json({"type": "error", "message": "First message must be type 'init'."})
        await websocket.close(code=1002)
        return

    profile_id = init_msg.get("profile_id")
    profile = get_profile_by_id(profile_id) if profile_id else None
    conv = None

    if profile is not None:
        requested_cid = init_msg.get("conversation_id")
        if requested_cid:
            conv = memory.get_conversation(requested_cid)
            if conv is not None and conv["profile_id"] != profile_id:
                conv = None
        if conv is None:
            conv = get_active_conversation(profile)
        if conv is None:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "This profile doesn't have a language set up yet - add one first.",
                }
            )
            await websocket.close()
            return

        # Inline overrides (e.g. a no-profile-yet fallback caller, or a
        # client that still sends these) only apply if the conversation's
        # own stored config doesn't already have them - the conversation is
        # the source of truth for voice/language/model once it exists.
        conv_config = dict(conv["config"])
        patch_profile(profile_id, {"active_conversation_id": conv["id"]})
        _record_active_day(profile)
    else:
        # No profile selected yet (first-run fallback) - fully ephemeral,
        # no persisted memory, config comes straight from the init message.
        profile = {
            "id": None,
            "name": init_msg.get("profile_name") or "the student",
            "api_key": init_msg.get("api_key"),
        }
        conv_config = {
            "voice_name": init_msg.get("voice_name") or DEFAULT_VOICE,
            "native_language": init_msg.get("native_language") or DEFAULT_NATIVE_LANGUAGE,
            "target_language": init_msg.get("target_language") or DEFAULT_TARGET_LANGUAGE,
            "model_name": init_msg.get("model_name") or DEFAULT_MODEL,
            "scenario": init_msg.get("scenario") or scenarios.DEFAULT_SCENARIO,
            "difficulty": init_msg.get("difficulty") or DEFAULT_DIFFICULTY,
        }

    model_name = conv_config.get("model_name") or DEFAULT_MODEL
    print(f"[ws_session] init: profile={profile.get('name')!r} conversation={(conv or {}).get('name')!r} model={model_name!r}")

    # The config (voice/language/model) this conversation actually wants. If
    # it differs from the config that produced the currently stored
    # resumption handle, the handle would just resume the OLD session with
    # its baked-in settings - so drop it and open a fresh session, re-seeded
    # from the conversation's rolling summary instead. Otherwise (plain
    # reconnect: network blip, session time limit) we resume and keep the
    # conversation context Google is already holding.
    config_identity = dict(conv_config)
    resumption_handle = None
    summary_text = None
    review_terms = None
    taught_vocab = None

    if conv is not None:
        conv, resumption_handle, summary_text, review_terms, taught_vocab = _refresh_conversation_memory_context(
            conv, config_identity
        )

    observability.set_profile_keys(
        profile.get("langfuse_public_key"),
        profile.get("langfuse_secret_key"),
        profile.get("langfuse_base_url"),
    )

    try:
        client = get_client_for_key(profile.get("api_key"))
    except ValueError as e:
        print(f"[ws_session] {e}")
        try:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": f"{e} Add one for this profile on the landing page (or /profiles) and try again.",
                }
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[ws_session] send_json failed: {exc}")
        await websocket.close()
        return

    # Computed once, up front, so it covers every fallback: the initial
    # connect attempt below, and every later mid-session reconnect (see
    # _connect_live_session_with_fallback).
    fallback_model = None
    if model_name:
        for candidate in MODEL_OPTIONS:
            if candidate["id"] != model_name:
                fallback_model = candidate["id"]
                break

    # One instance for the whole browser session (not recreated per
    # reconnect) - see ResumableSender's own docstring for why its buffered
    # audio needs to survive a reconnect while its index numbering needs to
    # reset for each new connection, and why that split is exactly what
    # attach()/replay_unconsumed give it.
    sender = ResumableSender()

    try:
        connect_result = await _connect_live_session_with_fallback(
            client,
            profile,
            conv,
            conv_config,
            model_name,
            fallback_model,
            resumption_handle,
            summary_text,
            review_terms,
            taught_vocab=taught_vocab,
            sender=sender,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[ws_session] connect failed after retries/fallback: {type(exc).__name__}: {exc}")
        try:
            await websocket.send_json(
                _connect_failure_payload(exc, f"Could not connect to the tutor: {type(exc).__name__}: {exc}")
            )
            await websocket.send_json({"type": "session_status", "model_name": None, "unavailable": True})
        except Exception as send_exc:  # noqa: BLE001
            print(f"[ws_session] send_json failed: {send_exc}")
        await websocket.close()
        return

    live_cm = connect_result["live_cm"]
    live_session = connect_result["live_session"]
    handle_was_dropped = connect_result["handle_was_dropped"]
    model_name = connect_result["model_name"]
    fallback_model = connect_result["fallback_model"]
    conv_config = connect_result["conv_config"]
    config_identity = connect_result["config_identity"]
    resumption_handle = connect_result["resumption_handle"]
    if handle_was_dropped and conv is not None:
        print(f"[ws_session] session_resumption handle for conversation={conv['id']!r} was rejected as dead - cleared.")

    resumed = resumption_handle is not None and not handle_was_dropped
    try:
        await websocket.send_json(
            {
                "type": "session_status",
                "resumed": resumed,
                "conversation_name": (conv or {}).get("name"),
                "model_name": model_name,
            }
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[ws_session] send_json failed: {exc}")

    # Stats tab's "hours studied" figure (stats.py) - one continuous
    # wall-clock span per Live session, from here (connected and about to
    # start relaying) to the accumulation in this function's finally block
    # below, regardless of how many go_away/1011 reconnects happen to the
    # underlying Gemini session in between (those replace live_session
    # in-place without this browser websocket ever closing - see the
    # module docstring). time.monotonic() rather than wall-clock time
    # since only the elapsed duration matters, never an absolute timestamp,
    # and monotonic is immune to the system clock changing mid-session.
    session_start_monotonic = time.monotonic()

    # Voice input is gated (see _VOICE_MESSAGE_TYPES) while quiz_state["active"]
    # is True - set either here (an unfinished quiz from an earlier app
    # session, resumed below) or later when the tutor calls start_quiz mid-
    # conversation (see the tool_call handling in live_to_browser).
    quiz_state = {"active": False, "quiz_id": None}
    if conv is not None:
        in_progress_quiz = quizzes.get_in_progress_quiz(conv["id"])
        if in_progress_quiz is not None:
            quiz_state["active"] = True
            quiz_state["quiz_id"] = in_progress_quiz["quiz_id"]
            try:
                await websocket.send_json(
                    {
                        "type": "quiz_resume",
                        "quiz_id": in_progress_quiz["quiz_id"],
                        "quiz_type": in_progress_quiz["quiz_type"],
                        "items": in_progress_quiz["payload"].get("items"),
                        "current_index": in_progress_quiz["current_index"],
                        "answered_items": in_progress_quiz["answered_items"],
                    }
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[ws_session] send_json failed: {exc}")

    # Streamed transcript chunks get batched into one row per speaker per
    # turn (not one row per chunk) and flushed to SQLite when Gemini signals
    # turn_complete. Both sides come from Gemini's own Live API transcription
    # (input_audio_transcription / output_audio_transcription - see
    # build_config) and stream in the same way, so both are buffered here
    # identically.
    turn_buffer = {"user": [], "tutor": []}

    # Hands-free mode's own turn-tracking state - see module docstring.
    # "buffer" accumulates raw PCM bytes toward the next ~2s window;
    # "turn_active" tracks whether an activity_start has been sent to the
    # CURRENT live_session without a matching activity_end yet, so it gets
    # reset to False at every point below where live_session is replaced
    # (a fresh Gemini session has no memory of the old one's open turn).
    hf_state = {"active": False, "buffer": bytearray(), "turn_active": False}

    # Tracks whether a turn is currently awaiting a response from Gemini -
    # set the moment activity_end is sent (push-to-talk's "turn_complete"
    # message, or hands-free closing a turn on silence/mute), cleared the
    # moment Gemini's own turn_complete comes back. Read by
    # live_to_browser's watchdog (see WAITING_LONG_S/STALL_GIVEUP_S) to
    # distinguish "nothing's happening because it's genuinely idle between
    # turns" from "a turn is outstanding and Gemini has gone silent."
    # Declared here (not inside the per-connection while loop below) since
    # it needs to survive a reconnect - a turn that was outstanding when
    # the old session died is still outstanding once its buffered audio is
    # replayed onto the new one.
    turn_state = {"awaiting_response": False, "started_at": None, "waiting_long_sent": False}
    # Diagnostic-only counter for design_plans/issues.md #3 - see
    # flush_turn_buffer_to_memory's own comment for what this is tracking
    # and why. Reset there at the end of every completed turn, same
    # survives-a-reconnect lifetime as turn_buffer/turn_state above (a
    # tool call that fired just before a drop should still count toward
    # the turn it belongs to once reconnected, not silently reset early).
    turn_diag = {"tool_calls": 0}
    # Which mic entry to read calibration from - keyed by mic label, same
    # key handsfreeSetup.js writes to (see constants.DEFAULT_MIC_CALIBRATION_KEY
    # for the default-mic sentinel). Falls back to the global defaults if
    # this specific mic was never calibrated (shouldn't normally happen -
    # audio.js routes to /handsfree-setup first - but a stale/edited
    # profile shouldn't crash a session over it).
    mic_key = profile.get("mic_label") or DEFAULT_MIC_CALIBRATION_KEY
    mic_calibration = (profile.get("mic_calibrations") or {}).get(mic_key) or {}
    hf_silence_threshold = mic_calibration.get("silence_rms_threshold") or HANDSFREE_SILENCE_RMS_THRESHOLD
    hf_similarity_threshold = mic_calibration.get("speaker_threshold") or SPEAKER_VERIFICATION_THRESHOLD

    # Reconnect-storm guard for the main loop below (go_away / mid-session
    # error reconnects) - see RAPID_RECONNECT_WINDOW_S/RAPID_RECONNECT_LIMIT.
    last_reconnect_at: float | None = None
    rapid_reconnect_count = 0

    async def flush_turn_buffer_to_memory(sender=None):
        user_text = "".join(turn_buffer["user"])
        tutor_text = "".join(turn_buffer["tutor"])
        tutor_chunk_count = len(turn_buffer["tutor"])
        turn_buffer["user"].clear()
        turn_buffer["tutor"].clear()

        if tutor_chunk_count:
            # Diagnostic only, for design_plans/issues.md #3 - a tutor turn
            # that turned out to be two separate model-generated responses
            # concatenated together with no reconnect in between (a
            # credible, documented upstream Gemini Live API issue
            # correlated with function calls - see
            # https://github.com/livekit/agents/issues/2884 - not something
            # this app's own reconnect/replay logic causes or could
            # prevent). One line per COMPLETED turn, not per streamed
            # chunk, so this stays quiet in ordinary use - an unusually
            # high chunk count or char length for a single exchange is the
            # signal worth scanning logs for if this happens again.
            print(
                f"[ws_session] tutor turn complete: {tutor_chunk_count} transcript chunks, "
                f"{len(tutor_text)} chars, {turn_diag['tool_calls']} tool call(s) this turn"
            )
        turn_diag["tool_calls"] = 0

        # audio_chunks_sent reuses state that's already tracked for a
        # completely different reason (replaying a buffered turn to a
        # fallback model on a 1011 - see ResumableSender above), not new
        # bookkeeping added just for this. It's a concrete, queryable
        # signal for one specific failure mode worth being able to search
        # for later: a tutor turn with real spoken content but zero
        # forwarded audio chunks means the tutor generated a response with
        # no real student input behind it that turn. sender.clear(None)
        # both counts and clears in one call - see its own docstring for
        # why clearing everything (not just one label) is correct here:
        # this turn is now fully committed to memory (or, if both texts
        # were empty, deliberately not) either way, so whatever audio
        # produced it - from EITHER source - is done being useful for a
        # later reconnect replay. Clearing here (not just on the next
        # start_turn) is what stops a long-running connection from
        # silently accumulating every past turn's audio into this buffer
        # forever; hands-free mode already clears its own share of it at
        # each of its own turn-boundary points too (see handle_handsfree_
        # window/handsfree_stop below), so this is just push-to-talk's
        # equivalent guarantee at ITS turn-boundary point, applied via the
        # same shared sender.
        audio_chunks_sent = sender.clear(None) if sender is not None else 0
        with observability.span(
            "conversation_turn",
            as_type="generation",
            input=user_text or None,
            model=model_name,
            metadata={"audio_chunks_sent": audio_chunks_sent},
            session_id=(conv or {}).get("id"),
            user_id=profile.get("id"),
        ) as gen:
            observability.update(gen, output=tutor_text or None)

            if conv is None:
                return

            if user_text.strip():
                memory.insert_turn(conv["id"], "user", user_text)
            if tutor_text.strip():
                memory.insert_turn(conv["id"], "tutor", tutor_text)

            prev = memory.get_summary(conv["id"])
            since = prev["based_on_turn"] if prev else 0
            if memory.get_turn_count(conv["id"]) - since >= memory.SUMMARY_FOLD_EVERY_N_TURNS:
                asyncio.create_task(
                    asyncio.to_thread(
                        summarize_conversation,
                        conv["id"],
                        profile.get("name") or "the student",
                        profile.get("api_key"),
                    )
                )

    try:
        while True:
            try:

                async def handle_handsfree_window(window_bytes: bytes):
                    """One ~2s window of continuous hands-free mic audio: decide
                    silence vs speech vs "not the enrolled speaker", and forward,
                    drop, or close out the current turn accordingly. See the
                    module docstring's hands-free section for the full picture.
                    """
                    audio_f32 = speech_detection.pcm16_bytes_to_float32(window_bytes)
                    rms = float(np.sqrt(np.mean(audio_f32.astype(np.float64) ** 2))) if len(audio_f32) else 0.0

                    if rms < hf_silence_threshold:
                        if hf_state["turn_active"]:
                            await sender.send_activity_end()
                            hf_state["turn_active"] = False
                            sender.clear("hands-free")  # turn completed normally - nothing left to replay on a later reconnect
                            turn_state["awaiting_response"] = True
                            turn_state["started_at"] = time.monotonic()
                            turn_state["waiting_long_sent"] = False
                        return

                    try:
                        # CPU/torch work - off the event loop, same reasoning as
                        # summarize_conversation's asyncio.to_thread usage.
                        result = await asyncio.to_thread(
                            speech_detection.verify,
                            profile.get("id"),
                            mic_key,
                            audio_f32,
                            hf_silence_threshold,
                            hf_similarity_threshold,
                        )
                    except Exception as e:  # noqa: BLE001
                        import traceback

                        print(f"[handsfree] verification failed, forwarding unfiltered this window: {type(e).__name__}: {e}")
                        traceback.print_exc()
                        result = None  # fail open - a model hiccup shouldn't silently break hands-free mode

                    if result is not None and not result[0]:
                        print(f"[handsfree] window rejected (score={result[1]:.3f})")
                        return  # someone else is talking (or a false reject) - drop, keep any open turn as-is

                    if result is not None:
                        print(f"[handsfree] window accepted (score={result[1]:.3f}, rms={rms:.4f}) - forwarding")

                    if not hf_state["turn_active"]:
                        await sender.send_activity_start()
                        hf_state["turn_active"] = True
                    await sender.send_audio(window_bytes, "hands-free")

                async def browser_to_live():
                    while True:
                        msg = await websocket.receive_json()
                        msg_type = msg.get("type")

                        if quiz_state["active"] and msg_type in _VOICE_MESSAGE_TYPES:
                            continue  # voice input gated while a quiz is open - see quiz_state

                        try:
                            if msg_type == "start_turn":
                                sender.clear("push-to-talk")
                                await sender.send_activity_start()
                            elif msg_type == "audio_chunk":
                                pcm_bytes = base64.b64decode(msg["data"])
                                await sender.send_audio(pcm_bytes, "push-to-talk")
                            elif msg_type == "turn_complete":
                                await sender.send_activity_end()
                                turn_state["awaiting_response"] = True
                                turn_state["started_at"] = time.monotonic()
                                turn_state["waiting_long_sent"] = False
                            elif msg_type == "handsfree_start":
                                hf_state["active"] = True
                                hf_state["buffer"] = bytearray()
                            elif msg_type == "handsfree_chunk":
                                if not hf_state["active"]:
                                    continue  # stray chunk after a mute race - ignore
                                hf_state["buffer"].extend(base64.b64decode(msg["data"]))
                                while len(hf_state["buffer"]) >= HANDSFREE_WINDOW_BYTES:
                                    window_bytes = bytes(hf_state["buffer"][:HANDSFREE_WINDOW_BYTES])
                                    del hf_state["buffer"][:HANDSFREE_WINDOW_BYTES]
                                    await handle_handsfree_window(window_bytes)
                            elif msg_type == "handsfree_stop":
                                hf_state["active"] = False
                                hf_state["buffer"] = bytearray()
                                if hf_state["turn_active"]:
                                    await sender.send_activity_end()
                                    hf_state["turn_active"] = False
                                    sender.clear("hands-free")  # turn completed normally on mute - nothing left to replay
                                    turn_state["awaiting_response"] = True
                                    turn_state["started_at"] = time.monotonic()
                                    turn_state["waiting_long_sent"] = False
                            elif msg_type == "quiz_answer":
                                quizzes.record_item_answer(
                                    msg["quiz_id"],
                                    item_index=msg["item_index"],
                                    target_term=msg["target_term"],
                                    prompt_or_text=msg["prompt_or_text"],
                                    correct_answer=msg["correct_answer"],
                                    student_answer=msg.get("student_answer"),
                                    is_correct=bool(msg["is_correct"]),
                                )
                            elif msg_type == "quiz_done":
                                quiz_id = msg["quiz_id"]
                                quizzes.finalize_quiz_session(quiz_id, status="completed")
                                quiz_state["active"] = False
                                quiz_state["quiz_id"] = None
                                items = quizzes.get_quiz_items(quiz_id)
                                summary = _quiz_results_summary(items)
                                with observability.span(
                                    "quiz_done",
                                    input={"quiz_id": quiz_id},
                                    metadata={
                                        "total": len(items),
                                        "correct": sum(1 for i in items if i["is_correct"]),
                                    },
                                    session_id=(conv or {}).get("id"),
                                    user_id=profile.get("id"),
                                ):
                                    pass
                                try:
                                    await sender.send_client_content(
                                        turns=types.Content(role="user", parts=[types.Part(text=summary)]),
                                        turn_complete=True,
                                    )
                                except Exception as e:  # noqa: BLE001
                                    print(f"[browser_to_live] quiz results injection failed: {type(e).__name__}: {e}")
                            elif msg_type == "quiz_skip":
                                quiz_id = msg["quiz_id"]
                                quizzes.finalize_quiz_session(quiz_id, status="skipped")
                                quiz_state["active"] = False
                                quiz_state["quiz_id"] = None
                                answered = [i for i in quizzes.get_quiz_items(quiz_id) if i["student_answer"] is not None]
                                with observability.span(
                                    "quiz_skip",
                                    input={"quiz_id": quiz_id},
                                    metadata={"answered_count": len(answered)},
                                    session_id=(conv or {}).get("id"),
                                    user_id=profile.get("id"),
                                ):
                                    pass
                                if answered:
                                    correct = sum(1 for i in answered if i["is_correct"])
                                    summary = f"[Quiz skipped partway through: {correct}/{len(answered)} answered correctly before stopping.]"
                                else:
                                    summary = "[Quiz skipped before answering anything.]"
                                try:
                                    await sender.send_client_content(
                                        turns=types.Content(role="user", parts=[types.Part(text=summary)]),
                                        turn_complete=True,
                                    )
                                except Exception as e:  # noqa: BLE001
                                    print(f"[browser_to_live] quiz skip injection failed: {type(e).__name__}: {e}")
                            elif msg_type == "close":
                                return
                        except Exception as e:
                            print(f"[browser_to_live] '{msg_type}' failed: {type(e).__name__}: {e}")
                            await websocket.send_json(
                                {
                                    "type": "error",
                                    "message": f"'{msg_type}' failed: {type(e).__name__}: {e}",
                                }
                            )
                            raise

                async def live_to_browser(
                    _live_session=live_session,
                    _config_identity=config_identity,
                ):
                    go_away_pending = False
                    while True:
                        response_iter = _live_session.receive().__aiter__()
                        while True:
                            try:
                                response = await asyncio.wait_for(response_iter.__anext__(), timeout=WATCHDOG_POLL_INTERVAL_S)
                            except TimeoutError:
                                # No message from Gemini this poll interval -
                                # only worth acting on if a turn is actually
                                # outstanding (see turn_state above);
                                # otherwise this is just an ordinary idle gap
                                # between turns and needs no attention.
                                if turn_state["awaiting_response"]:
                                    elapsed = time.monotonic() - turn_state["started_at"]
                                    if elapsed >= STALL_GIVEUP_S:
                                        turn_state["waiting_long_sent"] = False
                                        await websocket.send_json({"type": "waiting_long", "active": False})
                                        await websocket.send_json(
                                            {
                                                "type": "error",
                                                "kind": "stalled",
                                                "message": "The tutor didn't respond in time - reconnecting...",
                                            }
                                        )
                                        raise LiveSessionStalled(f"no response within {STALL_GIVEUP_S:.0f}s")
                                    if elapsed >= WAITING_LONG_S and not turn_state["waiting_long_sent"]:
                                        turn_state["waiting_long_sent"] = True
                                        await websocket.send_json({"type": "waiting_long", "active": True})
                                continue
                            except StopAsyncIteration:
                                break

                            # Real progress from Gemini - clear any "still
                            # waiting" indicator the watchdog above may have
                            # shown, regardless of what this particular
                            # response actually contains.
                            if turn_state["waiting_long_sent"]:
                                turn_state["waiting_long_sent"] = False
                                await websocket.send_json({"type": "waiting_long", "active": False})

                            sc = getattr(response, "server_content", None)
                            go_away = getattr(response, "go_away", None)
                            if go_away is not None:
                                go_away_pending = True
                                print(f"[ws_session] go_away received: {go_away}")
                                continue

                            if go_away_pending and sc and getattr(sc, "turn_complete", False):
                                go_away_pending = False
                                turn_state["awaiting_response"] = False
                                await flush_turn_buffer_to_memory(sender)
                                await websocket.send_json({"type": "turn_complete"})
                                return "go_away"

                            # Logged only, no client action. Google's docs
                            # describe this as meaning a genuine barge-in, but
                            # also note it can fire with zero client-side
                            # activity (a "phantom interrupt"). This app's mic
                            # never forwards audio to Gemini while the tutor is
                            # speaking, in push-to-talk or hands-free (see
                            # audio.js/websocket.js), so a genuine barge-in
                            # can't happen here - every occurrence is a phantom
                            # one, kept visible here in case the frequency ever
                            # becomes worth investigating further.
                            if sc and getattr(sc, "interrupted", False):
                                print("[ws_session] server_content.interrupted=True (no client action - see comment)")

                            if getattr(response, "data", None):
                                await websocket.send_json(
                                    {
                                        "type": "audio",
                                        "data": base64.b64encode(response.data).decode("ascii"),
                                    }
                                )

                            in_transcript = getattr(sc, "input_transcription", None) if sc else None
                            in_text = getattr(in_transcript, "text", None) if in_transcript else None
                            if in_text:
                                turn_buffer["user"].append(in_text)
                                await websocket.send_json({"type": "transcript_in", "text": in_text})

                            out_transcript = getattr(sc, "output_transcription", None) if sc else None
                            out_text = getattr(out_transcript, "text", None) if out_transcript else None
                            if out_text:
                                turn_buffer["tutor"].append(out_text)
                                await websocket.send_json({"type": "transcript_out", "text": out_text})

                            tool_call = getattr(response, "tool_call", None)
                            if tool_call:
                                turn_diag["tool_calls"] += 1
                                function_responses = []
                                for fc in tool_call.function_calls:
                                    if fc.name == "set_mood":
                                        mood = (fc.args or {}).get("mood", "neutral")
                                        with observability.span(
                                            "tool_call:set_mood",
                                            input=dict(fc.args or {}),
                                            session_id=(conv or {}).get("id"),
                                            user_id=profile.get("id"),
                                        ):
                                            pass
                                        await websocket.send_json({"type": "mood_change", "mood": mood})
                                    elif fc.name == "start_quiz" and conv is not None:
                                        # Duplicate-quiz guard: if the student never
                                        # finished a previous quiz (possibly from an
                                        # earlier app session - already re-shown via
                                        # quiz_resume above), re-show that one instead
                                        # of starting a second one, so there's never
                                        # more than one in-progress quiz per
                                        # conversation to keep track of.
                                        existing = quizzes.get_in_progress_quiz(conv["id"])
                                        if existing is not None:
                                            quiz_id = existing["quiz_id"]
                                            payload = existing["payload"]
                                        else:
                                            payload = dict(fc.args or {})
                                            items = payload.get("items") or []
                                            _validate_quiz_items(items)
                                            quiz_id = quizzes.start_quiz_session(
                                                conv["id"], quizzes.compute_quiz_type(items), payload
                                            )
                                        # Recorded in full, not summarized - this is
                                        # exactly the artifact needed to permanently
                                        # diagnose a malformed generated quiz the
                                        # defensive per-item validation in
                                        # quizRenderers.js/quizDragDrop.js): every
                                        # quiz payload the tutor ever generates is now
                                        # queryable in Langfuse, whether or not the
                                        # student happened to notice a bad slide.
                                        with observability.span(
                                            "tool_call:start_quiz",
                                            input=payload,
                                            metadata={"reused_in_progress": existing is not None, "quiz_id": quiz_id},
                                            session_id=conv["id"],
                                            user_id=profile.get("id"),
                                        ):
                                            pass
                                        quiz_state["active"] = True
                                        quiz_state["quiz_id"] = quiz_id
                                        await websocket.send_json(
                                            {
                                                "type": "quiz_start",
                                                "quiz_id": quiz_id,
                                                "items": payload.get("items"),
                                            }
                                        )
                                    function_responses.append(
                                        types.FunctionResponse(
                                            id=fc.id,
                                            name=fc.name,
                                            response={"result": "ok"},
                                        )
                                    )
                                await sender.send_tool_response(function_responses=function_responses)

                            resumption_update = getattr(response, "session_resumption_update", None)
                            if resumption_update is not None:
                                # Logged unconditionally (not just the actionable
                                # branch below) - this is the only way to see
                                # whether Google is sending these at all, since
                                # "always shows fresh session" could mean either
                                # "never arrives" or "arrives but resumable=False"/
                                # no new_handle, and those need different fixes.
                                # last_consumed_client_message_index (only
                                # populated when transparent=True and the SDK/
                                # server actually cooperate - see build_config
                                # and ResumableSender's own docstrings for why
                                # neither is guaranteed) is logged the same way
                                # for the same reason - the only way to tell
                                # "unsupported" from "supported but the server
                                # just isn't populating it this session" apart.
                                last_consumed = getattr(resumption_update, "last_consumed_client_message_index", None)
                                print(
                                    f"[ws_session] session_resumption_update: resumable={getattr(resumption_update, 'resumable', None)} has_new_handle={getattr(resumption_update, 'new_handle', None) is not None} last_consumed_client_message_index={last_consumed}"
                                )
                                sender.prune(last_consumed)
                            if resumption_update and getattr(resumption_update, "resumable", False):
                                new_handle = getattr(resumption_update, "new_handle", None)
                                if conv is not None and new_handle:
                                    print(f"[ws_session] storing new resumption handle for conversation={conv['id']!r}")
                                    memory.set_resumption(conv["id"], new_handle, _config_identity)

                            if sc and getattr(sc, "turn_complete", False):
                                turn_state["awaiting_response"] = False
                                await flush_turn_buffer_to_memory(sender)
                                await websocket.send_json({"type": "turn_complete"})

                browser_task = asyncio.create_task(browser_to_live())
                live_task = asyncio.create_task(live_to_browser())
                tasks = [browser_task, live_task]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()

                # What ended this iteration: a task exception (something
                # actually went wrong with the Gemini session, not just this
                # browser socket), a go_away close (live_to_browser returns
                # the "go_away" sentinel once its pending turn wrapped up -
                # see the go_away_pending handling above), the browser's own
                # WebSocketDisconnect (nothing to reconnect - the person is
                # gone), or a clean 'close' message/normal end (also nothing
                # to reconnect).
                task_exception: Exception | None = None
                go_away_closed = False
                browser_disconnected = False
                for task in done:
                    if task.cancelled():
                        continue
                    exc = task.exception()
                    if exc is not None:
                        if isinstance(exc, WebSocketDisconnect):
                            browser_disconnected = True
                        else:
                            task_exception = task_exception or exc
                            print(f"[ws_session] live task failed: {type(exc).__name__}: {exc}")
                    elif task is live_task and task.result() == "go_away":
                        go_away_closed = True

                if browser_disconnected or (task_exception is None and not go_away_closed):
                    break  # browser gone, or a clean end - nothing to reconnect for

                now = time.monotonic()
                if last_reconnect_at is not None and (now - last_reconnect_at) < RAPID_RECONNECT_WINDOW_S:
                    rapid_reconnect_count += 1
                else:
                    rapid_reconnect_count = 0
                last_reconnect_at = now
                if rapid_reconnect_count >= RAPID_RECONNECT_LIMIT:
                    print(
                        f"[ws_session] {rapid_reconnect_count + 1} reconnects within {RAPID_RECONNECT_WINDOW_S}s "
                        "- giving up to avoid a reconnect storm."
                    )
                    try:
                        await websocket.send_json(
                            {
                                "type": "error",
                                "message": "The tutor's connection keeps dropping - please try again in a moment.",
                            }
                        )
                        await websocket.send_json({"type": "session_status", "model_name": None, "unavailable": True})
                    except Exception as exc:  # noqa: BLE001
                        print(f"[ws_session] send_json failed: {exc}")
                    break

                # A go_away reconnect tries the SAME model first (nothing
                # about it indicates that model is unhealthy), with the other
                # configured model only as a safety net. A dead-resumption-
                # handle error (is_dead_resumption_handle_error - typically a
                # 1008 close) gets the same same-model treatment: it's a
                # signal about the SESSION/HANDLE, not evidence the model
                # itself is unhealthy, exactly like go_away. Any OTHER
                # mid-session error tries the OTHER model first (something
                # about the current one genuinely might be wrong), with the
                # original as its own safety net.
                dead_handle_error = task_exception is not None and is_dead_resumption_handle_error(task_exception)
                if task_exception is not None and not dead_handle_error:
                    reconnect_primary, reconnect_secondary = (
                        (fallback_model, model_name) if fallback_model else (model_name, None)
                    )
                    print(f"[ws_session] reconnecting after a mid-session error - trying {reconnect_primary!r}")
                else:
                    reconnect_primary, reconnect_secondary = model_name, fallback_model
                    reason = "a dead-resumption-handle error" if dead_handle_error else "go_away"
                    print(f"[ws_session] reconnecting after {reason} - trying {reconnect_primary!r} (same model)")

                if task_exception is not None and turn_state["awaiting_response"] and turn_buffer["tutor"]:
                    # The student's turn had already been fully sent to
                    # Gemini (activity_end) AND Gemini had already started
                    # streaming SOME response back (turn_buffer["tutor"] is
                    # non-empty) when this connection died mid-response -
                    # the sender's buffered audio for this turn is NOT safe
                    # to replay onto the new session in that case: Gemini
                    # already received and started answering that exact
                    # turn, so replaying the identical audio makes it
                    # answer the same question again, streamed right after
                    # whatever partial response it had already sent to the
                    # browser before the drop. That's what surfaced as "the
                    # tutor repeats itself within the same turn" (see
                    # design_plans/issues.md #1).
                    #
                    # The turn_buffer["tutor"] check specifically (not just
                    # turn_state["awaiting_response"] alone) is what
                    # distinguishes that case from a DIFFERENT one that
                    # looks similar but needs the opposite handling: the
                    # turn was sent, but the connection died before Gemini
                    # said ANYTHING back at all (turn_buffer["tutor"] still
                    # empty). There, nothing has been said yet, so there's
                    # nothing to duplicate - discarding in that case would
                    # just silently swallow the student's turn for no
                    # benefit, forcing them to notice and repeat themselves
                    # (an earlier version of this fix didn't distinguish
                    # the two and did exactly that). That case instead falls
                    # through to the normal reconnect path below, which
                    # replays the buffered audio via
                    # _connect_live_session_with_fallback/replay_unconsumed
                    # exactly like a still-mid-utterance drop - safe here
                    # for the same reason it's safe there: Gemini never
                    # received a complete turn to respond to yet.
                    print(
                        "[ws_session] dropped mid-response with the student's turn already sent to Gemini AND a partial "
                        "reply already started - discarding buffered audio instead of replaying it, to avoid answering "
                        "the same turn twice."
                    )
                    # Also flushes turn_buffer (whatever partial tutor text
                    # had already streamed in before the drop) to memory
                    # as-is, rather than letting it silently carry forward
                    # and get concatenated onto whatever the new session
                    # ends up saying for its own next turn - the second,
                    # independent way this same bug could corrupt what gets
                    # stored, on top of what the person actually hears.
                    await flush_turn_buffer_to_memory(sender)
                    turn_state["awaiting_response"] = False

                try:
                    await live_cm.__aexit__(None, None, None)
                except Exception as exc:  # noqa: BLE001
                    print(f"[ws_session] live_cm.__aexit__ failed during reconnect: {exc}")

                # Re-fetches resumption_handle/summary_text/review_terms/
                # taught_vocab from storage before presenting them to the new
                # connect attempt - see _refresh_conversation_memory_context.
                # Without this, every reconnect within one browser session
                # would keep reusing whatever these were at session START (or
                # at the previous reconnect), even though session_resumption_
                # update events and summarize_conversation both keep writing
                # fresher versions to storage throughout an open connection.
                if conv is not None:
                    conv, resumption_handle, summary_text, review_terms, taught_vocab = _refresh_conversation_memory_context(
                        conv, config_identity
                    )

                try:
                    connect_result = await _connect_live_session_with_fallback(
                        client,
                        profile,
                        conv,
                        conv_config,
                        reconnect_primary,
                        reconnect_secondary,
                        resumption_handle,
                        summary_text,
                        review_terms,
                        taught_vocab=taught_vocab,
                        sender=sender,
                    )
                except Exception as reconnect_exc:  # noqa: BLE001
                    print(f"[ws_session] reconnect failed: {type(reconnect_exc).__name__}: {reconnect_exc}")
                    traceback.print_exc()
                    try:
                        await websocket.send_json(
                            _connect_failure_payload(
                                reconnect_exc,
                                f"Lost connection to the tutor and couldn't reconnect: "
                                f"{type(reconnect_exc).__name__}: {reconnect_exc}",
                            )
                        )
                        await websocket.send_json({"type": "session_status", "model_name": None, "unavailable": True})
                    except Exception as exc:  # noqa: BLE001
                        print(f"[ws_session] send_json failed: {exc}")
                    break

                live_cm = connect_result["live_cm"]
                live_session = connect_result["live_session"]
                handle_was_dropped = connect_result["handle_was_dropped"]
                model_name = connect_result["model_name"]
                fallback_model = connect_result["fallback_model"]
                conv_config = connect_result["conv_config"]
                config_identity = connect_result["config_identity"]
                resumption_handle = connect_result["resumption_handle"]
                resumed = resumption_handle is not None and not handle_was_dropped
                hf_state["turn_active"] = False  # the new Gemini session has no open turn from the old one
                if turn_state["awaiting_response"]:
                    # A turn was still outstanding when the old session died
                    # (its audio just got replayed onto the new one, above) -
                    # give the fresh connection its own full window before
                    # the watchdog could consider IT stalled too.
                    if turn_state["waiting_long_sent"]:
                        turn_state["waiting_long_sent"] = False
                        try:
                            await websocket.send_json({"type": "waiting_long", "active": False})
                        except Exception as exc:  # noqa: BLE001
                            print(f"[ws_session] send_json failed: {exc}")
                    turn_state["started_at"] = time.monotonic()
                try:
                    await websocket.send_json(
                        {
                            "type": "session_status",
                            "resumed": resumed,
                            "conversation_name": (conv or {}).get("name"),
                            "model_name": model_name,
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"[ws_session] send_json failed: {exc}")
                continue
            except WebSocketDisconnect:
                break
            except Exception as e:  # noqa: BLE001
                print(f"[ws_session] outer exception: {type(e).__name__}: {e}")
                try:
                    await websocket.send_json(_connect_failure_payload(e, str(e)))
                except (WebSocketDisconnect, RuntimeError) as exc:
                    print(f"[ws_session] send_json failed: {exc}")
                break
    finally:
        # sender.attach()/clear() calls throughout the loop above always ran
        # at least once by the time we reach here - this finally sits after
        # the while loop, which always runs its body at least once before
        # any path that reaches this point.
        await flush_turn_buffer_to_memory(sender)
        if profile.get("id"):
            # Best-effort. Backed by memory.add_seconds_studied (memory.db's
            # profile_state table) - a single atomic SQL
            # UPDATE ... SET x = x + ? rather than a read-modify-write, so
            # this can't lose a concurrent update and needs no defensive
            # re-fetch of the profile first the way the old profiles.json
            # version did (see memory.py's own docstring on that function,
            # and its module docstring for why this moved out of
            # profiles.json at all).
            #
            # Deliberately runs BEFORE summarize_conversation below, not
            # after: this is a fast, local-only SQLite write (milliseconds),
            # while summarize_conversation is a slow, network-dependent
            # Gemini call (seconds) with an outer bound on how much time
            # this whole `finally` block gets to run when the app window is
            # what triggered this disconnect (see desktop.py's
            # _shut_down_server_gracefully). If that bound is ever actually
            # hit, this ordering means the fast/critical write already
            # landed and it's only the slower, purely-additive summary fold
            # that gets cut short - never the reverse.
            try:
                elapsed_seconds = int(time.monotonic() - session_start_monotonic)
                if elapsed_seconds > 0:
                    memory.add_seconds_studied(profile["id"], elapsed_seconds)
            except Exception as exc:  # noqa: BLE001
                print(f"[ws_session] total_seconds_studied update failed: {exc}")
        if conv is not None:
            # Final fold on disconnect - catches the tail so a session that
            # ends mid-way through a summarization interval isn't lost, and
            # is cheap/best-effort like every other summarization call. Not
            # a data-loss risk even if this never completes (a shutdown
            # timeout, an API error, offline) - every turn is already safe
            # in SQLite via flush_turn_buffer_to_memory above; this only
            # ever tops up the derived rolling summary, which just catches
            # up on the next fold if it's skipped this time.
            try:
                await asyncio.to_thread(
                    summarize_conversation,
                    conv["id"],
                    profile.get("name") or "the student",
                    profile.get("api_key"),
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[ws_session] summarize_conversation failed: {exc}")
        try:
            await live_cm.__aexit__(None, None, None)
        except Exception as exc:  # noqa: BLE001
            print(f"[ws_session] live_cm.__aexit__ failed: {exc}")
        try:
            await websocket.close()
        except Exception as exc:  # noqa: BLE001
            print(f"[ws_session] websocket.close failed: {exc}")
