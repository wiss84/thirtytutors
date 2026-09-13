"""Read-only aggregation over existing memory.py/quizzes.py tables for the
Settings modal's Stats tab, plus milestone-crossing detection for the
notification bell. No new tracking tables here beyond memory.py's own
profile_state table - the only genuinely new state is
total_seconds_studied/last_active_date/current_streak/seen_milestones
(memory.py's profile_state fields, written by live_session.py via
memory.record_active_day/add_seconds_studied and by get_new_milestones
below via memory.add_seen_milestones); everything else in this module is
computed fresh from turns/vocab_mistakes/quiz_sessions on every call rather
than cached, since a self-hosted single-profile app never has enough rows
for that to matter.
"""

from . import memory, quizzes

# A term counts as "mastered" once its correct_streak reaches this -
# reuses memory.py's own threshold (the same one that retires a term from
# get_review_candidates), so "mastered" here means the same thing it does
# everywhere else in the app rather than a second, competing definition.
_MASTERED_STREAK = 2

# Vocabulary milestones fire every N words mastered, per language.
VOCAB_MILESTONE_STEP = 50

# Day-streak milestones - a fixed tier list rather than every-N, since a
# streak realistically only ever needs a handful of celebrated waypoints.
STREAK_MILESTONE_TIERS = (3, 7, 14, 30, 60, 100, 200, 365)


def _new_language_bucket(target_language: str) -> dict:
    return {
        "target_language": target_language,
        "conversations": 0,
        "user_turns": 0,
        "vocab_mastered": 0,
        "review_candidates": 0,
        "quiz_sessions_completed": 0,
        "quiz_correct_items": 0,
        "quiz_total_items": 0,
        "perfect_quizzes": 0,
        "review_sessions_completed": 0,
    }


def get_profile_stats(profile_id: str) -> dict:
    """Per-language breakdown plus profile-wide totals, for the Stats tab.

    "review" quiz_type sessions (Test Yourself - see
    quizzes.get_reviewable_quiz_items) are deliberately excluded from
    quiz_sessions_completed/quiz_correct_items/quiz_total_items/
    perfect_quizzes: those numbers are meant to reflect performance on
    material as the tutor first taught it, and a review run is by
    definition re-testing something already covered, not new evidence of
    learning. Review activity isn't hidden entirely, though - it gets its
    own separate review_sessions_completed count per language instead of
    being silently folded into or excluded from the rest.
    """
    conversations = memory.list_conversations(profile_id)
    languages: dict[str, dict] = {}

    for conv in conversations:
        lang = (conv.get("config") or {}).get("target_language") or "Unknown"
        bucket = languages.setdefault(lang, _new_language_bucket(lang))
        bucket["conversations"] += 1

        bucket["user_turns"] += sum(1 for t in memory.get_turns(conv["id"]) if t["role"] == "user")
        bucket["vocab_mastered"] += sum(
            1 for v in memory.get_vocab_mistakes(conv["id"]) if v["correct_streak"] >= _MASTERED_STREAK
        )
        bucket["review_candidates"] += len(memory.get_review_candidates(conv["id"], limit=10_000))

        sessions = [s for s in quizzes.get_quiz_sessions(conv["id"]) if s["status"] == "completed"]
        real_quizzes = [s for s in sessions if s["quiz_type"] != "review"]
        review_quizzes = [s for s in sessions if s["quiz_type"] == "review"]

        bucket["quiz_sessions_completed"] += len(real_quizzes)
        bucket["review_sessions_completed"] += len(review_quizzes)
        bucket["quiz_correct_items"] += sum(s["correct_items"] for s in real_quizzes)
        bucket["quiz_total_items"] += sum(s["total_items"] for s in real_quizzes)
        bucket["perfect_quizzes"] += sum(
            1 for s in real_quizzes if s["total_items"] > 0 and s["correct_items"] == s["total_items"]
        )

    for bucket in languages.values():
        total = bucket["quiz_total_items"]
        bucket["quiz_accuracy_pct"] = round(bucket["quiz_correct_items"] / total * 100) if total else None
        # Progress toward the NEXT vocab-milestone tier, for a progress bar
        # rather than just a raw count - e.g. 37 mastered -> 37/50 toward
        # the next tier at 50, not "toward 100".
        mastered = bucket["vocab_mastered"]
        bucket["vocab_next_tier"] = ((mastered // VOCAB_MILESTONE_STEP) + 1) * VOCAB_MILESTONE_STEP

    return {
        "languages": sorted(languages.values(), key=lambda b: b["target_language"]),
        "languages_practiced": len(languages),
        "conversations_total": len(conversations),
        "review_candidates_total": sum(b["review_candidates"] for b in languages.values()),
    }


def _compute_earned_milestones(stats: dict, state: dict) -> list[dict]:
    """Every milestone currently earned - vocab tiers per language plus the
    single highest day-streak tier reached. Each fires at most once ever
    for a given profile (see get_new_milestones), including a streak tier:
    re-crossing the same tier after a streak resets doesn't re-notify -
    simpler and more consistent than tracking separate streak "runs", and
    avoids the streak-loss framing this app already deliberately avoids
    elsewhere.
    """
    earned = []
    for bucket in stats["languages"]:
        tier = (bucket["vocab_mastered"] // VOCAB_MILESTONE_STEP) * VOCAB_MILESTONE_STEP
        if tier > 0:
            lang = bucket["target_language"]
            earned.append({"id": f"vocab:{lang}:{tier}", "message": f"{tier} words mastered in {lang}"})

    streak = state.get("current_streak") or 0
    reached = [t for t in STREAK_MILESTONE_TIERS if streak >= t]
    if reached:
        top = max(reached)
        earned.append({"id": f"streak:{top}", "message": f"{top}-day streak"})

    return earned


def get_new_milestones(profile_id: str) -> list[dict]:
    """Milestones earned but not yet recorded in profile_state's
    seen_milestones (memory.py). Marks them seen as a side effect of being
    returned (merges their ids into seen_milestones via
    memory.add_seen_milestones) - same "surfaces once, then done" idea as
    updater.mark_version_seen for the what's-new notification, just
    triggered by this read itself rather than a dedicated detail page,
    since a milestone has no page of its own to visit - the Settings
    modal's Stats tab is where the underlying numbers live, not a one-off
    "you earned this" view.
    """
    stats = get_profile_stats(profile_id)
    state = memory.get_profile_state(profile_id)
    earned = _compute_earned_milestones(stats, state)
    seen = set(state.get("seen_milestones") or [])
    new = [m for m in earned if m["id"] not in seen]
    if new:
        memory.add_seen_milestones(profile_id, {m["id"] for m in new})
    return new
