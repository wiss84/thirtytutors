"""Tool schemas (types.Tool/FunctionDeclaration) the tutor model can call -
what a call is named and what parameters it accepts, nothing else.

Deliberately separate from tutor_instructions.py: a schema only constrains
the SHAPE of a tool call (field names, types, enums) - Gemini's function-
calling can't emit a field that isn't declared here regardless of what any
prompt says, so judgment about WHEN to call a tool and HOW to choose good
values for its fields belongs in prose instructions instead, where it can
actually be read and followed rather than just validated against. See
tutor_instructions.py's CONVERSATIONAL_RULES/GUARDRAILS for that guidance -
each tool description below only carries an **Invocation Condition:**
clause (Google's own recommended pattern, see ai.google.dev/gemini-api/
docs/live-api/best-practices), not the full behavior explanation.

QUIZ_TOOL - both quiz mechanics
share ONE flat, fully-required field set per item, disambiguated by the
required item_type field, rather than two mechanic-specific optional field
sets. Gemini's schema has no way to express "field X is required only
when field Y equals Z" (no conditional/if-then support) - the only way to
guarantee a field like correct_answers is actually present is to make it
required unconditionally. The irrelevant fields for a given item_type are
still required, just filled with an empty placeholder (see each field's
description) - a mixed quiz (some multiple_choice items, some
fill_blank_dragdrop items, one start_quiz call) still works exactly as
before, since item_type is per-item, not per-call. There is deliberately
no top-level quiz_type parameter anymore - it's computed server-side from
the items' own item_type values (see live_session.py) purely as a DB
label, so the model has one less thing to get right.

QUIZ_TOOL is a function, not a module-level constant, unlike MOOD_TOOL:
its question/choices/word_bank field descriptions need the actual
native/target language names substituted in (same {native_language}/
{target_language} placeholder + .format() pattern as
tutor_instructions.build_system_instruction, for the same reason - a
literal "Native Language"/"Target Language" string in the description
Gemini reads is a much weaker constraint than "Spanish"/"French"). Call
build_quiz_tool(native_language=..., target_language=...) once per
build_config call (see live_session.py) rather than importing a constant.
MOOD_TOOL has no language-dependent text, so it stays a plain constant.
"""

from google.genai import types

MOOD_TOOL = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="set_mood",
            description=(
                "Silently express the tutor's emotional reaction to the "
                "current moment in the conversation, so the avatar's face "
                "reflects it."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "mood": types.Schema(
                        type="STRING",
                        enum=["neutral", "happy", "sad", "fear", "love"],
                    )
                },
                required=["mood"],
            ),
        )
    ]
)

# Field-description templates for build_quiz_tool below - kept as their
# own named constants (rather than inlined) purely so the {native_language}/
# {target_language} placeholders are easy to spot, same reasoning as
# tutor_instructions.py keeping each templated string separately named.
_QUESTION_DESC_TEMPLATE = (
    "A short question or context line for the student, fully in {native_language}. Descriptive enough to "
    "understand on its own, and must NEVER contain any correct_answers value. For a fill_blank_dragdrop item "
    "specifically: this field is ONLY the introductory context/instruction sentence (for example: 'You meet "
    "someone in the morning. Complete the greeting:') - it must NEVER also repeat the sentence from "
    "text_with_blanks, and must NEVER contain a literal placeholder like {{0}} or {{1}}. Those placeholders "
    "belong ONLY inside text_with_blanks, which the app renders as an interactive blank automatically, "
    "directly below this question - writing them here shows the raw, un-rendered token to the student instead."
)
_CHOICES_DESC_TEMPLATE = (
    "multiple_choice: 2+ answer options. fill_blank_dragdrop: empty array []. Must be fully in {target_language}."
)
_WORD_BANK_DESC_TEMPLATE = (
    "fill_blank_dragdrop: every correct_answers entry plus 1-3 "
    "distractors, shuffled - never in correct-answer order. "
    "multiple_choice: empty array []. Must be fully in {target_language}."
)


def build_quiz_tool(*, native_language: str, target_language: str) -> types.Tool:
    """Builds QUIZ_TOOL with the question/choices/word_bank field
    descriptions filled in for this conversation's actual languages - see
    this module's docstring for why that's a function call per
    build_config rather than a module-level constant."""
    fmt_kwargs = {"native_language": native_language, "target_language": target_language}
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="start_quiz",
                description=(
                    "Opens a quiz for the student to check understanding, "
                    "separate from the spoken corrections you already do. "
                    "Each item can be either quiz mechanic - see item_type.\n"
                    "**Invocation Condition:** Invoke this tool *only after* "
                    "you have said out loud, in this same turn, that it's "
                    "time for a quiz - never silently, and never a the first "
                    "thing you do on a new topic, or at the start of a conversation. "
                    "The entire quiz - every question, choice, blank, and correct answer - comes from "
                    "your own arguments to this tool, not from the app. "
                    "IMPORTANT: This tool must never be invoked at the same turn as spoken question, you either ask the student to repeat a word or you invoke this tool, but not both in the same turn."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "items": types.Schema(
                            type="ARRAY",
                            items=types.Schema(
                                type="OBJECT",
                                properties={
                                    "target_term": types.Schema(
                                        type="STRING",
                                        description="The word or phrase this item tests.",
                                    ),
                                    "question": types.Schema(
                                        type="STRING",
                                        description=_QUESTION_DESC_TEMPLATE.format(**fmt_kwargs),
                                    ),
                                    "item_type": types.Schema(
                                        type="STRING",
                                        enum=["multiple_choice", "fill_blank_dragdrop"],
                                        description="Which quiz mechanic this item uses - the single source of truth for how the app renders it.",
                                    ),
                                    "choices": types.Schema(
                                        type="ARRAY",
                                        items=types.Schema(type="STRING"),
                                        description=_CHOICES_DESC_TEMPLATE.format(**fmt_kwargs),
                                    ),
                                    "correct_choice_index": types.Schema(
                                        type="INTEGER",
                                        description="multiple_choice: index into choices. fill_blank_dragdrop: 0 (ignored).",
                                    ),
                                    "text_with_blanks": types.Schema(
                                        type="STRING",
                                        description="fill_blank_dragdrop: sentence(s) with blanks marked {0}, {1}, etc. multiple_choice: empty string.",
                                    ),
                                    "correct_answers": types.Schema(
                                        type="ARRAY",
                                        items=types.Schema(type="STRING"),
                                        description="fill_blank_dragdrop: REQUIRED - exactly one entry per {N} blank in text_with_blanks, in order, spelled identically to the matching word_bank entry. multiple_choice: empty array [].",
                                    ),
                                    "word_bank": types.Schema(
                                        type="ARRAY",
                                        items=types.Schema(type="STRING"),
                                        description=_WORD_BANK_DESC_TEMPLATE.format(**fmt_kwargs),
                                    ),
                                },
                                required=[
                                    "target_term",
                                    "question",
                                    "item_type",
                                    "choices",
                                    "correct_choice_index",
                                    "text_with_blanks",
                                    "correct_answers",
                                    "word_bank",
                                ],
                            ),
                        ),
                    },
                    required=["items"],
                ),
            )
        ]
    )
