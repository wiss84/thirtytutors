"""Integration tests for /ws/session's message routing (live_session.py) -
the one explicitly called out as an example test target. Runs the real
route against a minimal FastAPI app, but with the Gemini client itself
faked out entirely (FakeClient/FakeLiveSession below) - nothing here
touches the network or needs a real API key.

Scope note: this covers the core push-to-talk turn flow, session-status
reporting, resumption-handle storage, the go_away mid-session reconnect
path, and that reconnect's buffered-audio replay excludes audio from
turns that already completed (see
test_go_away_reconnects_without_closing_browser_socket and
test_go_away_replay_excludes_audio_from_a_turn_that_already_completed
below) - plus the equivalent guard for an abrupt mid-session error/drop
(not go_away) that happens while a turn is already awaiting Gemini's
response (see
test_mid_response_drop_discards_buffered_audio_instead_of_replaying), and
ResumableSender's own indexing/pruning/replay logic in isolation (see the
tests below that section). It does NOT cover hands-free mode's own
turn-boundary logic or dead-resumption-handle (1008) retry - those would
need considerably more elaborate fakes and are left for a follow-up
rather than rushed here.
"""

import asyncio
import base64
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from thirtytutors import live_session, memory, quizzes

pytestmark = pytest.mark.integration


class FakeLiveSession:
    """Records everything sent to it, and yields a scripted batch of fake
    Gemini responses (once) from receive(). Every subsequent call to
    receive() blocks (awaiting an Event that's never set) instead of
    returning immediately - matching how the real API's receive() behaves
    while genuinely waiting for more data, and avoiding a busy-loop that
    would otherwise starve the event loop once the scripted responses run
    out (see the comment inside receive() below - this bit the first draft
    of this file for real, as a several-minutes-long apparent hang).
    """

    def __init__(self, responses=None, wait_until=None, raise_after=None):
        self._responses = responses or []
        self._served = False
        self._exhausted = asyncio.Event()
        self.sent_realtime_inputs = []
        self.sent_tool_responses = []
        self.sent_client_contents = []
        # Optional exception instance - raised (instead of hanging) right
        # after the scripted responses run out, simulating an abrupt
        # mid-session drop (a genuine error, not go_away) for reconnect
        # tests that need one.
        self._raise_after = raise_after
        # Optional zero-arg callable - if given, receive() polls it (async-
        # sleeping between checks, same event loop/task-set as everything
        # else here) before yielding ANY scripted response. Without this,
        # scripted responses (e.g. go_away) fire essentially immediately
        # once live_to_browser starts, with no guarantee that a test's own
        # client-sent messages (audio_chunk etc., processed concurrently by
        # browser_to_live) have been forwarded to THIS session yet - a real
        # race, not just a timing nicety, for any test that needs "this
        # session actually received turn X's audio before go_away fires".
        self._wait_until = wait_until

    async def send_realtime_input(self, **kwargs):
        self.sent_realtime_inputs.append(kwargs)

    async def send_tool_response(self, function_responses):
        self.sent_tool_responses.append(function_responses)

    async def send_client_content(self, **kwargs):
        self.sent_client_contents.append(kwargs)

    async def receive(self):
        if not self._served:
            if self._wait_until is not None:
                while not self._wait_until():
                    await asyncio.sleep(0.01)
            self._served = True
            for r in self._responses:
                yield r
            if self._raise_after is not None:
                raise self._raise_after
        else:
            # Nothing left scripted. A plain `return` here would make the
            # outer `while True: async for response in live_session.receive():`
            # loop in live_to_browser call receive() again immediately, over
            # and over, with no actual suspension in between - a busy loop
            # that starves the event loop and hangs the whole test (this is
            # exactly what happened on first run). Awaiting an Event that's
            # never set is a real suspension instead: the task sits idle
            # until it's cancelled once browser_to_live finishes (the
            # asyncio.wait(..., FIRST_COMPLETED) + task.cancel() in
            # live_session.py's own loop), which is exactly how the real
            # API's receive() behaves while genuinely waiting for more data.
            await self._exhausted.wait()


class FakeLiveConnectCM:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc_info):
        return False


class FakeClient:
    """Stands in for genai.Client - only the .aio.live.connect(...) surface
    live_session.py actually uses."""

    def __init__(self, session):
        self.aio = SimpleNamespace(live=SimpleNamespace(connect=lambda model, config: FakeLiveConnectCM(session)))


def _server_content(input_text=None, output_text=None, turn_complete=False):
    return SimpleNamespace(
        input_transcription=SimpleNamespace(text=input_text) if input_text else None,
        output_transcription=SimpleNamespace(text=output_text) if output_text else None,
        turn_complete=turn_complete,
    )


def _response(
    server_content=None,
    data=None,
    go_away=None,
    tool_call=None,
    session_resumption_update=None,
):
    return SimpleNamespace(
        server_content=server_content,
        data=data,
        go_away=go_away,
        tool_call=tool_call,
        session_resumption_update=session_resumption_update,
    )


def _function_call(name, args, call_id="fc-1"):
    return SimpleNamespace(name=name, args=args, id=call_id)


def _tool_call(*function_calls):
    return SimpleNamespace(function_calls=list(function_calls))


def _quiz_payload(n_items=2):
    """Shaped like a real start_quiz tool-call payload under the current,
    normalized QUIZ_TOOL schema (tutor_tools.py) - every item carries all 8
    fields regardless of item_type."""
    return {
        "items": [
            {
                "target_term": f"term-{i}",
                "question": f"Question {i}?",
                "item_type": "fill_blank_dragdrop",
                "choices": [],
                "correct_choice_index": 0,
                "text_with_blanks": f"Sentence with a blank {{0}} number {i}.",
                "correct_answers": [f"answer-{i}"],
                "word_bank": [f"answer-{i}", "distractor"],
            }
            for i in range(n_items)
        ],
    }


@pytest.fixture
def ws_app(monkeypatch):
    """Wires a FakeClient (with no scripted responses by default - tests
    override via the fake_session fixture below) into live_session.py and
    returns a TestClient for a minimal app exposing just /ws/session.
    Also stubs out summarize_conversation entirely: the code path that
    calls it on disconnect always runs regardless of turn count (see
    live_session.py's `finally` block), and a real call would try to reach
    Gemini for real with whatever fake API key the test profile has.
    """
    monkeypatch.setattr(live_session, "summarize_conversation", lambda *a, **k: None)

    def _make(fake_session: FakeLiveSession):
        monkeypatch.setattr(live_session, "get_client_for_key", lambda api_key: FakeClient(fake_session))
        app = FastAPI()
        app.include_router(live_session.router)
        return TestClient(app)

    return _make


def test_normal_turn_flow_routes_messages_and_persists_transcript(ws_app, make_profile, make_conversation):
    profile = make_profile(api_key="fake-key")
    conv = make_conversation(profile["id"], target_language="Spanish")

    fake_session = FakeLiveSession(
        responses=[
            _response(server_content=_server_content(input_text="hola")),
            _response(server_content=_server_content(output_text="\u00a1hola! \u00bfc\u00f3mo est\u00e1s?")),
            _response(server_content=_server_content(turn_complete=True)),
        ]
    )
    client = ws_app(fake_session)

    with client.websocket_connect("/ws/session") as ws:
        ws.send_json(
            {
                "type": "init",
                "profile_id": profile["id"],
                "profile_name": profile["name"],
                "conversation_id": conv["id"],
            }
        )

        status = ws.receive_json()
        assert status["type"] == "session_status"
        assert status["resumed"] is False  # brand-new conversation, no stored handle
        assert status["model_name"]

        assert ws.receive_json() == {"type": "transcript_in", "text": "hola"}
        assert ws.receive_json() == {
            "type": "transcript_out",
            "text": "\u00a1hola! \u00bfc\u00f3mo est\u00e1s?",
        }
        assert ws.receive_json() == {"type": "turn_complete"}

        # Drive the push-to-talk side - this is what actually exercises
        # browser_to_live's message routing into send_realtime_input.
        ws.send_json({"type": "start_turn"})
        audio_b64 = base64.b64encode(b"\x00\x01" * 50).decode()
        ws.send_json({"type": "audio_chunk", "data": audio_b64})
        ws.send_json({"type": "turn_complete"})
        ws.send_json({"type": "close"})

    # The scripted transcript was flushed to memory on turn_complete:
    turns = memory.get_turns(conv["id"])
    assert [(t["role"], t["text"]) for t in turns] == [
        ("user", "hola"),
        ("tutor", "\u00a1hola! \u00bfc\u00f3mo est\u00e1s?"),
    ]

    # And the push-to-talk sequence sent exactly activity_start -> audio -> activity_end:
    kinds = [next(iter(call.keys())) for call in fake_session.sent_realtime_inputs]
    assert kinds == ["activity_start", "audio", "activity_end"]
    assert fake_session.sent_realtime_inputs[1]["audio"].data == base64.b64decode(audio_b64)


def test_session_resumption_update_is_stored(ws_app, make_profile, make_conversation):
    profile = make_profile(api_key="fake-key")
    conv = make_conversation(profile["id"], target_language="Spanish")

    fake_session = FakeLiveSession(
        responses=[
            _response(session_resumption_update=SimpleNamespace(resumable=True, new_handle="fake-handle-123")),
            _response(server_content=_server_content(turn_complete=True)),
        ]
    )
    client = ws_app(fake_session)

    with client.websocket_connect("/ws/session") as ws:
        ws.send_json(
            {
                "type": "init",
                "profile_id": profile["id"],
                "profile_name": profile["name"],
                "conversation_id": conv["id"],
            }
        )
        ws.receive_json()  # session_status
        ws.receive_json()  # turn_complete
        ws.send_json({"type": "close"})

    stored = memory.get_conversation(conv["id"])
    assert stored["resumption_handle"] == "fake-handle-123"


def test_non_resumable_update_is_not_stored(ws_app, make_profile, make_conversation):
    profile = make_profile(api_key="fake-key")
    conv = make_conversation(profile["id"], target_language="Spanish")

    fake_session = FakeLiveSession(
        responses=[
            _response(session_resumption_update=SimpleNamespace(resumable=False, new_handle=None)),
            _response(server_content=_server_content(turn_complete=True)),
        ]
    )
    client = ws_app(fake_session)

    with client.websocket_connect("/ws/session") as ws:
        ws.send_json(
            {
                "type": "init",
                "profile_id": profile["id"],
                "profile_name": profile["name"],
                "conversation_id": conv["id"],
            }
        )
        ws.receive_json()
        ws.receive_json()
        ws.send_json({"type": "close"})

    assert memory.get_conversation(conv["id"])["resumption_handle"] is None


def test_reconnect_resumes_when_config_unchanged(ws_app, make_profile, make_conversation):
    profile = make_profile(api_key="fake-key")
    conv = make_conversation(profile["id"], target_language="Spanish")
    # Simulate a resumption handle stored by an earlier session with the
    # exact same config this conversation still has:
    memory.set_resumption(conv["id"], "handle-from-before", conv["config"])

    fake_session = FakeLiveSession(responses=[_response(server_content=_server_content(turn_complete=True))])
    client = ws_app(fake_session)

    with client.websocket_connect("/ws/session") as ws:
        ws.send_json(
            {
                "type": "init",
                "profile_id": profile["id"],
                "profile_name": profile["name"],
                "conversation_id": conv["id"],
            }
        )
        status = ws.receive_json()
        assert status["resumed"] is True
        ws.receive_json()
        ws.send_json({"type": "close"})


def test_init_must_be_the_first_message(ws_app, make_profile):
    client = ws_app(FakeLiveSession())
    with client.websocket_connect("/ws/session") as ws:
        ws.send_json({"type": "start_turn"})
        msg = ws.receive_json()
        assert msg["type"] == "error"


def test_profile_with_no_conversations_errors_cleanly(ws_app, make_profile):
    profile = make_profile(api_key="fake-key")  # no make_conversation call - zero conversations
    client = ws_app(FakeLiveSession())

    with client.websocket_connect("/ws/session") as ws:
        ws.send_json(
            {
                "type": "init",
                "profile_id": profile["id"],
                "profile_name": profile["name"],
            }
        )
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "language" in msg["message"].lower()


def test_unknown_profile_id_falls_back_to_ephemeral_session(ws_app):
    """No persisted profile/conversation - config comes straight from the
    init message itself, and nothing gets written to memory.py (there's no
    conversation id to write against)."""
    fake_session = FakeLiveSession(responses=[_response(server_content=_server_content(turn_complete=True))])
    client = ws_app(fake_session)

    with client.websocket_connect("/ws/session") as ws:
        ws.send_json(
            {
                "type": "init",
                "profile_id": "does-not-exist",
                "profile_name": "Guest",
                "target_language": "Spanish",
            }
        )
        status = ws.receive_json()
        assert status["type"] == "session_status"
        assert status["resumed"] is False
        ws.receive_json()  # turn_complete
        ws.send_json({"type": "close"})


def test_missing_api_key_reports_friendly_error(make_profile, make_conversation):
    """Doesn't use the ws_app fixture - deliberately exercises the real
    get_client_for_key (profiles_store.py), unmocked, against a profile
    with no API key at all.
    """
    profile = make_profile(api_key=None)
    conv = make_conversation(profile["id"], target_language="Spanish")

    app = FastAPI()
    app.include_router(live_session.router)
    client = TestClient(app)

    with client.websocket_connect("/ws/session") as ws:
        ws.send_json(
            {
                "type": "init",
                "profile_id": profile["id"],
                "profile_name": profile["name"],
                "conversation_id": conv["id"],
            }
        )
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "API key" in msg["message"]


# --- Quiz tool-call routing, persistence, and resume ---


def test_start_quiz_tool_call_forwards_to_browser(ws_app, make_profile, make_conversation):
    profile = make_profile(api_key="fake-key")
    conv = make_conversation(profile["id"], target_language="Spanish")
    payload = _quiz_payload()

    fake_session = FakeLiveSession(
        responses=[
            _response(tool_call=_tool_call(_function_call("start_quiz", payload))),
            _response(server_content=_server_content(turn_complete=True)),
        ]
    )
    client = ws_app(fake_session)

    with client.websocket_connect("/ws/session") as ws:
        ws.send_json(
            {
                "type": "init",
                "profile_id": profile["id"],
                "profile_name": profile["name"],
                "conversation_id": conv["id"],
            }
        )
        ws.receive_json()  # session_status
        quiz_start = ws.receive_json()
        assert quiz_start["type"] == "quiz_start"
        assert len(quiz_start["items"]) == 2
        ws.receive_json()  # turn_complete
        ws.send_json({"type": "close"})

    assert fake_session.sent_tool_responses  # the ack was sent, same as set_mood
    in_progress = quizzes.get_in_progress_quiz(conv["id"])
    assert in_progress is not None
    assert in_progress["quiz_id"] == quiz_start["quiz_id"]
    # quiz_type is no longer part of the message (see tutor_tools.py) - it's
    # computed server-side from the items' own item_type and stored as a DB
    # label only, so this also exercises live_session._compute_quiz_type.
    assert in_progress["quiz_type"] == "fill_blank_dragdrop"


def test_duplicate_start_quiz_reuses_in_progress(ws_app, make_profile, make_conversation):
    profile = make_profile(api_key="fake-key")
    conv = make_conversation(profile["id"], target_language="Spanish")
    original_payload = _quiz_payload(n_items=1)
    original_id = quizzes.start_quiz_session(conv["id"], "fill_blank_dragdrop", original_payload)

    new_payload = _quiz_payload(n_items=3)  # what Gemini generated for a second, unwanted call
    fake_session = FakeLiveSession(
        responses=[
            _response(tool_call=_tool_call(_function_call("start_quiz", new_payload))),
            _response(server_content=_server_content(turn_complete=True)),
        ]
    )
    client = ws_app(fake_session)

    with client.websocket_connect("/ws/session") as ws:
        ws.send_json(
            {
                "type": "init",
                "profile_id": profile["id"],
                "profile_name": profile["name"],
                "conversation_id": conv["id"],
            }
        )
        ws.receive_json()  # session_status
        # An in-progress quiz already exists, so connecting resumes it before
        # the scripted tool call even fires - see quiz_resume below.
        resume_msg = ws.receive_json()
        assert resume_msg["type"] == "quiz_resume"
        assert resume_msg["quiz_id"] == original_id

        quiz_start = ws.receive_json()
        assert quiz_start["type"] == "quiz_start"
        assert quiz_start["quiz_id"] == original_id  # reused, not a new one
        assert len(quiz_start["items"]) == 1  # the original payload, not new_payload's 3
        ws.receive_json()  # turn_complete
        ws.send_json({"type": "close"})

    assert len(quizzes.get_quiz_sessions(conv["id"])) == 1  # no second row was created


def test_quiz_answer_persists_incrementally(ws_app, make_profile, make_conversation):
    profile = make_profile(api_key="fake-key")
    conv = make_conversation(profile["id"], target_language="Spanish")
    payload = _quiz_payload(n_items=2)

    fake_session = FakeLiveSession(
        responses=[
            _response(tool_call=_tool_call(_function_call("start_quiz", payload))),
            _response(server_content=_server_content(turn_complete=True)),
        ]
    )
    client = ws_app(fake_session)

    with client.websocket_connect("/ws/session") as ws:
        ws.send_json(
            {
                "type": "init",
                "profile_id": profile["id"],
                "profile_name": profile["name"],
                "conversation_id": conv["id"],
            }
        )
        ws.receive_json()  # session_status
        quiz_start = ws.receive_json()
        ws.receive_json()  # turn_complete

        ws.send_json(
            {
                "type": "quiz_answer",
                "quiz_id": quiz_start["quiz_id"],
                "item_index": 0,
                "target_term": "term-0",
                "prompt_or_text": "Sentence with a blank answer-0 number 0.",
                "correct_answer": "answer-0",
                "student_answer": "answer-0",
                "is_correct": True,
            }
        )
        ws.send_json({"type": "close"})

    in_progress = quizzes.get_in_progress_quiz(conv["id"])
    assert in_progress is not None  # not finalized - still resumable
    assert in_progress["status"] == "in_progress"
    assert in_progress["current_index"] == 1
    assert len(in_progress["answered_items"]) == 1


def test_quiz_done_finalizes_and_injects_summary(ws_app, make_profile, make_conversation):
    profile = make_profile(api_key="fake-key")
    conv = make_conversation(profile["id"], target_language="Spanish")
    payload = _quiz_payload(n_items=2)

    fake_session = FakeLiveSession(
        responses=[
            _response(tool_call=_tool_call(_function_call("start_quiz", payload))),
            _response(server_content=_server_content(turn_complete=True)),
        ]
    )
    client = ws_app(fake_session)

    with client.websocket_connect("/ws/session") as ws:
        ws.send_json(
            {
                "type": "init",
                "profile_id": profile["id"],
                "profile_name": profile["name"],
                "conversation_id": conv["id"],
            }
        )
        ws.receive_json()  # session_status
        quiz_start = ws.receive_json()
        ws.receive_json()  # turn_complete

        ws.send_json(
            {
                "type": "quiz_answer",
                "quiz_id": quiz_start["quiz_id"],
                "item_index": 0,
                "target_term": "term-0",
                "prompt_or_text": "p0",
                "correct_answer": "answer-0",
                "student_answer": "wrong",
                "is_correct": False,
            }
        )
        ws.send_json(
            {
                "type": "quiz_answer",
                "quiz_id": quiz_start["quiz_id"],
                "item_index": 1,
                "target_term": "term-1",
                "prompt_or_text": "p1",
                "correct_answer": "answer-1",
                "student_answer": "answer-1",
                "is_correct": True,
            }
        )
        ws.send_json({"type": "quiz_done", "quiz_id": quiz_start["quiz_id"]})
        ws.send_json({"type": "close"})

    sessions = quizzes.get_quiz_sessions(conv["id"])
    assert len(sessions) == 1
    assert sessions[0]["status"] == "completed"
    assert sessions[0]["correct_items"] == 1
    assert quizzes.get_in_progress_quiz(conv["id"]) is None  # no longer resumable

    mistakes = memory.get_vocab_mistakes(conv["id"])
    assert any(m["term"] == "term-0" for m in mistakes)

    assert fake_session.sent_client_contents  # a results turn was injected
    injected_text = fake_session.sent_client_contents[0]["turns"].parts[0].text
    assert "1/2" in injected_text
    assert "term-0" in injected_text


def test_reconnect_resumes_in_progress_quiz(ws_app, make_profile, make_conversation):
    profile = make_profile(api_key="fake-key")
    conv = make_conversation(profile["id"], target_language="Spanish")
    payload = _quiz_payload(n_items=2)
    quiz_id = quizzes.start_quiz_session(conv["id"], "fill_blank_dragdrop", payload)
    quizzes.record_item_answer(
        quiz_id,
        item_index=0,
        target_term="term-0",
        prompt_or_text="p0",
        correct_answer="answer-0",
        student_answer="answer-0",
        is_correct=True,
    )

    fake_session = FakeLiveSession(responses=[_response(server_content=_server_content(turn_complete=True))])
    client = ws_app(fake_session)

    with client.websocket_connect("/ws/session") as ws:
        ws.send_json(
            {
                "type": "init",
                "profile_id": profile["id"],
                "profile_name": profile["name"],
                "conversation_id": conv["id"],
            }
        )
        ws.receive_json()  # session_status
        resume_msg = ws.receive_json()
        assert resume_msg["type"] == "quiz_resume"
        assert resume_msg["quiz_id"] == quiz_id
        assert resume_msg["current_index"] == 1
        assert len(resume_msg["answered_items"]) == 1
        assert resume_msg["answered_items"][0]["target_term"] == "term-0"
        ws.receive_json()  # turn_complete
        ws.send_json({"type": "close"})


def test_quiz_skip_before_answering_injects_summary(ws_app, make_profile, make_conversation):
    profile = make_profile(api_key="fake-key")
    conv = make_conversation(profile["id"], target_language="Spanish")
    quiz_id = quizzes.start_quiz_session(conv["id"], "fill_blank_dragdrop", _quiz_payload())

    fake_session = FakeLiveSession(responses=[_response(server_content=_server_content(turn_complete=True))])
    client = ws_app(fake_session)

    with client.websocket_connect("/ws/session") as ws:
        ws.send_json(
            {
                "type": "init",
                "profile_id": profile["id"],
                "profile_name": profile["name"],
                "conversation_id": conv["id"],
            }
        )
        ws.receive_json()  # session_status
        ws.receive_json()  # quiz_resume
        ws.receive_json()  # turn_complete

        ws.send_json({"type": "quiz_skip", "quiz_id": quiz_id})
        ws.send_json({"type": "close"})

    sessions = quizzes.get_quiz_sessions(conv["id"])
    assert sessions[0]["status"] == "skipped"
    # Unlike an earlier version of this behavior, skip now always tells the
    # tutor the quiz ended - otherwise it has no way to know the quiz drawer
    # closed and can end up acting as if it's still waiting on results.
    assert fake_session.sent_client_contents
    injected_text = fake_session.sent_client_contents[0]["turns"].parts[0].text
    assert "skipped" in injected_text.lower()


def test_quiz_skip_after_partial_answers_reports_progress(ws_app, make_profile, make_conversation):
    profile = make_profile(api_key="fake-key")
    conv = make_conversation(profile["id"], target_language="Spanish")
    quiz_id = quizzes.start_quiz_session(conv["id"], "fill_blank_dragdrop", _quiz_payload(n_items=2))
    quizzes.record_item_answer(
        quiz_id,
        item_index=0,
        target_term="term-0",
        prompt_or_text="p0",
        correct_answer="answer-0",
        student_answer="answer-0",
        is_correct=True,
    )

    fake_session = FakeLiveSession(responses=[_response(server_content=_server_content(turn_complete=True))])
    client = ws_app(fake_session)

    with client.websocket_connect("/ws/session") as ws:
        ws.send_json(
            {
                "type": "init",
                "profile_id": profile["id"],
                "profile_name": profile["name"],
                "conversation_id": conv["id"],
            }
        )
        ws.receive_json()  # session_status
        ws.receive_json()  # quiz_resume
        ws.receive_json()  # turn_complete

        ws.send_json({"type": "quiz_skip", "quiz_id": quiz_id})
        ws.send_json({"type": "close"})

    injected_text = fake_session.sent_client_contents[0]["turns"].parts[0].text
    assert "1/1" in injected_text  # one answered, one correct, before skipping


def test_voice_input_gated_while_quiz_active(ws_app, make_profile, make_conversation):
    profile = make_profile(api_key="fake-key")
    conv = make_conversation(profile["id"], target_language="Spanish")
    payload = _quiz_payload()

    fake_session = FakeLiveSession(
        responses=[
            _response(tool_call=_tool_call(_function_call("start_quiz", payload))),
            _response(server_content=_server_content(turn_complete=True)),
        ]
    )
    client = ws_app(fake_session)

    with client.websocket_connect("/ws/session") as ws:
        ws.send_json(
            {
                "type": "init",
                "profile_id": profile["id"],
                "profile_name": profile["name"],
                "conversation_id": conv["id"],
            }
        )
        ws.receive_json()  # session_status
        ws.receive_json()  # quiz_start
        ws.receive_json()  # turn_complete

        ws.send_json({"type": "start_turn"})
        audio_b64 = base64.b64encode(b"\x00\x01" * 10).decode()
        ws.send_json({"type": "audio_chunk", "data": audio_b64})
        ws.send_json({"type": "turn_complete"})
        ws.send_json({"type": "close"})

    # Every voice message sent above was dropped rather than forwarded to Gemini:
    assert fake_session.sent_realtime_inputs == []


# --- go_away mid-session reconnect ---


def test_go_away_reconnects_without_closing_browser_socket(monkeypatch, make_profile, make_conversation):
    """go_away must not close the browser's own websocket (see
    live_session.py's module docstring): the old session finishes its
    pending turn, a fresh session is opened in its place, and a second
    session_status arrives - all on the SAME browser connection, with a
    second real connect() call underneath it.
    """
    monkeypatch.setattr(live_session, "summarize_conversation", lambda *a, **k: None)
    profile = make_profile(api_key="fake-key")
    conv = make_conversation(profile["id"], target_language="Spanish")

    first_session = FakeLiveSession(
        responses=[
            _response(go_away=SimpleNamespace(time_left="10s")),
            _response(server_content=_server_content(turn_complete=True)),
        ]
    )
    second_session = FakeLiveSession(responses=[_response(server_content=_server_content(turn_complete=True))])
    sessions = iter([first_session, second_session])

    class _MultiConnectClient:
        def __init__(self):
            self.connect_calls = 0
            self.aio = SimpleNamespace(live=SimpleNamespace(connect=self._connect))

        def _connect(self, model, config):
            self.connect_calls += 1
            return FakeLiveConnectCM(next(sessions))

    fake_client = _MultiConnectClient()
    monkeypatch.setattr(live_session, "get_client_for_key", lambda api_key: fake_client)

    app = FastAPI()
    app.include_router(live_session.router)
    client = TestClient(app)

    with client.websocket_connect("/ws/session") as ws:
        ws.send_json(
            {
                "type": "init",
                "profile_id": profile["id"],
                "profile_name": profile["name"],
                "conversation_id": conv["id"],
            }
        )
        status1 = ws.receive_json()
        assert status1["type"] == "session_status"
        assert ws.receive_json() == {"type": "turn_complete"}  # the go_away-pending turn wraps up first

        # The reconnect happens without the browser socket ever closing -
        # a second session_status arrives on the SAME websocket connection.
        status2 = ws.receive_json()
        assert status2["type"] == "session_status"
        assert status2.get("unavailable") is not True
        assert ws.receive_json() == {"type": "turn_complete"}  # the new session's own scripted turn

        ws.send_json({"type": "close"})

    assert fake_client.connect_calls == 2


def test_go_away_replay_excludes_audio_from_a_turn_that_already_completed(monkeypatch, make_profile, make_conversation):
    """Regression test for the buffer-accumulation bug: current_turn_chunks
    used to only ever get cleared by the NEXT start_turn, never by a turn
    actually completing (flush_turn_buffer_to_memory read its length for
    logging but never cleared it) - so a long-running connection kept
    accumulating every past turn's audio, and a later go_away/error
    reconnect replayed that whole growing pile as one garbled blob instead
    of just whatever was genuinely still mid-turn. Real push-to-talk audio
    sent for a turn that goes on to complete (server_content.turn_complete)
    BEFORE a go_away reconnect must NOT be replayed onto the new session -
    only audio from a turn still open when the connection died should be.
    """
    monkeypatch.setattr(live_session, "summarize_conversation", lambda *a, **k: None)
    profile = make_profile(api_key="fake-key")
    conv = make_conversation(profile["id"], target_language="Spanish")

    # Gates first_session's scripted go_away until the real turn sent below
    # has genuinely been forwarded to THIS session - without this, go_away
    # (scripted to fire as soon as live_to_browser starts) races ahead of
    # browser_to_live ever processing the client's queued messages at all,
    # which would make them land on the SECOND session as an ordinary new
    # turn instead of exercising the old-session-clears-its-buffer path
    # this test actually needs (see FakeLiveSession.wait_until above).
    first_session_ref = {}

    def _real_turn_forwarded():
        session = first_session_ref.get("session")
        return session is not None and len(session.sent_realtime_inputs) >= 6  # activity_start + 4x audio + activity_end

    first_session = FakeLiveSession(
        responses=[
            _response(go_away=SimpleNamespace(time_left="10s")),
            _response(server_content=_server_content(turn_complete=True)),  # go_away-pending wrap-up
        ],
        wait_until=_real_turn_forwarded,
    )
    first_session_ref["session"] = first_session
    second_session = FakeLiveSession(responses=[])
    sessions = iter([first_session, second_session])

    class _MultiConnectClient:
        def __init__(self):
            self.connect_calls = 0
            self.aio = SimpleNamespace(live=SimpleNamespace(connect=self._connect))

        def _connect(self, model, config):
            self.connect_calls += 1
            return FakeLiveConnectCM(next(sessions))

    fake_client = _MultiConnectClient()
    monkeypatch.setattr(live_session, "get_client_for_key", lambda api_key: fake_client)

    app = FastAPI()
    app.include_router(live_session.router)
    client = TestClient(app)

    with client.websocket_connect("/ws/session") as ws:
        ws.send_json(
            {
                "type": "init",
                "profile_id": profile["id"],
                "profile_name": profile["name"],
                "conversation_id": conv["id"],
            }
        )
        assert ws.receive_json()["type"] == "session_status"

        # A real, complete turn - go_away is gated (see wait_until above)
        # until browser_to_live has genuinely forwarded all of this to
        # first_session, so this is guaranteed processed by the OLD
        # session, not deferred to the new one by a race. The bug was that
        # this audio stayed in current_turn_chunks forever, well past this
        # turn actually completing.
        ws.send_json({"type": "start_turn"})
        audio_b64 = base64.b64encode(b"\x00\x01" * 50).decode()
        for _ in range(4):
            ws.send_json({"type": "audio_chunk", "data": audio_b64})
        ws.send_json({"type": "turn_complete"})

        # go_away's own wrap-up turn_complete - this is what runs
        # flush_turn_buffer_to_memory (and, with the fix, clears
        # current_turn_chunks) before the reconnect below.
        assert ws.receive_json() == {"type": "turn_complete"}

        status2 = ws.receive_json()
        assert status2["type"] == "session_status"

        ws.send_json({"type": "close"})

    assert fake_client.connect_calls == 2
    # The turn really did reach the OLD session first (proves this test
    # exercises the intended scenario, not a no-op):
    assert len(first_session.sent_realtime_inputs) == 6
    # The whole point: nothing from that already-completed turn was
    # replayed onto the new session.
    assert second_session.sent_realtime_inputs == []


# --- Mid-response abrupt-drop reconnect (issues.md #1: same-turn repeat) ---


def test_mid_response_drop_discards_buffered_audio_instead_of_replaying(monkeypatch, make_profile, make_conversation):
    """Regression test for the same-turn-repeat bug (design_plans/issues.md
    #1): if the connection dies with a genuine mid-session error (not
    go_away) WHILE the student's turn has already been fully sent to
    Gemini (activity_end sent - the tutor's response was mid-stream), the
    buffered audio for that turn must NOT be replayed onto the reconnected
    session - Gemini already received that exact turn, so replaying it
    makes the tutor answer the same question a second time, which is what
    surfaced as "the tutor repeats itself within the same turn". Whatever
    partial tutor text had already streamed in before the drop must still
    get flushed to memory, not silently carried forward into the next
    turn.
    """
    monkeypatch.setattr(live_session, "summarize_conversation", lambda *a, **k: None)
    profile = make_profile(api_key="fake-key")
    conv = make_conversation(profile["id"], target_language="Spanish")

    first_session_ref = {}

    def _real_turn_forwarded():
        session = first_session_ref.get("session")
        return session is not None and len(session.sent_realtime_inputs) >= 3  # activity_start + audio + activity_end

    first_session = FakeLiveSession(
        responses=[_response(server_content=_server_content(output_text="Partial answer before the drop"))],
        wait_until=_real_turn_forwarded,
        raise_after=ConnectionResetError("simulated mid-response drop"),
    )
    first_session_ref["session"] = first_session
    second_session = FakeLiveSession(responses=[_response(server_content=_server_content(turn_complete=True))])
    sessions = iter([first_session, second_session])

    class _MultiConnectClient:
        def __init__(self):
            self.connect_calls = 0
            self.aio = SimpleNamespace(live=SimpleNamespace(connect=self._connect))

        def _connect(self, model, config):
            self.connect_calls += 1
            return FakeLiveConnectCM(next(sessions))

    fake_client = _MultiConnectClient()
    monkeypatch.setattr(live_session, "get_client_for_key", lambda api_key: fake_client)

    app = FastAPI()
    app.include_router(live_session.router)
    client = TestClient(app)

    with client.websocket_connect("/ws/session") as ws:
        ws.send_json(
            {
                "type": "init",
                "profile_id": profile["id"],
                "profile_name": profile["name"],
                "conversation_id": conv["id"],
            }
        )
        assert ws.receive_json()["type"] == "session_status"

        # A real, complete student turn - once forwarded (gated via
        # wait_until above), the OLD session streams a partial response
        # and then dies mid-stream (raise_after).
        ws.send_json({"type": "start_turn"})
        audio_b64 = base64.b64encode(b"\x00\x01" * 50).decode()
        ws.send_json({"type": "audio_chunk", "data": audio_b64})
        ws.send_json({"type": "turn_complete"})

        assert ws.receive_json() == {"type": "transcript_out", "text": "Partial answer before the drop"}

        # The reconnect after the drop is reported the same way any other
        # reconnect is - a fresh session_status on the same browser socket.
        status2 = ws.receive_json()
        assert status2["type"] == "session_status"

        ws.send_json({"type": "close"})

    assert fake_client.connect_calls == 2
    # The whole point: the already-sent turn's audio was NOT replayed onto
    # the new session.
    assert second_session.sent_realtime_inputs == []
    # And the partial tutor text that did stream in before the drop was
    # still saved, not silently discarded or carried into the next turn.
    turns = memory.get_turns(conv["id"])
    assert ("tutor", "Partial answer before the drop") in [(t["role"], t["text"]) for t in turns]


def test_mid_response_drop_before_any_reply_still_replays_the_turn(monkeypatch, make_profile, make_conversation):
    """Regression test for a gap the original fix above left: if the
    connection dies with a genuine mid-session error WHILE the student's
    turn has already been fully sent to Gemini (activity_end sent) but
    BEFORE Gemini has said anything back at all, the buffered audio for
    that turn must still be replayed onto the reconnected session -
    there's nothing said yet to duplicate, so discarding it here would
    just silently swallow the student's turn, forcing them to notice and
    repeat themselves (exactly the reported symptom: said hello, got no
    response, had to say hello again).
    """
    monkeypatch.setattr(live_session, "summarize_conversation", lambda *a, **k: None)
    profile = make_profile(api_key="fake-key")
    conv = make_conversation(profile["id"], target_language="Spanish")

    first_session_ref = {}

    def _real_turn_forwarded():
        session = first_session_ref.get("session")
        return session is not None and len(session.sent_realtime_inputs) >= 3  # activity_start + audio + activity_end

    # No scripted responses at all before the drop - Gemini never said
    # anything back for this turn.
    first_session = FakeLiveSession(
        responses=[],
        wait_until=_real_turn_forwarded,
        raise_after=ConnectionResetError("simulated drop before any reply"),
    )
    first_session_ref["session"] = first_session
    second_session = FakeLiveSession(responses=[_response(server_content=_server_content(turn_complete=True))])
    sessions = iter([first_session, second_session])

    class _MultiConnectClient:
        def __init__(self):
            self.connect_calls = 0
            self.aio = SimpleNamespace(live=SimpleNamespace(connect=self._connect))

        def _connect(self, model, config):
            self.connect_calls += 1
            return FakeLiveConnectCM(next(sessions))

    fake_client = _MultiConnectClient()
    monkeypatch.setattr(live_session, "get_client_for_key", lambda api_key: fake_client)

    app = FastAPI()
    app.include_router(live_session.router)
    client = TestClient(app)

    with client.websocket_connect("/ws/session") as ws:
        ws.send_json(
            {
                "type": "init",
                "profile_id": profile["id"],
                "profile_name": profile["name"],
                "conversation_id": conv["id"],
            }
        )
        assert ws.receive_json()["type"] == "session_status"

        ws.send_json({"type": "start_turn"})
        audio_b64 = base64.b64encode(b"\x00\x01" * 50).decode()
        ws.send_json({"type": "audio_chunk", "data": audio_b64})
        ws.send_json({"type": "turn_complete"})

        status2 = ws.receive_json()
        assert status2["type"] == "session_status"

        ws.send_json({"type": "close"})

    assert fake_client.connect_calls == 2
    # The whole point: the turn WAS replayed, since nothing had been said
    # yet for it to duplicate.
    assert len(second_session.sent_realtime_inputs) == 3


# --- ResumableSender (transparent session resumption's client-side half -
# see build_config's session_resumption_config and ResumableSender's own
# docstring). Pure logic, no websocket harness needed - a minimal fake
# session is enough to exercise every method directly. ---


class _FakeRawSession:
    """Bare-minimum stand-in for the underlying Gemini session
    ResumableSender wraps - just enough surface (the three send_* methods)
    to record what it was called with, without any of FakeLiveSession's
    receive()/scripting machinery this doesn't need.
    """

    def __init__(self):
        self.realtime_inputs = []
        self.tool_responses = []
        self.client_contents = []

    async def send_realtime_input(self, **kwargs):
        self.realtime_inputs.append(kwargs)

    async def send_tool_response(self, **kwargs):
        self.tool_responses.append(kwargs)

    async def send_client_content(self, **kwargs):
        self.client_contents.append(kwargs)


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.unit
def test_resumable_sender_indexes_every_send_including_unbuffered_ones():
    """The index must count EVERY send (see the class's own docstring for
    why) - not just the ones that get buffered for replay."""
    sender = live_session.ResumableSender()
    session = _FakeRawSession()
    sender.attach(session)

    _run(sender.send_activity_start())
    _run(sender.send_audio(b"chunk-1", "push-to-talk"))
    _run(sender.send_tool_response(function_responses=[]))
    _run(sender.send_audio(b"chunk-2", "push-to-talk"))
    _run(sender.send_activity_end())

    assert sender._next_index == 6  # 5 sends, 1-indexed, so the next one would be 6
    # Only the two audio sends landed in the buffer, each with its own
    # index reflecting its position among ALL sends, not just audio ones:
    assert [i for (i, _, _) in sender._buffer] == [2, 4]


@pytest.mark.unit
def test_resumable_sender_attach_resets_index_but_keeps_buffer():
    sender = live_session.ResumableSender()
    sender.attach(_FakeRawSession())
    _run(sender.send_audio(b"chunk-1", "push-to-talk"))
    assert sender._next_index == 2

    sender.attach(_FakeRawSession())  # a reconnect - fresh session
    assert sender._next_index == 1  # reset, per protocol
    assert sender.buffered_audio("push-to-talk") == [b"chunk-1"]  # NOT cleared


@pytest.mark.unit
def test_resumable_sender_prune_drops_only_consumed_indices():
    sender = live_session.ResumableSender()
    sender.attach(_FakeRawSession())
    _run(sender.send_audio(b"c1", "push-to-talk"))  # index 1
    _run(sender.send_audio(b"c2", "push-to-talk"))  # index 2
    _run(sender.send_audio(b"c3", "push-to-talk"))  # index 3

    sender.prune(2)

    assert sender.buffered_audio("push-to-talk") == [b"c3"]


@pytest.mark.unit
def test_resumable_sender_prune_with_none_index_is_a_noop():
    """A None index (transparent mode unsupported, or - per a currently
    open, credible community report - simply not populated this session)
    must never drop anything - see prune()'s own docstring for why this
    matters: it's what makes the whole mechanism degrade safely instead of
    ever losing audio that should have been replayed.
    """
    sender = live_session.ResumableSender()
    sender.attach(_FakeRawSession())
    _run(sender.send_audio(b"c1", "push-to-talk"))

    sender.prune(None)

    assert sender.buffered_audio("push-to-talk") == [b"c1"]


@pytest.mark.unit
def test_resumable_sender_clear_by_label_leaves_other_labels_alone():
    sender = live_session.ResumableSender()
    sender.attach(_FakeRawSession())
    _run(sender.send_audio(b"pt1", "push-to-talk"))
    _run(sender.send_audio(b"hf1", "hands-free"))

    dropped = sender.clear("push-to-talk")

    assert dropped == 1
    assert sender.buffered_audio("push-to-talk") == []
    assert sender.buffered_audio("hands-free") == [b"hf1"]


@pytest.mark.unit
def test_resumable_sender_clear_all_when_label_is_none():
    sender = live_session.ResumableSender()
    sender.attach(_FakeRawSession())
    _run(sender.send_audio(b"pt1", "push-to-talk"))
    _run(sender.send_audio(b"hf1", "hands-free"))

    dropped = sender.clear(None)

    assert dropped == 2
    assert sender.buffered_audio("push-to-talk") == []
    assert sender.buffered_audio("hands-free") == []


@pytest.mark.unit
def test_resumable_sender_replay_unconsumed_resends_in_order_and_rebuffers():
    """Replay goes through the sender's own send_* methods (not a raw,
    untracked resend) specifically so a SECOND drop shortly after has
    something to replay too - see replay_unconsumed's own docstring for
    the gap this closes versus the old direct-to-session replay."""
    sender = live_session.ResumableSender()
    old_session = _FakeRawSession()
    sender.attach(old_session)
    _run(sender.send_audio(b"c1", "push-to-talk"))
    _run(sender.send_audio(b"c2", "push-to-talk"))

    new_session = _FakeRawSession()
    sender.attach(new_session)  # simulates a reconnect
    _run(sender.replay_unconsumed("push-to-talk"))

    kinds = [next(iter(call.keys())) for call in new_session.realtime_inputs]
    assert kinds == ["activity_start", "audio", "audio", "activity_end"]
    assert [c["audio"].data for c in new_session.realtime_inputs if "audio" in c] == [b"c1", b"c2"]
    # Re-buffered under the new connection's own numbering, not lost:
    assert sender.buffered_audio("push-to-talk") == [b"c1", b"c2"]


@pytest.mark.unit
def test_resumable_sender_replay_unconsumed_is_a_noop_when_buffer_empty():
    sender = live_session.ResumableSender()
    session = _FakeRawSession()
    sender.attach(session)

    _run(sender.replay_unconsumed("push-to-talk"))

    assert session.realtime_inputs == []
