// ThirtyTutors - status/error banners + transcript bubble rendering.

let sessionStatusTimer = null;

// One "in-progress" bubble per speaker per turn - transcripts stream in as
// several small chunks (from both Gemini's output_audio_transcription for
// the tutor and input_audio_transcription for the student), so these get
// appended to rather than each chunk becoming its own bubble.
let activeBubbles = { mine: null, tutor: null };

// --- Errors / status ---

// tone: 'error' (failure) or 'info' (transient "still waiting" state).
// retryable shows a Retry button - only true for an actual connection
// failure (see websocket.js), never for the local validation messages
// below (showError), where a reconnect wouldn't be the right action.
// Pass no message to hide the toast.
function showStatusToast(message, tone = 'error', retryable = false) {
  if (!message) { hideStatusToast(); return; }
  statusToastMessage.textContent = message;
  statusToast.className = `visible ${tone}`;
  statusToastRetryBtn.hidden = !retryable;
}

function hideStatusToast() {
  statusToast.className = '';
  statusToastMessage.textContent = '';
  statusToastRetryBtn.hidden = true;
}

// connectWebSocket/reconnectTimer are declared in websocket.js - reachable
// here directly since all these <script> files share one global scope
// (see state.js's header comment). By the time a person can actually
// click this button, every script has already loaded, so there's no
// load-order issue even though websocket.js loads after this file.
statusToastRetryBtn.addEventListener('click', () => {
  hideStatusToast();
  clearTimeout(reconnectTimer);
  connectWebSocket();
});

// Backward-compatible wrapper for audio.js's local validation messages
// (e.g. "finish the quiz first", "not connected yet") - always
// non-retryable, since those aren't connection failures a Retry button
// would make sense for.
function showError(text) {
  showStatusToast(text, 'error', false);
}

// The "still waiting on the tutor" indicator (see live_session.py's
// watchdog / the 'waiting_long' message) - a lighter-weight info toast,
// separate from a real error so one arriving afterward isn't accidentally
// suppressed: only clears the toast if it's still showing ITS OWN
// message, not whatever a real error may have replaced it with since.
function showWaitingIndicator(active) {
  if (active) {
    showStatusToast("Still waiting on the tutor - this can take a moment...", 'info', false);
  } else if (statusToast.classList.contains('info')) {
    hideStatusToast();
  }
}

function setConnectionState(state) {
  connectionDot.className = state === 'connected' ? 'connected' : state === 'error' ? 'error' : '';
}

// Separate from the connection dot above - that one reflects whether our
// own WebSocket to the backend is up; this one reflects whether Gemini's
// Live API itself is reachable on the currently active model, which can
// differ from our own connection state (e.g. our WS is fine, but Gemini
// dropped the session with a 1011 and both configured models are down -
// see live_session.py's ws_session). state is 'connecting' | 'connected' |
// 'unavailable' - decided color mapping: green=connected, red=connecting/
// reconnecting, gray=unavailable (both models exhausted, gave up).
function setModelLampState(state, modelId) {
  modelDot.className = state;
  if (state === 'connected' && modelId) {
    modelNameText.textContent = modelLabels[modelId] || modelId;
  } else if (state === 'unavailable') {
    modelNameText.textContent = 'No available models';
  } else {
    modelNameText.textContent = '';
  }
}

function showSessionStatus(resumed) {
  sessionStatusText.textContent = resumed ? '\u21BB resumed session' : '\u2726 fresh session';
  sessionStatusText.classList.add('visible');
  clearTimeout(sessionStatusTimer);
  sessionStatusTimer = setTimeout(() => sessionStatusText.classList.remove('visible'), 4000);
}

// --- Transcript ---

function appendOrCreateBubble(who, textChunk) {
  if (activeBubbles[who]) {
    activeBubbles[who].textSpan.textContent += textChunk;
  } else {
    if (emptyState.isConnected) emptyState.remove();
    const bubble = document.createElement('div');
    bubble.className = `bubble ${who === 'mine' ? 'mine' : 'tutor'}`;
    const label = document.createElement('span');
    label.className = 'speaker-label';
    label.textContent = who === 'mine' ? (currentProfile ? currentProfile.name : 'You') : (currentVoiceAlias || 'Tutor');
    const body = document.createElement('span');
    body.textContent = textChunk;
    bubble.appendChild(label);
    bubble.appendChild(body);
    transcriptArea.appendChild(bubble);
    activeBubbles[who] = { el: bubble, textSpan: body };
  }
  transcriptArea.scrollTop = transcriptArea.scrollHeight;
}

function finalizeTurnBubbles() {
  activeBubbles.mine = null;
  activeBubbles.tutor = null;
}

function renderHistoryBubble(who, text) {
  const bubble = document.createElement('div');
  bubble.className = `bubble ${who === 'mine' ? 'mine' : 'tutor'}`;
  const label = document.createElement('span');
  label.className = 'speaker-label';
  label.textContent = who === 'mine' ? (currentProfile ? currentProfile.name : 'You') : (currentVoiceAlias || 'Tutor');
  const body = document.createElement('span');
  body.textContent = text;
  bubble.appendChild(label);
  bubble.appendChild(body);
  transcriptArea.appendChild(bubble);
}

function renderConversationTranscript(turns) {
  finalizeTurnBubbles();
  transcriptArea.innerHTML = '';
  // 'user' turns are stored (they still feed memory/summarization) but not
  // rendered - see websocket.js's transcript_in handler for the matching
  // change and the reasoning (unreliable transcription for the student's
  // own speech). Filtered before the empty-state check, not after, so a
  // conversation with only a user turn so far (e.g. disconnected before
  // the tutor replied) still shows the empty-state message instead of a
  // blank area.
  const tutorTurns = (turns || []).filter((t) => t.role !== 'user');
  if (tutorTurns.length === 0) {
    const fresh = document.createElement('div');
    fresh.id = 'emptyState';
    fresh.textContent = 'Hold the button below and start speaking.';
    transcriptArea.appendChild(fresh);
    return;
  }
  tutorTurns.forEach((t) => renderHistoryBubble('tutor', t.text));
  transcriptArea.scrollTop = transcriptArea.scrollHeight;
}
