// ThirtyTutors - hands-free setup page (/handsfree-setup). Reached once per
// profile+mic combination from the learning page's hands-free mic button
// (see audio.js) - walks through mic calibration, voice enrollment, and a
// threshold-calibration test, then flags that specific mic as ready so
// future hands-free clicks on the same mic go straight into a session.
//
// EVERYTHING here is per-mic - calibration, voice enrollment, AND the
// threshold test - all stored/looked-up by mic label (see
// constants.DEFAULT_MIC_CALIBRATION_KEY for the default-mic sentinel).
// Voice enrollment might look like it should be global (a person's voice
// doesn't change with their microphone), but the embedding it produces
// absolutely does: mic frequency response, gain, and (especially for
// Bluetooth) codec compression artifacts all shape what the model actually
// "hears," and comparing across mics measurably degrades genuine-speech
// similarity scores - so enrollment is keyed per-mic the same way
// calibration and the threshold test already are.

const micSelect = document.getElementById('micSelect');
const refreshMicsBtn = document.getElementById('refreshMicsBtn');
const calibrateMicBtn = document.getElementById('calibrateMicBtn');
const calibrateMicStatus = document.getElementById('calibrateMicStatus');
const voiceEnrollmentBanner = document.getElementById('voiceEnrollmentBanner');
const voiceEnrollmentRows = document.getElementById('voiceEnrollmentRows');
const saveEnrollmentBtn = document.getElementById('saveEnrollmentBtn');
const startTestsBtn = document.getElementById('startTestsBtn');
const thresholdTestLog = document.getElementById('thresholdTestLog');
const hfSetupDoneSection = document.getElementById('hfSetupDoneSection');
const hfBackBtn = document.getElementById('hfBackBtn');
const hfContinueBtn = document.getElementById('hfContinueBtn');
const watchHandsfreeTutorialBtn = document.getElementById('watchHandsfreeTutorialBtn');
const handsfreeTutorialOverlay = document.getElementById('handsfreeTutorialOverlay');
const handsfreeTutorialVideo = document.getElementById('handsfreeTutorialVideo');
const closeHandsfreeTutorialBtn = document.getElementById('closeHandsfreeTutorialBtn');

const DEFAULT_MIC_CALIBRATION_KEY = '__default__';

// currentProfile is NOT declared here - it's the shared global `let
// currentProfile` already declared by profileMenu.js (loaded on every
// page, including this one, via index.html). This file used to redeclare
// its own separate `let currentProfile = null;` right here, which was a
// silent time bomb: two `let` declarations of the same name in the same
// global scope (classic scripts, not modules - both this file and
// profileMenu.js load as plain <script> tags sharing one scope) is a
// SyntaxError the instant the SECOND one is parsed - and a SyntaxError
// kills that entire script before a single line of it runs. Nothing on
// this page ever silently failed BECAUSE of this - the whole script,
// including every event listener (Back, Continue, calibrate, everything)
// simply never executed at all. init() below now assigns straight into
// profileMenu.js's existing global instead of shadowing it.
let speechModelStatus = 'checking'; // 'checking' | 'loading' | 'ready' | 'failed'
let enrollmentSaved = false; // for the CURRENT mic - re-checked on every mic switch

function currentMicName() {
  if (!micSelect.value) return null;
  return micSelect.options[micSelect.selectedIndex].textContent;
}

function micCalibrationKey() {
  return currentMicName() || DEFAULT_MIC_CALIBRATION_KEY;
}

function currentMicCalibration() {
  return (currentProfile.mic_calibrations || {})[micCalibrationKey()] || {};
}

function isCurrentMicCalibrated() {
  return !!currentMicCalibration().calibrated;
}

function isCurrentMicTested() {
  return !!currentMicCalibration().tested;
}

async function saveMicCalibration(patch) {
  const key = micCalibrationKey();
  const calibrations = { ...(currentProfile.mic_calibrations || {}) };
  calibrations[key] = { ...(calibrations[key] || {}), ...patch };
  await fetch(`/api/profiles/${currentProfile.id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mic_calibrations: calibrations }),
  });
  currentProfile.mic_calibrations = calibrations;
}

function updateContinueEnabled() {
  hfContinueBtn.disabled = !(enrollmentSaved && isCurrentMicCalibrated() && isCurrentMicTested());
}

// --- Shared, persistent mic session ---
// Opened once, lazily, on whichever of the three flows (calibration, voice
// enrollment, threshold test) the person tries first - then kept OPEN and
// reused by all three, rather than opening/closing a fresh stream on every
// click. A mic that hasn't been opened yet this browser session can take a
// genuinely long few seconds to actually start delivering audio frames on
// Windows (hardware negotiation) - paying that cost once, up front, is far
// better than paying it again on every Record click.
let micSession = null; // { stream, context, workletNode }
let micReadyPromise = null;
let micCollecting = false;
let micSamples = [];
const MIC_SETTLE_TIMEOUT_MS = 5000; // safety cap while waiting for the first real audio frame

// Wraps getUserMedia with a hard timeout - same rationale as audio.js's
// identical helper (a documented WKWebView hang on macOS with no
// resolve/reject at all). Duplicated here rather than shared since this
// page never loads audio.js.
function getUserMediaWithTimeout(constraints, timeoutMs = 12000) {
  return Promise.race([
    navigator.mediaDevices.getUserMedia(constraints),
    new Promise((_, reject) => setTimeout(() => reject(new Error('mic-permission-timeout')), timeoutMs)),
  ]);
}

function ensureMicReady() {
  if (micReadyPromise) return micReadyPromise;
  micReadyPromise = (async () => {
    const constraints = currentProfile.mic_device_id ? { audio: { deviceId: { exact: currentProfile.mic_device_id } } } : { audio: true };
    const stream = await getUserMediaWithTimeout(constraints);
    const context = new AudioContext({ sampleRate: 16000 });
    await context.audioWorklet.addModule('/pcm-processor.js');
    const source = context.createMediaStreamSource(stream);
    const workletNode = new AudioWorkletNode(context, 'pcm-capture-processor');

    let gotFirstFrame = false;
    workletNode.port.onmessage = (event) => {
      gotFirstFrame = true;
      if (micCollecting) micSamples.push(event.data);
    };
    source.connect(workletNode);
    micSession = { stream, context, workletNode };

    // Wait for the mic to actually start delivering real frames before
    // calling it "ready" - getUserMedia's own promise can resolve before
    // Windows hardware negotiation has actually finished, and starting a
    // real capture before that produces an empty/near-empty clip. This
    // wait only ever happens once per page load, here.
    await new Promise((resolve) => {
      const poll = setInterval(() => { if (gotFirstFrame) { clearInterval(poll); resolve(); } }, 50);
      setTimeout(() => { clearInterval(poll); resolve(); }, MIC_SETTLE_TIMEOUT_MS);
    });
  })();
  return micReadyPromise;
}

// A stream is permanently tied to the device it was opened against, so a
// mic switch has to tear the whole session down - the next ensureMicReady()
// call (from whichever flow needs it next) opens a fresh one against
// whatever's now selected.
function teardownMicSession() {
  if (micSession) {
    micSession.stream.getTracks().forEach((t) => t.stop());
    micSession.context.close();
  }
  micSession = null;
  micReadyPromise = null;
  micCollecting = false;
  micSamples = [];
}

function startCollectingMic() {
  micSamples = [];
  micCollecting = true;
}

function stopCollectingMic() {
  micCollecting = false;
  let total = 0;
  micSamples.forEach((s) => { total += s.length; });
  const combined = new Float32Array(total);
  let offset = 0;
  micSamples.forEach((s) => { combined.set(s, offset); offset += s.length; });
  return combined;
}

window.addEventListener('beforeunload', teardownMicSession);

// Reflects the CURRENT mic's saved state into the UI - called on init and
// whenever the mic selection changes, so switching to a mic that was
// already set up before doesn't force the person through it again, and
// switching to a fresh mic clearly shows what's still needed. Async now,
// since it needs to re-check enrollment status for whichever mic is
// current (enrollment is per-mic - see header comment).
async function refreshMicDependentUI() {
  const calibrated = isCurrentMicCalibrated();
  const tested = isCurrentMicTested();

  calibrateMicStatus.textContent = calibrated
    ? `Calibrated for this mic (threshold ${currentMicCalibration().silence_rms_threshold?.toFixed(4)}).`
    : 'Not calibrated for this mic yet.';

  // Re-check enrollment for whichever mic is now selected - a mic switch
  // doesn't carry over a different mic's enrollment.
  recordedVoiceSamples.fill(null);
  voiceEnrollmentRows.querySelectorAll('.enrollment-row-status').forEach((s) => { s.textContent = ''; });
  try {
    const res = await fetch(`/api/profiles/${currentProfile.id}/voice-enrollment?mic_key=${encodeURIComponent(micCalibrationKey())}`);
    const data = await res.json();
    enrollmentSaved = !!data.enrolled;
  } catch (e) {
    enrollmentSaved = false;
  }
  voiceEnrollmentBanner.style.display = enrollmentSaved ? '' : 'none';
  if (enrollmentSaved) {
    voiceEnrollmentBanner.textContent = 'Already enrolled for this mic - re-record any sentence below to replace it.';
  }
  saveEnrollmentBtn.disabled = true;

  // Calibration is mandatory before the threshold test can start - the
  // test's silence handling and the runtime gate both depend on knowing
  // this mic's noise floor first. Enrollment is mandatory too, since the
  // test scores audio against the enrolled reference.
  startTestsBtn.disabled = !(speechModelStatus === 'ready' && enrollmentSaved && calibrated);
  startTestsBtn.textContent = tested ? 'Retake tests' : 'Start tests';

  thresholdTestLog.innerHTML = '';
  if (tested) {
    const line = document.createElement('div');
    line.className = 'hf-test-line';
    line.textContent = `Already tested for this mic - threshold ${currentMicCalibration().speaker_threshold?.toFixed(3)}.`;
    thresholdTestLog.appendChild(line);
  }

  hfSetupDoneSection.style.display = (enrollmentSaved && calibrated && tested) ? '' : 'none';
  updateContinueEnabled();
}

// --- Microphone picker ---

async function loadMics() {
  let tempStream;
  try {
    tempStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    return;
  }
  // Requesting audio:true with no deviceId constraint resolves to whatever
  // the OS currently treats as the default input - reading it back off the
  // resulting track (before we stop it) is how we resolve "Default
  // microphone" to a concrete device instead of leaving it ambiguous. See
  // resolveAndPinDefaultMic below for why this matters. This is a separate,
  // quick, immediately-stopped probe - unrelated to the persistent
  // micSession above, which only opens once something actually needs to
  // record.
  const resolvedDefaultDeviceId = tempStream.getAudioTracks()[0]?.getSettings().deviceId || null;
  tempStream.getTracks().forEach((t) => t.stop());

  const devices = await navigator.mediaDevices.enumerateDevices();
  const mics = dedupeMicsByGroup(devices.filter((d) => d.kind === 'audioinput'));
  micSelect.innerHTML = '<option value="">Default microphone</option>';
  mics.forEach((d, i) => {
    const opt = document.createElement('option');
    opt.value = d.deviceId;
    opt.textContent = d.label || `Microphone ${i + 1}`;
    micSelect.appendChild(opt);
  });
  if (currentProfile.mic_device_id && mics.some((d) => d.deviceId === currentProfile.mic_device_id)) {
    micSelect.value = currentProfile.mic_device_id;
  } else if (currentProfile.mic_label) {
    const match = mics.find((d) => d.label === currentProfile.mic_label);
    if (match) micSelect.value = match.deviceId;
  }

  if (!currentProfile.mic_device_id) {
    await resolveAndPinDefaultMic(mics, resolvedDefaultDeviceId);
  }
}

// "Default microphone" is convenient in the dropdown, but ambiguous as a
// stored identity - see profileDetail.js's identical comment for the full
// reasoning. Resolves "Default" to whatever concrete device it points to
// right now and pins that as the profile's actual selection, so
// mic_calibrations (keyed by mic label) never ends up split across a
// generic placeholder and the same device's explicit entry.
async function resolveAndPinDefaultMic(mics, resolvedDefaultDeviceId) {
  if (!resolvedDefaultDeviceId) return;
  const resolved = mics.find((d) => d.deviceId === resolvedDefaultDeviceId);
  if (!resolved) return;

  teardownMicSession(); // the persistent session (if any) was opened against a different device identity
  currentProfile.mic_device_id = resolved.deviceId;
  currentProfile.mic_label = resolved.label;
  micSelect.value = resolved.deviceId;
  await fetch(`/api/profiles/${currentProfile.id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mic_device_id: resolved.deviceId, mic_label: resolved.label }),
  }).catch(() => {});
}

// Windows exposes the same physical mic multiple times - once as the
// device itself, and again for each "role" (Default / Communications) it's
// been assigned to in Windows sound settings. Chrome surfaces all of these
// as separate deviceIds, but tags role-duplicates of the same physical
// hardware with a shared groupId - so collapsing to one entry per groupId
// removes the duplicates.
function dedupeMicsByGroup(mics) {
  const byGroup = new Map();
  for (const d of mics) {
    const key = d.groupId || d.deviceId;
    const existing = byGroup.get(key);
    const isPlain = !/^(Default|Communications) -/.test(d.label || '');
    if (!existing || (isPlain && /^(Default|Communications) -/.test(existing.label || ''))) {
      byGroup.set(key, d);
    }
  }
  return Array.from(byGroup.values());
}

refreshMicsBtn.addEventListener('click', loadMics);

micSelect.addEventListener('change', async () => {
  if (!micSelect.value) {
    // They explicitly picked "Default microphone" again - resolve it to a
    // concrete device right away (see resolveAndPinDefaultMic's comment).
    const devices = await navigator.mediaDevices.enumerateDevices();
    const mics = dedupeMicsByGroup(devices.filter((d) => d.kind === 'audioinput'));
    let tempStream;
    try {
      tempStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      return;
    }
    const resolvedDefaultDeviceId = tempStream.getAudioTracks()[0]?.getSettings().deviceId || null;
    tempStream.getTracks().forEach((t) => t.stop());
    await resolveAndPinDefaultMic(mics, resolvedDefaultDeviceId);
    await refreshMicDependentUI();
    return;
  }
  teardownMicSession(); // the persistent session (if any) was opened against a different device
  const selectedOption = micSelect.options[micSelect.selectedIndex];
  const mic_device_id = micSelect.value;
  const mic_label = selectedOption.textContent;
  currentProfile.mic_device_id = mic_device_id;
  currentProfile.mic_label = mic_label;
  fetch(`/api/profiles/${currentProfile.id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mic_device_id, mic_label }),
  }).catch(() => {});
  await refreshMicDependentUI();
});

// --- Mic calibration ---

const CALIBRATION_SECONDS = 2;
const CALIBRATION_MARGIN = 3;

async function calibrateMic() {
  calibrateMicBtn.disabled = true;
  calibrateMicStatus.textContent = 'Getting the mic ready...';

  try {
    await ensureMicReady();
  } catch (e) {
    console.error(e);
    calibrateMicStatus.textContent = e && e.message === 'mic-permission-timeout'
      ? "Microphone access didn't respond - check your OS's microphone privacy settings for this app."
      : 'Calibration failed - check microphone access and try again.';
    calibrateMicBtn.disabled = false;
    return;
  }

  calibrateMicStatus.textContent = 'Stay quiet for a couple seconds...';
  startCollectingMic();
  await new Promise((r) => setTimeout(r, CALIBRATION_SECONDS * 1000));
  const combined = stopCollectingMic();

  if (combined.length === 0) {
    calibrateMicStatus.textContent = "This mic isn't delivering audio - check it's not muted/disabled, then try again.";
    calibrateMicBtn.disabled = false;
    return;
  }

  let sumSquares = 0;
  for (let i = 0; i < combined.length; i++) sumSquares += combined[i] * combined[i];
  const noiseFloorRms = Math.sqrt(sumSquares / combined.length);
  const threshold = noiseFloorRms * CALIBRATION_MARGIN;

  // A fresh calibration invalidates any threshold test already recorded
  // for this mic - the test's silence handling and scores were measured
  // against the OLD noise floor.
  await saveMicCalibration({ silence_rms_threshold: threshold, calibrated: true, tested: false, speaker_threshold: null });
  calibrateMicStatus.textContent = `Calibrated (threshold ${threshold.toFixed(4)}).`;
  calibrateMicBtn.disabled = false;
  await refreshMicDependentUI();
}

calibrateMicBtn.addEventListener('click', calibrateMic);

// --- Voice enrollment (toggle-record-per-row) - PER MIC, see header
// comment for why ---

let enrollmentSentences = null;
const recordedVoiceSamples = [null, null, null];
let recordingRowIndex = null; // which row (if any) is currently recording, via the shared mic session above

function rowEls(container, index) {
  const row = container.querySelector(`.enrollment-row[data-index="${index}"]`);
  return row ? { row, btn: row.querySelector('.enrollment-record-btn'), status: row.querySelector('.enrollment-row-status') } : null;
}

function markRowRecorded(index) {
  const els = rowEls(voiceEnrollmentRows, index);
  if (els) els.status.textContent = '\u2705';
}

function setRowError(index, message) {
  const els = rowEls(voiceEnrollmentRows, index);
  if (els) els.status.textContent = message ? '\u26a0\ufe0f' : '';
  if (els && message) els.row.title = message;
}

function setAllRecordButtonsDisabled(disabled) {
  voiceEnrollmentRows.querySelectorAll('.enrollment-record-btn').forEach((b) => { b.disabled = disabled; });
}

function renderEnrollmentRows(sentences) {
  voiceEnrollmentRows.innerHTML = '';
  sentences.forEach((sentence, i) => {
    const row = document.createElement('div');
    row.className = 'enrollment-row';
    row.dataset.index = String(i);

    const text = document.createElement('span');
    text.className = 'enrollment-sentence';
    text.textContent = sentence;

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'icon-btn enrollment-record-btn';
    btn.textContent = '\ud83c\udf99\ufe0f';
    btn.title = 'Click to record, click again to stop';
    btn.disabled = speechModelStatus !== 'ready';
    btn.addEventListener('click', () => toggleRowRecording(i));

    const status = document.createElement('span');
    status.className = 'enrollment-row-status';

    row.appendChild(text);
    row.appendChild(btn);
    row.appendChild(status);
    voiceEnrollmentRows.appendChild(row);
  });
}

async function toggleRowRecording(index) {
  if (recordingRowIndex === index) {
    stopRowRecording(index);
    return;
  }
  if (recordingRowIndex !== null) return;

  const els = rowEls(voiceEnrollmentRows, index);
  setRowError(index, null);
  els.btn.disabled = true;
  els.btn.title = 'Getting the mic ready...';

  try {
    await ensureMicReady();
  } catch (e) {
    console.error(e);
    setRowError(index, e && e.message === 'mic-permission-timeout' ? "Mic didn't respond - check OS privacy settings." : 'Could not access the microphone.');
    els.btn.disabled = false;
    els.btn.title = 'Click to record, click again to stop';
    return;
  }

  recordingRowIndex = index;
  startCollectingMic();
  els.btn.classList.add('recording');
  els.btn.textContent = '\u23f9\ufe0f';
  els.btn.title = 'Click to stop recording';
  setAllRecordButtonsDisabled(true);
  els.btn.disabled = false;
}

function floatToBase64Pcm16(float32) {
  const int16 = new Int16Array(float32.length);
  for (let i = 0; i < float32.length; i++) {
    const v = Math.max(-1, Math.min(1, float32[i]));
    int16[i] = v < 0 ? v * 32768 : v * 32767;
  }
  const bytes = new Uint8Array(int16.buffer);
  let binary = '';
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

function stopRowRecording(index) {
  const combined = stopCollectingMic();
  recordingRowIndex = null;

  const els = rowEls(voiceEnrollmentRows, index);
  els.btn.classList.remove('recording');
  els.btn.textContent = '\ud83c\udf99\ufe0f';
  els.btn.title = 'Click to record, click again to stop';
  setAllRecordButtonsDisabled(speechModelStatus !== 'ready');

  if (combined.length < 8000) {
    setRowError(index, 'Too short - click Record and try again.');
    return;
  }

  recordedVoiceSamples[index] = floatToBase64Pcm16(combined);
  markRowRecorded(index);
  saveEnrollmentBtn.disabled = !recordedVoiceSamples.every((s) => !!s);
}

saveEnrollmentBtn.addEventListener('click', async () => {
  saveEnrollmentBtn.disabled = true;
  voiceEnrollmentBanner.style.display = '';
  voiceEnrollmentBanner.textContent = 'Saving...';
  try {
    const res = await fetch(`/api/profiles/${currentProfile.id}/voice-enrollment`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ samples: recordedVoiceSamples, mic_key: micCalibrationKey() }),
    });
    if (!res.ok) throw new Error('Enrollment request failed');
    voiceEnrollmentBanner.textContent = 'Voice enrolled for this mic - saved.';
    enrollmentSaved = true;
    // A fresh enrollment invalidates any threshold test already recorded
    // for this mic - those scores were measured against the OLD reference.
    if (isCurrentMicTested()) {
      await saveMicCalibration({ tested: false, speaker_threshold: null });
    }
    await refreshMicDependentUI();
  } catch (e) {
    console.error(e);
    voiceEnrollmentBanner.textContent = 'Saving failed - check your connection and try again.';
    saveEnrollmentBtn.disabled = false;
  }
});

// --- Speech-model readiness gating ---

async function pollSpeechModelStatus() {
  try {
    const res = await fetch('/api/speech-detection-status');
    const data = await res.json();
    speechModelStatus = data.status;
  } catch (e) {
    speechModelStatus = 'failed';
  }

  if (speechModelStatus === 'ready') {
    setAllRecordButtonsDisabled(false);
    await refreshMicDependentUI();
  } else if (speechModelStatus === 'failed') {
    voiceEnrollmentBanner.style.display = '';
    voiceEnrollmentBanner.textContent = "Voice recognition couldn't load - try restarting the app.";
    setAllRecordButtonsDisabled(true);
    startTestsBtn.disabled = true;
  } else {
    voiceEnrollmentBanner.style.display = '';
    voiceEnrollmentBanner.textContent = 'Voice recognition model is downloading (first run only) - this will be available in a moment...';
    setAllRecordButtonsDisabled(true);
    startTestsBtn.disabled = true;
    setTimeout(pollSpeechModelStatus, 2000);
  }
}

// --- Threshold-calibration test ---
// Records the 5 THRESHOLD_TEST_SENTENCES, THEN one deliberately-not-speech
// clip (stay quiet / make background noise), so the threshold can be
// placed at the real midpoint between "weakest genuine reading" and
// "measured noise" instead of guessed from speech scores alone.
//
// Recording is click-to-start/click-to-stop (same pattern as voice
// enrollment's rows), not a fixed auto-timer - a fixed window either
// rushes someone who reads slowly or isn't fluent in the target language,
// or (if made longer to compensate) wastes time for someone who reads
// quickly. Self-pacing doesn't affect the underlying scoring at all - the
// embedding just reflects however much real speech ends up in the clip,
// whether that's 1 second or 5.

let thresholdTestSentences = null;
let thresholdTestNoisePrompt = null;
let collectedScores = [];

// Replaces whatever's currently shown rather than appending - only one
// sentence/status is ever visible at a time, so a completed reading gets
// cleaned away and replaced by the next prompt instead of the log growing
// into a scrolling list.
function addTestLogLine(text, active) {
  thresholdTestLog.innerHTML = '';
  const line = document.createElement('div');
  line.className = 'hf-test-line' + (active ? ' hf-test-active' : '');
  line.textContent = text;
  thresholdTestLog.appendChild(line);
  return line;
}

// Shows one prompt (a sentence, or the noise instruction) with a
// Record/Stop button the person controls themselves, resolves once they've
// stopped and the clip is scored. `promptText` is shown as-is (the caller
// decides the "Say:" vs plain wording).
function runTestStep(promptText) {
  return new Promise((resolve) => {
    const line = addTestLogLine(promptText, true);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'icon-btn enrollment-record-btn';
    btn.textContent = '\ud83c\udf99\ufe0f';
    btn.title = 'Click to record, click again to stop';
    line.appendChild(document.createTextNode(' '));
    line.appendChild(btn);

    let recording = false;

    btn.addEventListener('click', async () => {
      if (!recording) {
        btn.disabled = true;
        btn.title = 'Getting the mic ready...';
        try {
          await ensureMicReady();
        } catch (e) {
          console.error(e);
          line.textContent = e && e.message === 'mic-permission-timeout'
            ? `${promptText} - mic didn't respond, check OS privacy settings`
            : `${promptText} - could not access the microphone`;
          btn.remove();
          resolve(null);
          return;
        }
        recording = true;
        startCollectingMic();
        btn.disabled = false;
        btn.classList.add('recording');
        btn.textContent = '\u23f9\ufe0f';
        btn.title = 'Click to stop recording';
        return;
      }

      const combined = stopCollectingMic();
      btn.remove();
      line.classList.remove('hf-test-active');

      if (combined.length < 8000) {
        line.textContent = `${promptText} - too short (click Start tests to restart)`;
        resolve(null);
        return;
      }

      line.textContent = `${promptText} - scoring...`;
      try {
        const s = await scoreTestClip(floatToBase64Pcm16(combined));
        line.textContent = `${promptText} - score ${s.toFixed(3)}`;
        resolve(s);
      } catch (e) {
        console.error(e);
        line.textContent = `${promptText} - couldn't score this one`;
        resolve(null);
      }
    });
  });
}

async function scoreTestClip(clip) {
  const res = await fetch(`/api/profiles/${currentProfile.id}/voice-enrollment-test`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sample: clip, mic_key: micCalibrationKey() }),
  });
  const data = await res.json();
  if (res.ok && typeof data.score === 'number') return data.score;
  throw new Error(data.detail || 'error');
}

async function runThresholdTests() {
  if (!isCurrentMicCalibrated()) {
    calibrateMicStatus.textContent = 'Calibrate this mic first.';
    return;
  }

  startTestsBtn.disabled = true;
  collectedScores = [];

  if (!thresholdTestSentences) {
    // Shown immediately, before the await below - without this, the log
    // area just sits empty for however long the request takes (a real gap
    // on this route's first hit), which reads as "nothing happened" rather
    // than "loading".
    addTestLogLine('Loading test sentences...', false);
    try {
      const res = await fetch('/api/threshold-test-sentences');
      if (!res.ok) throw new Error(`status ${res.status}`);
      const data = await res.json();
      thresholdTestSentences = data.sentences || [];
      thresholdTestNoisePrompt = data.noise_prompt || 'Stay quiet, or make some noise without speaking.';
    } catch (e) {
      console.error(e);
      addTestLogLine('Could not load the test sentences - check your connection and click Start tests again.', false);
      startTestsBtn.disabled = false;
      return;
    }
  } else {
    thresholdTestLog.innerHTML = '';
  }

  for (const sentence of thresholdTestSentences) {
    const s = await runTestStep(`Say: "${sentence}"`);
    if (s !== null) collectedScores.push(s);
  }

  if (collectedScores.length === 0) {
    addTestLogLine('No usable scores collected - try Start tests again.', false);
    startTestsBtn.disabled = false;
    return;
  }

  // One real noise/silence measurement, so the threshold is placed against
  // actual evidence instead of guessed from speech scores alone.
  const noiseScore = await runTestStep(thresholdTestNoisePrompt);

  const speechFloor = Math.min(...collectedScores);
  let threshold;
  if (noiseScore !== null && noiseScore < speechFloor) {
    // Clean separation: place the cutoff at the actual midpoint between
    // the weakest genuine reading and the measured noise.
    threshold = (speechFloor + noiseScore) / 2;
    addTestLogLine(`Done - threshold set to ${threshold.toFixed(3)} (midpoint between speech floor ${speechFloor.toFixed(3)} and noise ${noiseScore.toFixed(3)}).`, false);
  } else {
    // Poor separation on this mic/room (noise scored as high as or higher
    // than genuine speech) - fall back to a statistics-based estimate and
    // say so plainly, since this threshold is less trustworthy.
    const mean = collectedScores.reduce((a, b) => a + b, 0) / collectedScores.length;
    const variance = collectedScores.reduce((a, b) => a + (b - mean) ** 2, 0) / collectedScores.length;
    threshold = Math.max(mean - Math.sqrt(variance), speechFloor - 0.05);
    addTestLogLine(
      `Warning: this mic/room didn't separate speech from noise well (noise scored ${noiseScore !== null ? noiseScore.toFixed(3) : 'N/A'}, close to or above your speech scores). Set threshold to ${threshold.toFixed(3)} as a fallback - consider a quieter room or a closer mic.`,
      false
    );
  }

  await saveMicCalibration({ speaker_threshold: threshold, tested: true });

  startTestsBtn.disabled = false;
  await refreshMicDependentUI();
}

startTestsBtn.addEventListener('click', runThresholdTests);

// --- Back / Continue ---
// Deliberately different destinations, not an oversight: Back is a
// "nevermind, take me back to wherever I actually was" escape hatch - the
// Settings modal's "Redo hands-free setup" button (settings.js) is global
// now and can send someone here from ANY page (landing, profiles, quiz-
// mode, wherever Settings happened to be open), and audio.js's inline
// hands-free-button prompt sends them here from the learning page itself
// - history.back() genuinely returns to whichever of those it was, rather
// than guessing one fixed destination. Continue means something different
// on purpose - "you're done, go actually USE this" - and that only ever
// makes sense pointing at '/' (the learning page) regardless of where
// setup was started from, which is exactly what its own label and done-
// message already promise ("go back to your session and re-click the
// hands-free button").
hfBackBtn.addEventListener('click', () => {
  window.history.back();
});

hfContinueBtn.addEventListener('click', () => {
  window.location.href = '/';
});

// --- Tutorial video popup ---

function openHandsfreeTutorial() {
  handsfreeTutorialOverlay.classList.add('visible');
  handsfreeTutorialVideo.currentTime = 0;
  handsfreeTutorialVideo.play().catch(() => {}); // autoplay can still be blocked on some platforms - the video still has its own controls either way
}

function closeHandsfreeTutorial() {
  handsfreeTutorialOverlay.classList.remove('visible');
  handsfreeTutorialVideo.pause();
}

watchHandsfreeTutorialBtn.addEventListener('click', openHandsfreeTutorial);
closeHandsfreeTutorialBtn.addEventListener('click', closeHandsfreeTutorial);
handsfreeTutorialOverlay.addEventListener('click', (e) => {
  if (e.target === handsfreeTutorialOverlay) closeHandsfreeTutorial();
});

// --- Init ---

async function init() {
  const profileId = localStorage.getItem('tutorProfileId');
  if (!profileId) { window.location.href = '/profiles'; return; }

  const res = await fetch(`/api/profiles/${profileId}`);
  if (!res.ok) { window.location.href = '/profiles'; return; }
  currentProfile = await res.json();

  await loadMics();

  const sentencesRes = await fetch('/api/voice-enrollment-sentences');
  const sentencesData = await sentencesRes.json();
  enrollmentSentences = sentencesData.sentences || [];
  renderEnrollmentRows(enrollmentSentences);

  await refreshMicDependentUI();
  pollSpeechModelStatus();
}
init();
