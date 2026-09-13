"""Periodic rolling-summary folding for a conversation's transcript. Split
out of main.py; called from live_session.py both periodically (every
memory.SUMMARY_FOLD_EVERY_N_TURNS turns) and once more on disconnect.
"""

import json
import traceback

from google.genai import types

from . import memory, retry
from .constants import SUMMARY_MODEL
from .profiles_store import get_client_for_key


def summarize_conversation(conversation_id: str, student_name: str, api_key: str | None) -> None:
    """Folds turns since the last summary into an updated rolling summary,
    and - from the same call, since it's already looking at the transcript -
    upserts any vocabulary/recurring-mistake terms into vocab_mistakes and
    appends one lesson_log line for this batch of turns.

    Best-effort throughout: any failure here is swallowed and just logged -
    the raw turns stay in SQLite either way, so nothing is lost, and the
    next due summarization attempt (or the final one on disconnect) can
    retry. A malformed/non-JSON model response degrades to "summary only"
    (see the parsing fallback below) rather than losing the summary too.
    A profile with no API key set just skips this - the raw turns are still
    safe in SQLite for whenever a key gets added.
    """
    try:
        client = get_client_for_key(api_key)
    except ValueError:
        print(f"[summarize_conversation] skipped for conversation={conversation_id!r}: no API key set for this profile.")
        return

    try:
        prev = memory.get_summary(conversation_id)
        since_seq = prev["based_on_turn"] if prev else 0
        new_turns = memory.get_turns(conversation_id, since_seq=since_seq)
        if not new_turns:
            return
        transcript_lines = "\n".join(f"{t['role']}: {t['text']}" for t in new_turns)
        prompt = (
            "You maintain memory for an ongoing language-tutoring conversation "
            f"with {student_name}. Given the previous rolling summary and the "
            "new turns below, respond with ONLY a JSON object (no markdown "
            "fences, no commentary) shaped exactly like this:\n"
            '{"summary": "...", "vocab": [{"term": "...", "note": "..."}], "lesson_note": "..."}\n\n'
            "- summary: the previous summary updated to fold in the new turns. "
            "Compact (roughly 150-250 words), plain prose notes (not a "
            "transcript, not addressed to anyone): topics covered, recurring "
            "mistakes in context, open threads to follow up on. The specific "
            "vocabulary taught doesn't need to be enumerated here - it's tracked "
            "separately in full via the vocab list below. This "
            "tutoring relationship is ongoing and open-ended - never phrase any "
            "part of the summary as though a session/lesson 'ended,' 'concluded,' "
            "or will 'continue later'; describe progress neutrally (e.g. 'covered "
            "greetings and past tense' not 'the lesson wrapped up after covering...').\n"
            "- vocab: EVERY notable vocabulary word/phrase or recurring mistake "
            "taught or corrected in this batch (not the whole summary) - don't cap "
            "yourself to a handful if more genuinely came up, and don't include "
            "anything the student already knew coming in. term is the word/phrase "
            "itself; note is a few words of context (meaning, or what correction "
            "it needed). Omit if nothing notable came up.\n"
            "- lesson_note: one short sentence describing what this batch of "
            "turns covered - phrased as a log entry, e.g. 'Practiced ordering "
            "food at a restaurant, worked on past tense.'\n\n"
            f"PREVIOUS SUMMARY:\n{prev['summary'] if prev else '(none yet - this is the first summary)'}\n\n"
            f"NEW TURNS:\n{transcript_lines}"
        )
        response = retry.call_with_retry(
            client.models.generate_content,
            model=SUMMARY_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
            label=f"summarize_conversation/{conversation_id}",
        )
        raw_text = (getattr(response, "text", None) or "").strip()
        if not raw_text:
            return

        summary_text = None
        vocab_items = []
        lesson_note = None
        try:
            parsed = json.loads(raw_text)
            summary_text = (parsed.get("summary") or "").strip() or None
            vocab_items = parsed.get("vocab") or []
            lesson_note = (parsed.get("lesson_note") or "").strip() or None
        except (json.JSONDecodeError, AttributeError):
            # Model didn't follow the JSON contract - fall back to treating
            # the whole response as the summary, so at least that half of
            # the memory update still lands.
            summary_text = raw_text

        if summary_text:
            memory.upsert_summary(conversation_id, summary_text, new_turns[-1]["seq"])
        for item in vocab_items:
            if isinstance(item, dict) and item.get("term"):
                memory.upsert_vocab_mistake(conversation_id, str(item["term"]), item.get("note"))
        if lesson_note:
            memory.append_lesson_log(conversation_id, lesson_note)

        if summary_text:
            print(
                f"[summarize_conversation] updated summary for conversation={conversation_id!r} (through turn {new_turns[-1]['seq']}, +{len(vocab_items)} vocab items)"
            )
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as e:
        print(f"[summarize_conversation] skipped for conversation={conversation_id!r}: {type(e).__name__}: {e}")
        traceback.print_exc()
