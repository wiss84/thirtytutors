// ThirtyTutors - mic capture/recording, audio playback, and the waveform
// visualizer (grouped together since the visualizer reads live state -
// isRecording, the analyser nodes - that recording/playback maintain).

let micContext = null;
let micStream = null;
let workletNode = null;
let micAnalyser = null;
let isRecording = false;

// The mic (getUserMedia + AudioContext + worklet graph) is acquired ONCE
// per page load and kept alive for the whole session, rather than
// opened/closed on every press/release. On Windows, opening a built-in
// mic is a real hardware negotiation each time (visible as the taskbar
// mic indicator appearing/disappearing) and took 4-7s per press - every
// turn was paying that cost. Bluetooth mics don't show this because they
// stay in an "active-ready" state as part of their own connection
// profile, so this was never visible on those. Now startRecording/
// stopRecording only toggle whether captured audio is actually sent
// (isRecording), never whether the mic itself is open - the one-time
// warm-up cost is paid on the FIRST press of a page session, not every
// press. micReadyPromise dedupes concurrent calls (e.g. a fast double
// press) so ensureMicReady's setup work only ever runs once.
let micReadyPromise = null;

// Wraps getUserMedia with a hard timeout. WKWebView on macOS has a known
// failure mode where getUserMedia's promise can simply hang forever with
// no prompt, no resolve, no reject. Without this, that hang would leave
// startRecording/setHandsFreeActive's own try/catch waiting indefinitely
// instead of surfacing an error.
function getUserMediaWithTimeout(constraints, timeoutMs = 12000) {
  return Promise.race([
    navigator.mediaDevices.getUserMedia(constraints),
    new Promise((_, reject) => setTimeout(() => reject(new Error('mic-permission-timeout')), timeoutMs)),
  ]);
}

let playbackContext = null;
let playbackAnalyser = null;
let playbackBus = null;
let nextPlaybackTime = 0;

// When the avatar drawer (avatarDrawer.js, a separate ES module) has a
// TalkingHead + HeadAudio ready, playback routes through the avatar's own
// AudioContext instead of the standalone one below - HeadAudio has to
// share a context with whatever audio it's analyzing (Web Audio nodes
// can't cross contexts), so this is what makes the avatar's mouth move in
// sync with the actual live conversation audio rather than sitting idle.
// null until the drawer is opened for the first time; once set it stays
// set (the avatar keeps existing even when the drawer is closed again, so
// there's no need to switch back).
let avatarAudioSink = null; // { audioCtx, headaudio }

function registerAvatarAudioSink(audioCtx, headaudio) {
  avatarAudioSink = { audioCtx, headaudio };
  nextPlaybackTime = audioCtx.currentTime; // fresh scheduling clock for the new context
}

// --- Idle -> sleep mood ---
// After 2 minutes with no conversation activity, the avatar's mood is set
// to 'sleep' - a deterministic, Gemini-independent UI state (the set_mood
// tool only fires at Gemini's discretion, so nothing guarantees a call to
// end an idle period). Resuming activity always resets the mood to
// 'neutral' first, before anything round-trips through Gemini - whatever
// mood Gemini reflects for the new turn (if any) naturally overrides this
// afterward. window.setAvatarMood is exposed by avatarDrawer.js and is a
// no-op until the avatar drawer has actually been opened once.
const IDLE_SLEEP_MS = 2 * 60 * 1000;
let idleSleepTimer = null;
let avatarAsleep = false;

function armIdleSleepTimer() {
  if (idleSleepTimer) clearTimeout(idleSleepTimer);
  idleSleepTimer = setTimeout(() => {
    avatarAsleep = true;
    if (window.setAvatarMood) window.setAvatarMood('sleep');
  }, IDLE_SLEEP_MS);
}

function noteConversationActivity() {
  if (avatarAsleep) {
    avatarAsleep = false;
    if (window.setAvatarMood) window.setAvatarMood('neutral');
  }
  armIdleSleepTimer();
}

armIdleSleepTimer(); // start the clock at page load too

const CHUNK_SAMPLES = 640; // ~40ms at 16kHz
let pcmBuffer = [];
let pcmBufferedSamples = 0;

// --- Hands-free mode ---
// Mirrors the push-to-talk buffer/flush shape above, but is independent of
// isRecording: while active, EVERY worklet callback feeds it (mic never
// needs a press), sent as a distinct 'handsfree_chunk' message type so the
// backend can run its own windowing/speaker-verification gate in front of
// forwarding anything to Gemini (see live_session.py's module docstring).
let handsFreeActive = false;
let hfBuffer = [];
let hfBufferedSamples = 0;

// --- Audio playback ---

function ensurePlaybackContext() {
  if (avatarAudioSink || playbackContext) return;
  playbackContext = new AudioContext({ sampleRate: 24000 });
  playbackAnalyser = playbackContext.createAnalyser();
  playbackAnalyser.fftSize = 256;
  playbackBus = playbackContext.createGain();
  playbackBus.connect(playbackAnalyser);
  playbackAnalyser.connect(playbackContext.destination);
  nextPlaybackTime = playbackContext.currentTime;
}

function base64ToInt16(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new Int16Array(bytes.buffer);
}

// Crossfades every chunk's boundary through near-silence instead of
// gluing discrete AudioBufferSourceNodes together at full amplitude -
// stitching them together directly caused an intermittent audible
// click/beep during tutor speech, reproduced consistently across
// different machines. A short edge fade is the standard fix for this
// class of artifact in any app that stitches together separately-
// scheduled audio buffers.
const EDGE_FADE_S = 0.002;

function playAudioChunk(b64data) {
  ensurePlaybackContext();
  const ctx = avatarAudioSink ? avatarAudioSink.audioCtx : playbackContext;

  const int16 = base64ToInt16(b64data);
  const float32 = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) float32[i] = int16[i] / 32768;

  const buffer = ctx.createBuffer(1, float32.length, 24000);
  buffer.copyToChannel(float32, 0);

  const source = ctx.createBufferSource();
  source.buffer = buffer;

  const gainNode = ctx.createGain();
  source.connect(gainNode);
  if (avatarAudioSink) {
    gainNode.connect(avatarAudioSink.headaudio); // viseme analysis -> mouth movement
    gainNode.connect(ctx.destination); // actually audible
  } else {
    gainNode.connect(playbackBus);
  }

  const startAt = Math.max(nextPlaybackTime, ctx.currentTime);
  // Clamped to half the chunk's own duration - some chunks are as short
  // as ~40ms, and the fade-in/fade-out must never overlap each other
  // within one chunk (AudioParam automation events must be strictly
  // increasing in time).
  const fadeS = Math.min(EDGE_FADE_S, buffer.duration / 2);
  gainNode.gain.setValueAtTime(0, startAt);
  gainNode.gain.linearRampToValueAtTime(1, startAt + fadeS);
  gainNode.gain.setValueAtTime(1, startAt + buffer.duration - fadeS);
  gainNode.gain.linearRampToValueAtTime(0, startAt + buffer.duration);

  source.start(startAt);
  nextPlaybackTime = startAt + buffer.duration;
}

function isTutorSpeaking() {
  const ctx = avatarAudioSink ? avatarAudioSink.audioCtx : playbackContext;
  return !!ctx && ctx.currentTime < nextPlaybackTime;
}

// Resets the audio-playback scheduling clock (see playAudioChunk above) -
// called whenever a NEW session_status arrives (websocket.js), since that
// always means either the very first connect (nothing scheduled yet, so
// this is a no-op - ctx is still null at that point) or a reconnect
// (go_away/error - see live_session.py's module docstring) that replaced
// the underlying Gemini session entirely. Without this, nextPlaybackTime
// keeps counting from wherever the OLD session's last audio chunk left
// it - if that session was cut off mid-speech (the common case for an
// error-triggered reconnect), the NEW session's first audio chunk would
// get scheduled to start at that stale, now-meaningless future timestamp
// instead of right away, producing an audible gap and a waveform
// discontinuity (heard as a click/pop) at the seam once it finally starts.
function resetPlaybackClock() {
  const ctx = avatarAudioSink ? avatarAudioSink.audioCtx : playbackContext;
  if (ctx) nextPlaybackTime = ctx.currentTime;
}
window.resetPlaybackClock = resetPlaybackClock;

// --- Recording ---

function float32ToInt16Base64(float32Array) {
  const int16 = new Int16Array(float32Array.length);
  for (let i = 0; i < float32Array.length; i++) {
    const s = Math.max(-1, Math.min(1, float32Array[i]));
    int16[i] = s < 0 ? s * 32768 : s * 32767;
  }
  const bytes = new Uint8Array(int16.buffer);
  let binary = '';
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

function flushPcmBuffer(force) {
  while (pcmBufferedSamples >= CHUNK_SAMPLES || (force && pcmBufferedSamples > 0)) {
    const combined = new Float32Array(pcmBufferedSamples);
    let offset = 0;
    for (const chunk of pcmBuffer) { combined.set(chunk, offset); offset += chunk.length; }
    const takeCount = force ? pcmBufferedSamples : CHUNK_SAMPLES;
    const toSend = combined.slice(0, takeCount);
    const remainder = combined.slice(takeCount);
    const b64 = float32ToInt16Base64(toSend);
    pendingReplayChunks.push(b64);
    ws.send(JSON.stringify({ type: 'audio_chunk', data: b64 }));
    pcmBuffer = remainder.length ? [remainder] : [];
    pcmBufferedSamples = remainder.length;
    if (force) break;
  }
}

function flushHfBuffer(force) {
  while (hfBufferedSamples >= CHUNK_SAMPLES || (force && hfBufferedSamples > 0)) {
    const combined = new Float32Array(hfBufferedSamples);
    let offset = 0;
    for (const chunk of hfBuffer) { combined.set(chunk, offset); offset += chunk.length; }
    const takeCount = force ? hfBufferedSamples : CHUNK_SAMPLES;
    const toSend = combined.slice(0, takeCount);
    const remainder = combined.slice(takeCount);
    ws.send(JSON.stringify({ type: 'handsfree_chunk', data: float32ToInt16Base64(toSend) }));
    hfBuffer = remainder.length ? [remainder] : [];
    hfBufferedSamples = remainder.length;
    if (force) break;
  }
}

// Acquires the mic + builds the capture graph exactly once per page
// session. Safe to call repeatedly - subsequent calls just await the same
// in-flight (or already-resolved) promise, so a fast double-press or a
// spacebar auto-repeat can never open a second stream.
function ensureMicReady() {
  if (micReadyPromise) return micReadyPromise;

  micReadyPromise = (async () => {
    micContext = new AudioContext({ sampleRate: 16000 });
    // Mic choice is a profile-level setting, picked on /profiles (see
    // profileDetail.js) rather than in a sidebar control here - so this
    // reads straight from the loaded profile instead of a DOM element.
    const deviceId = currentProfile && currentProfile.mic_device_id;
    const constraints = deviceId ? { audio: { deviceId: { exact: deviceId } } } : { audio: true };
    micStream = await getUserMediaWithTimeout(constraints);
    await micContext.audioWorklet.addModule('/pcm-processor.js');

    const source = micContext.createMediaStreamSource(micStream);
    micAnalyser = micContext.createAnalyser();
    micAnalyser.fftSize = 256;
    source.connect(micAnalyser);

    workletNode = new AudioWorkletNode(micContext, 'pcm-capture-processor');
    workletNode.port.onmessage = (event) => {
      if (isRecording) {
        pcmBuffer.push(event.data);
        pcmBufferedSamples += event.data.length;
        flushPcmBuffer(false);
      }
      // Dropped (not buffered/sent at all) while the tutor's own audio is
      // still playing, rather than gating at the hands-free toggle level -
      // hands-free is a continuous mode with no per-turn press, so the mic
      // stays open through the tutor's reply; without this, that reply
      // would get picked up by the mic and forwarded straight back to
      // Gemini as if the student had spoken over it. Mirrors the backend's
      // own drop-while-quiz-active pattern (_VOICE_MESSAGE_TYPES in
      // live_session.py), just gated on speech instead of quiz state.
      if (handsFreeActive && !isTutorSpeaking()) {
        hfBuffer.push(event.data);
        hfBufferedSamples += event.data.length;
        flushHfBuffer(false);
      }
    };
    source.connect(workletNode);
  })();

  return micReadyPromise;
}

// --- Reconnect audio replay (defense-in-depth) ---
// The backend now reconnects go_away and dropped-Gemini-session errors
// in place without ever closing this browser websocket (see
// live_session.py's module docstring), so under normal operation none of
// this fires - the backend's own buffered-audio replay already covers
// that case. This only matters for the rarer case where the BROWSER
// socket itself drops mid-recording (a backend crash/restart, or the
// network dropping outright): without it, whatever was already captured
// but never confirmed with a turn_complete would be silently lost, and
// the student would have to notice the tutor never replied and repeat
// themselves. Only ever holds one turn's worth - a fresh press
// (startRecording) always resets it, since replaying an abandoned turn
// after the student has already moved on and started speaking again would
// be confusing, not helpful.
let pendingReplayChunks = [];

function notifySocketClosed() {
  if (!isRecording) return;
  // The turn was still open when the socket died - turn_complete never
  // went out. Fold in whatever's left in pcmBuffer (flushPcmBuffer only
  // ever sends/records full CHUNK_SAMPLES-sized pieces, so a trailing
  // partial chunk would otherwise be dropped from the replay) and stop
  // "recording" locally, same UI state as a normal release.
  isRecording = false;
  talkBtn.classList.remove('recording');
  talkHint.textContent = 'Hold to speak';
  if (pcmBufferedSamples > 0) {
    const combined = new Float32Array(pcmBufferedSamples);
    let offset = 0;
    for (const chunk of pcmBuffer) { combined.set(chunk, offset); offset += chunk.length; }
    pendingReplayChunks.push(float32ToInt16Base64(combined));
    pcmBuffer = [];
    pcmBufferedSamples = 0;
  }
}

function getPendingReplayTurn() {
  return pendingReplayChunks.slice();
}

function clearPendingReplayTurn() {
  pendingReplayChunks = [];
}

window.notifySocketClosed = notifySocketClosed;
window.getPendingReplayTurn = getPendingReplayTurn;
window.clearPendingReplayTurn = clearPendingReplayTurn;

async function startRecording() {
  if (isRecording) return;
  if (quizActive) { showError('Finish or skip the quiz to use push-to-talk.'); return; }
  if (handsFreeActive) { showError('Turn off hands-free mode to use push-to-talk.'); return; }
  if (isTutorSpeaking()) { showError('Wait for the tutor to finish speaking.'); return; }
  if (!ws || ws.readyState !== WebSocket.OPEN) { showError('Not connected yet.'); return; }
  showError('');

  try {
    await ensureMicReady();
  } catch (e) {
    showError(e && e.message === 'mic-permission-timeout'
      ? "Microphone access didn't respond - check your OS's microphone privacy settings for this app."
      : 'Could not access the microphone.');
    console.error(e);
    micReadyPromise = null; // allow a retry on the next press
    return;
  }

  noteConversationActivity();
  pcmBuffer = [];
  pcmBufferedSamples = 0;
  pendingReplayChunks = []; // starting a new turn - any earlier undelivered turn is moot now
  isRecording = true;
  ws.send(JSON.stringify({ type: 'start_turn' }));
  talkBtn.classList.add('recording');
  talkHint.textContent = 'Listening...';
}

function stopRecording() {
  if (!isRecording) return;
  isRecording = false;
  flushPcmBuffer(true);
  talkBtn.classList.remove('recording');

  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'turn_complete' }));
    pendingReplayChunks = []; // turn completed normally - nothing left to replay
  }
  talkHint.textContent = 'Waiting for reply...';
}

talkBtn.addEventListener('mousedown', startRecording);
talkBtn.addEventListener('mouseup', stopRecording);
talkBtn.addEventListener('mouseleave', () => { if (isRecording) stopRecording(); });
talkBtn.addEventListener('touchstart', (e) => { e.preventDefault(); startRecording(); });
talkBtn.addEventListener('touchend', (e) => { e.preventDefault(); stopRecording(); });

// --- Spacebar push-to-talk ---
// Mirrors the talk button exactly (same startRecording/stopRecording),
// guarded so it doesn't fire while the user is typing in a text field
// (e.g. renaming a conversation) and doesn't re-trigger on key-repeat
// while held down.
function isTypingTarget(el) {
  if (!el) return false;
  const tag = el.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || el.isContentEditable;
}

document.addEventListener('keydown', (e) => {
  if (e.code !== 'Space' || e.repeat || isTypingTarget(e.target)) return;
  e.preventDefault(); // stop the page from scrolling on spacebar
  if (quizActive || isTutorSpeaking()) return; // startRecording() also gates on these, but skip the error/log noise entirely here
  startRecording();
});

document.addEventListener('keyup', (e) => {
  if (e.code !== 'Space' || isTypingTarget(e.target)) return;
  e.preventDefault();
  stopRecording();
});

// --- Hands-free mode button ---
// Single button, Zoom-style: mic icon (default) = muted, .live class (red,
// see base.css) = mic is open and streaming continuously. Default state on
// entering the learning page is muted - hands-free only starts on an
// explicit click, never automatically.
async function setHandsFreeActive(active) {
  if (active) {
    if (isRecording) { showError('Release push-to-talk before going hands-free.'); return; }
    if (!ws || ws.readyState !== WebSocket.OPEN) { showError('Not connected yet.'); return; }
    showError('');

    try {
      await ensureMicReady();
    } catch (e) {
      showError(e && e.message === 'mic-permission-timeout'
        ? "Microphone access didn't respond - check your OS's microphone privacy settings for this app."
        : 'Could not access the microphone.');
      console.error(e);
      micReadyPromise = null; // allow a retry on the next attempt
      return;
    }

    noteConversationActivity();
    handsFreeActive = true;
    hfBuffer = [];
    hfBufferedSamples = 0;
    handsFreeBtn.classList.add('live');
    handsFreeBtn.title = 'Hands-free listening (live - click to mute)';
    ws.send(JSON.stringify({ type: 'handsfree_start' }));
  } else {
    handsFreeActive = false;
    flushHfBuffer(true);
    handsFreeBtn.classList.remove('live');
    handsFreeBtn.title = 'Hands-free listening (muted - click to unmute)';
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'handsfree_stop' }));
    }
  }
}

handsFreeBtn.addEventListener('click', () => {
  if (quizActive) { showError('Finish or skip the quiz to use hands-free mode.'); return; }
  if (!handsFreeActive) {
    // Gate on the hands-free-setup page (mic calibration + voice enrollment
    // + per-mic threshold test) before ever opening a hands-free session -
    // see handsfreeSetup.js. Calibration/threshold data lives per-mic in
    // profile.mic_calibrations (keyed by mic label), so switching mics
    // re-requires this page for the new mic while a previously-set-up mic
    // can be switched back to without redoing anything.
    const micKey = currentProfile && (currentProfile.mic_label || '__default__');
    const micCal = currentProfile && (currentProfile.mic_calibrations || {})[micKey];
    const setupDone = !!(micCal && micCal.calibrated && micCal.tested);
    if (!setupDone) {
      window.location.href = '/handsfree-setup';
      return;
    }
  }
  setHandsFreeActive(!handsFreeActive);
});

// Release the mic on page unload so it doesn't stay flagged "in use"
// after navigating away or closing the app.
window.addEventListener('beforeunload', () => {
  if (micStream) micStream.getTracks().forEach((t) => t.stop());
});

// --- Waveform visualizer ---

function resizeCanvas() {
  const dpr = window.devicePixelRatio || 1;
  const rect = waveformCanvas.getBoundingClientRect();
  waveformCanvas.width = rect.width * dpr;
  waveformCanvas.height = rect.height * dpr;
  waveformCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
window.addEventListener('resize', resizeCanvas);

function drawWaveform() {
  requestAnimationFrame(drawWaveform);
  const rect = waveformCanvas.getBoundingClientRect();
  const w = rect.width, h = rect.height;
  waveformCtx.clearRect(0, 0, w, h);

  const activeAnalyser = isRecording ? micAnalyser : (isTutorSpeaking() ? playbackAnalyser : null);
  const barCount = 48;
  const barWidth = w / barCount;
  const mid = h / 2;

  waveformCtx.fillStyle = isRecording ? '#c1403d' : '#4a5568';

  if (!activeAnalyser) {
    const t = performance.now() / 1000;
    for (let i = 0; i < barCount; i++) {
      const amp = 2 + Math.sin(t * 1.2 + i * 0.3) * 1.5;
      waveformCtx.fillRect(i * barWidth + 1, mid - amp / 2, barWidth - 2, amp);
    }
    return;
  }

  const data = new Uint8Array(activeAnalyser.frequencyBinCount);
  activeAnalyser.getByteFrequencyData(data);
  const step = Math.floor(data.length / barCount) || 1;

  for (let i = 0; i < barCount; i++) {
    const v = data[i * step] / 255;
    const barHeight = Math.max(2, v * h * 0.9);
    waveformCtx.fillRect(i * barWidth + 1, mid - barHeight / 2, barWidth - 2, barHeight);
  }
}