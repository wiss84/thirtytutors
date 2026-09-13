// Thirtytutors - Live session WebSocket connection + reconnect handling.

// Model id -> display label, for the top-bar model lamp/name text (see
// setModelLampState in transcript.js). Fetched once at page load - if a
// session_status arrives before this resolves, the lamp just shows the raw
// model id until the next status update relabels it.
fetch('/api/models').then((r) => r.json()).then((data) => {
  (data.models || []).forEach((m) => { modelLabels[m.id] = m.label; });
}).catch(() => {});

// Plain reconnects (session time limit, model switch, a genuine blip) keep
// a flat 2s retry - that's deliberately snappy. A classified error kind
// ('network': no internet/DNS failure, or 'rate_limit': 429 quota
// exhausted after every retry - see live_session.py's
// _connect_failure_payload) is different: retrying every 2s just hammers
// a connection that's guaranteed to keep failing the same way until
// connectivity/quota actually recovers. backoffMs grows on each
// consecutive classified failure (capped) and resets the moment a
// connection actually succeeds. For 'network' specifically, a browser
// 'online' event also short-circuits the wait and retries immediately,
// since that's a far more reliable "connectivity is back" signal than a
// timer guess - there's no equivalent browser signal for quota
// recovering, so 'rate_limit' just rides out the backoff.
let lastCloseKind = null; // null | 'network' | 'rate_limit'
let backoffMs = 3000;
const BACKOFF_CAP_MS = 30000;
let onlineListenerAttached = false;
let consecutiveFailures = 0;
const FAST_RETRY_ATTEMPTS = 2;

function connectWebSocket() {
  setConnectionState('connecting');
  setModelLampState('connecting');

  // Tear down any previous socket first. Without this, calling
  // connectWebSocket() again while an old connection is still technically
  // open (the Retry button, or the 'online' event short-circuit below)
  // would abandon it without ever closing it - the server-side
  // ws_session() handler for that orphaned connection keeps running
  // indefinitely (still holding a live Gemini connection, still consuming
  // API quota) since nothing ever told it the client gave up on it.
  // manualClose=true first so the orphaned socket's own onclose doesn't
  // ALSO try to schedule a competing reconnect on top of the fresh one
  // this function is about to create.
  if (ws) {
    ws.manualClose = true;
    ws.close();
  }

  // Each socket tracks its own manualClose flag and is compared against the
  // current `ws` before acting on any event, instead of relying on one
  // shared `manualClose` boolean - a stale socket that's already been
  // superseded by a newer connection must never act on its own onclose or
  // schedule a competing reconnect.
  const socket = new WebSocket(`ws://${location.host}/ws/session`);
  socket.manualClose = false;
  ws = socket;

  socket.onopen = () => {
    if (socket !== ws) return; // superseded by a newer connection
    setConnectionState('connected');
    hideStatusToast();
    // voice_name/native_language/target_language/model_name are omitted -
    // with a profile_id and conversation_id given, the server always uses
    // its own stored config for that conversation (see ws_session in
    // main.py) rather than anything the client sends here. Those fields
    // only matter for the profile-less ephemeral fallback, which this
    // page's flow never reaches anymore (every session starts from
    // /landing or /avatar-select, both of which always produce a real
    // profile + conversation first).
    socket.send(JSON.stringify({
      type: 'init',
      profile_id: currentProfile.id,
      profile_name: currentProfile.name,
      conversation_id: currentConversationId,
    }));
    // A reconnect opens a brand-new server-side session with fresh
    // hands-free state (see live_session.py's hf_state) - if the client
    // was mid hands-free listening when the old socket dropped, tell the
    // new session so it doesn't silently stay muted server-side while the
    // button still shows "live" in the UI.
    if (handsFreeActive) {
      socket.send(JSON.stringify({ type: 'handsfree_start' }));
    }
    // Defense-in-depth for the rare case where the BROWSER socket itself
    // dropped mid-recording (see audio.js's "Reconnect audio replay"
    // section) - the backend's own go_away/error reconnect never closes
    // this socket at all, so this is only ever non-empty after something
    // more unusual (a backend restart/crash, or the network dropping
    // outright) killed the connection while a turn was still open.
    if (window.getPendingReplayTurn) {
      const chunks = window.getPendingReplayTurn();
      if (chunks.length) {
        socket.send(JSON.stringify({ type: 'start_turn' }));
        for (const data of chunks) {
          socket.send(JSON.stringify({ type: 'audio_chunk', data }));
        }
        socket.send(JSON.stringify({ type: 'turn_complete' }));
        window.clearPendingReplayTurn();
      }
    }
  };

  socket.onclose = () => {
    if (socket !== ws) return; // stale socket already superseded - don't double-reconnect
    setConnectionState('error');
    setModelLampState('connecting');
    if (window.notifySocketClosed) window.notifySocketClosed();
    if (socket.manualClose) return;

    consecutiveFailures++;

    if (lastCloseKind || consecutiveFailures > FAST_RETRY_ATTEMPTS) {
      reconnectTimer = setTimeout(connectWebSocket, backoffMs);
      backoffMs = Math.min(backoffMs * 2, BACKOFF_CAP_MS);
      if (lastCloseKind === 'network' && !onlineListenerAttached) {
        onlineListenerAttached = true;
        window.addEventListener('online', () => {
          clearTimeout(reconnectTimer);
          connectWebSocket();
        });
      }
    } else {
      reconnectTimer = setTimeout(connectWebSocket, 2000);
    }
  };

  socket.onerror = () => {
    if (socket === ws) { setConnectionState('error'); setModelLampState('connecting'); }
  };

  // console.log('WS message:', msg.type, msg);
  socket.onmessage = (event) => {
    if (socket !== ws) return; // ignore messages from a superseded connection
    const msg = JSON.parse(event.data);
    if (msg.type === 'audio') {
      playAudioChunk(msg.data);
    } else if (msg.type === 'interrupted') {
      // Deliberately no client action. This is Gemini's signal that the
      // response in progress got cut short - normally meaning the user
      // started talking over it, but Google's own docs note it can also
      // fire with no client-side cause at all ("phantom interrupt"). This
      // app's mic never forwards audio to Gemini while the tutor is
      // speaking in either mode (push-to-talk is gated on
      // isTutorSpeaking(); hands-free drops audio the same way - see
      // audio.js), so a genuine barge-in can't happen here - acting on
      // this signal would only ever be truncating the tutor's speech in
      // response to a phantom trigger.
    } else if (msg.type === 'transcript_in') {
      // Deliberately not rendered - see transcript.js's renderConversationTranscript
      // for the matching change on the history-reload path, and the reasoning
      // (Gemini's own input_audio_transcription is frequently badly garbled for
      // non-native/accented speech, unrelated to whether the model actually
      // understood the audio correctly - showing it was more confusing than
      // useful). The message itself still arrives and is still stored/
      // summarized exactly as before - this only skips the UI bubble.
    } else if (msg.type === 'transcript_out') {
      appendOrCreateBubble('tutor', msg.text);
    } else if (msg.type === 'turn_complete') {
      finalizeTurnBubbles();
      talkHint.textContent = 'Hold to speak';
      noteConversationActivity();
    } else if (msg.type === 'mood_change') {
      if (window.setAvatarMood) window.setAvatarMood(msg.mood);
    } else if (msg.type === 'quiz_start') {
      if (window.openQuizDrawer) window.openQuizDrawer(msg, { resumed: false });
    } else if (msg.type === 'quiz_resume') {
      if (window.openQuizDrawer) window.openQuizDrawer(msg, { resumed: true });
    } else if (msg.type === 'session_status') {
      showSessionStatus(msg.resumed);
      setModelLampState(msg.unavailable ? 'unavailable' : 'connected', msg.model_name);
      if (window.resetPlaybackClock) window.resetPlaybackClock();
      // A real, available session_status means the server actually
      // connected to a model - even if we were mid-backoff a moment ago,
      // we're demonstrably past whatever was failing now. Also clears any
      // lingering error/waiting toast (e.g. the "didn't respond in time -
      // reconnecting..." stalled-watchdog message) - this fires on every
      // successful reconnect INCLUDING the in-place server-side kind this
      // app relies on for go_away/mid-session errors, where the browser's
      // own socket never actually closes and so onopen (which does the
      // same hideStatusToast() for a fresh socket) never fires again to
      // clear it. Without this, a toast shown for a since-recovered error
      // could sit there indefinitely with nothing left to retry.
      if (!msg.unavailable) {
        lastCloseKind = null;
        backoffMs = 3000;
        consecutiveFailures = 0;
        hideStatusToast();
      }
    } else if (msg.type === 'waiting_long') {
      showWaitingIndicator(msg.active);
    } else if (msg.type === 'error') {
      showStatusToast(msg.message, 'error', true);
      // Purely a UI toast (see transcript.js's showStatusToast) - this is
      // never written to the transcript or memory.py.
      lastCloseKind = (msg.kind === 'network' || msg.kind === 'rate_limit') ? msg.kind : null;
    }
  };
}