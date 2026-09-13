"""Tests for tutor_tools.py's tool schemas. MOOD_TOOL is a plain constant
so needs no test; build_quiz_tool is a function with string .format()
calls that could break on an unescaped '{' typo in a future edit (exactly
what happened to need fixing for design_plans/issues.md #4's literal
'{0}' leak, when the question field's description was rewritten to
include an escaped example) - a cheap thing worth a regression test for.

Per this project's own convention, these test STRUCTURE (the schema
builds without raising, every declared field exists, dynamic values get
interpolated) - never literal prompt wording, which changes freely and
shouldn't break a test suite.
"""

import pytest

from thirtytutors.tutor_tools import build_quiz_tool

pytestmark = pytest.mark.unit


def test_build_quiz_tool_does_not_raise_for_various_languages():
    # Regression guard: every {native_language}/{target_language}
    # substitution, and any literal example placeholder text in a field
    # description, must be escaped correctly for .format() - an unescaped
    # brace raises KeyError/IndexError at call time, not at import time,
    # so this is the only thing that would actually catch it.
    for native, target in [("English", "Spanish"), ("Polish", "Japanese"), ("Arabic (Egyptian)", "French")]:
        build_quiz_tool(native_language=native, target_language=target)


def test_build_quiz_tool_item_schema_has_every_required_field():
    tool = build_quiz_tool(native_language="English", target_language="Spanish")
    item_schema = tool.function_declarations[0].parameters.properties["items"].items
    expected_fields = {
        "target_term",
        "question",
        "item_type",
        "choices",
        "correct_choice_index",
        "text_with_blanks",
        "correct_answers",
        "word_bank",
    }
    assert set(item_schema.properties.keys()) == expected_fields
    assert set(item_schema.required) == expected_fields


def test_build_quiz_tool_interpolates_the_given_languages():
    # Structural, not literal-wording: confirms the native_language
    # placeholder actually got substituted with the given DYNAMIC VALUE
    # (not left as a raw '{native_language}' token, which would mean the
    # .format() call silently broke) - not an assertion about the
    # instructional wording itself.
    tool = build_quiz_tool(native_language="Polish", target_language="Japanese")
    item_props = tool.function_declarations[0].parameters.properties["items"].items.properties
    assert "Polish" in item_props["question"].description
    assert "{native_language}" not in item_props["question"].description
    assert "Japanese" in item_props["choices"].description
    assert "{target_language}" not in item_props["choices"].description
