// ThirtyTutors - landing page (name / native language / API key / model /
// mic calibration). The profile is now created HERE, the moment mic
// calibration is first attempted (not deferred to /avatar-select as it
// used to be) - calibration has to persist against a real profile id (see
// calibrateMic below, same PUT /api/profiles/{id} pattern as
// profileDetail.js's calibration), and forcing people to actually
// calibrate before they can start a session was the whole point of moving
// this up. /avatar-select's existing "?profile_id=<id>" mode (originally
// built for "+ Learn a new language" from a profile's detail modal) is
// reused for the handoff here too, since by the time Next is clickable the
// profile already exists - its own "create a brand new profile" branch
// should now be unreachable from this page.

const firstNameInput = document.getElementById('firstNameInput');
const nativeLanguageInput = document.getElementById('nativeLanguageInput');
const apiKeyInput = document.getElementById('apiKeyInput');
const toggleApiKeyBtn = document.getElementById('toggleApiKeyBtn');
const modelSelect = document.getElementById('modelSelect');
const modelHint = document.getElementById('modelHint');
const micSelect = document.getElementById('micSelect');
const refreshMicsBtn = document.getElementById('refreshMicsBtn');
const calibrateMicBtn = document.getElementById('calibrateMicBtn');
const calibrateMicStatus = document.getElementById('calibrateMicStatus');
const nextBtn = document.getElementById('nextBtn');

attachLanguageAutocomplete(nativeLanguageInput);

const DRAFT_KEY = 'landingDraft';
let modelsData = [];

// Set once a profile has actually been created (see ensureProfileCreated) -
// null until then. Distinct from "calibrated", which additionally requires
// calibrateMic to have actually succeeded against this profile.
//
// Deliberately NOT named currentProfile: profileMenu.js (loaded globally,
// index.html, before this file) already declares a top-level `let
// currentProfile` shared by every page - a second `let currentProfile`
// here is a duplicate top-level declaration in that same shared scope,
// which is a parse-time SyntaxError that silently kills this entire
// script before a single line of it runs. That's the actual cause of the
// mic dropdown/model dropdown/Calibrate button all appearing to do
// nothing - not a caching or CSS issue. landingProfile, matching this
// file's existing #landingCard/#landingWrap/landingDraft naming, avoids
// it the same way settings.js/statsPane.js prefix their own names
// (settingsMicSelect, statsStreakValue, etc.) to avoid this exact class
// of bug on a page that loads several independent classic <script> tags
// into one shared global scope.
let landingProfile = null;
let calibrated = false;

// --- Restore a draft if the user came back from /avatar-select via Back ---

async function restoreDraft() {
  const raw = sessionStorage.getItem(DRAFT_KEY);
  if (!raw) return;
  let draft;
  try {
    draft = JSON.parse(raw);
  } catch (e) {
    return; // corrupt draft - ignore, start fresh
  }
  firstNameInput.value = draft.name || '';
  nativeLanguageInput.value = draft.native_language || '';
  apiKeyInput.value = draft.api_key || '';
  if (draft.model_name) modelSelect.value = draft.model_name;

  if (draft.profile_id) {
    try {
      const res = await fetch(`/api/profiles/${draft.profile_id}`);
      if (res.ok) {
        landingProfile = await res.json();
        calibrated = !!draft.calibrated;
        if (calibrated) calibrateMicStatus.textContent = 'Microphone calibrated.';
      }
    } catch (e) { /* profile fetch failed - treat as not-yet-created, Calibrate will recreate it */ }
  }
}

function saveDraft() {
  sessionStorage.setItem(DRAFT_KEY, JSON.stringify({
    name: firstNameInput.value.trim(),
    native_language: nativeLanguageInput.value.trim(),
    api_key: apiKeyInput.value.trim(),
    model_name: modelSelect.value,
    profile_id: landingProfile ? landingProfile.id : null,
    calibrated,
  }));
}

// --- Model list ---

async function loadModels() {
  const res = await fetch('/api/models');
  const data = await res.json();
  modelsData = data.models;
  modelSelect.innerHTML = '';
  modelsData.forEach((m) => {
    const opt = document.createElement('option');
    opt.value = m.id;
    opt.textContent = m.label;
    modelSelect.appendChild(opt);
  });
  modelSelect.value = data.default;
  updateModelHint();
}

function updateModelHint() {
  const m = modelsData.find((x) => x.id === modelSelect.value);
  if (!m) { modelHint.textContent = ''; return; }
  modelHint.textContent = `${m.rate_limit_note}${m.supports_affective_dialog ? ' - supports emotional tone (affective dialog)' : ' - lower latency, no affective dialog'}`;
}
modelSelect.addEventListener('change', updateModelHint);

// --- API key visibility toggle (same pattern as the learning page's
// sidebar - kept in sync deliberately, not shared code, since these are
// two separate pages/documents) ---

toggleApiKeyBtn.addEventListener('click', () => {
  const showing = apiKeyInput.type === 'text';
  apiKeyInput.type = showing ? 'password' : 'text';
  toggleApiKeyBtn.textContent = showing ? '👁️' : '🙈';
});

// --- Microphone (ported from profileDetail.js's pattern - see that file
// for dedupeMicsByGroup's fuller reasoning on Windows role-duplicate mics.
// Unlike there, this can run with NO profile yet: enumerating devices needs
// no profile, only persisting a choice does (see micSelect's change
// handler and calibrateMic below, both of which go through
// ensureProfileCreated first) - so "Default microphone" is deliberately
// NOT auto-resolved/pinned to a concrete device on load the way
// loadMicsForProfile does there, since there's nothing to pin it to yet. ---

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

async function loadMics() {
  let tempStream;
  try {
    tempStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    return; // permission denied - dropdown just stays at "Default microphone"
  }
  tempStream.getTracks().forEach((t) => t.stop());

  const devices = await navigator.mediaDevices.enumerateDevices();
  const mics = dedupeMicsByGroup(devices.filter((d) => d.kind === 'audioinput'));
  const previousValue = micSelect.value;
  micSelect.innerHTML = '<option value="">Default microphone</option>';
  mics.forEach((d, i) => {
    const opt = document.createElement('option');
    opt.value = d.deviceId;
    opt.textContent = d.label || `Microphone ${i + 1}`;
    micSelect.appendChild(opt);
  });
  if (landingProfile && landingProfile.mic_device_id && mics.some((d) => d.deviceId === landingProfile.mic_device_id)) {
    micSelect.value = landingProfile.mic_device_id;
  } else if (previousValue && mics.some((d) => d.deviceId === previousValue)) {
    micSelect.value = previousValue;
  }
}

refreshMicsBtn.addEventListener('click', loadMics);

// --- API key tutorial video popup ---

const watchApiKeyTutorialBtn = document.getElementById('watchApiKeyTutorialBtn');
const apiKeyTutorialOverlay = document.getElementById('apiKeyTutorialOverlay');
const apiKeyTutorialVideo = document.getElementById('apiKeyTutorialVideo');
const closeApiKeyTutorialBtn = document.getElementById('closeApiKeyTutorialBtn');

function openApiKeyTutorial() {
  apiKeyTutorialOverlay.classList.add('visible');
  apiKeyTutorialVideo.currentTime = 0;
  apiKeyTutorialVideo.play().catch(() => {}); // autoplay can still be blocked on some platforms - the video still has its own controls either way
}

function closeApiKeyTutorial() {
  apiKeyTutorialOverlay.classList.remove('visible');
  apiKeyTutorialVideo.pause();
}

watchApiKeyTutorialBtn.addEventListener('click', openApiKeyTutorial);
closeApiKeyTutorialBtn.addEventListener('click', closeApiKeyTutorial);
apiKeyTutorialOverlay.addEventListener('click', (e) => {
  if (e.target === apiKeyTutorialOverlay) closeApiKeyTutorial();
});

// --- Validation + profile creation ---

function updateNextEnabled() {
  nextBtn.disabled = !(
    firstNameInput.value.trim() &&
    nativeLanguageInput.value.trim() &&
    apiKeyInput.value.trim() &&
    calibrated
  );
}

// Once a profile exists, a change to any of these three fields invalidates
// today's saved-and-calibrated state for it only in the sense that Next
// needs the CURRENT values written back before handoff (see nextBtn's
// click handler) - calibration itself doesn't depend on name/native
// language/api key, so it deliberately stays valid across edits to those.
[firstNameInput, nativeLanguageInput, apiKeyInput].forEach((el) => {
  el.addEventListener('input', () => { updateNextEnabled(); saveDraft(); });
});

// Creates the profile the first time it's needed (a Calibrate click with no
// profile yet) rather than eagerly on page load, so someone who abandons
// this page before ever calibrating doesn't leave an orphan profile
// behind. Requires name/native language/api key already filled - a profile
// needs a name at minimum (see create_profile's own validation), and
// there's no reason to create one before the person has gotten this far
// anyway. Returns the profile, or null if creation didn't happen (either
// already existed, or validation failed).
async function ensureProfileCreated() {
  if (landingProfile) return landingProfile;

  const name = firstNameInput.value.trim();
  const nativeLanguage = nativeLanguageInput.value.trim();
  const apiKey = apiKeyInput.value.trim();
  if (!name || !nativeLanguage || !apiKey) {
    calibrateMicStatus.textContent = 'Fill in your name, native language, and API key first.';
    return null;
  }

  const createRes = await fetch('/api/profiles', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, api_key: apiKey }),
  });
  if (!createRes.ok) {
    calibrateMicStatus.textContent = 'Could not create your profile - try again.';
    return null;
  }
  const profile = await createRes.json();

  // create_profile only accepts name/api_key (see routes_api.py) - native
  // language and the chosen model are filled in with this follow-up PUT,
  // both already in PROFILE_EDITABLE_FIELDS (constants.py).
  await fetch(`/api/profiles/${profile.id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ native_language: nativeLanguage, model_name: modelSelect.value }),
  }).catch(() => {});

  landingProfile = profile;
  saveDraft();
  return profile;
}

// --- Mic calibration (ported from profileDetail.js's calibrateMic - see
// that file for the fuller reasoning on the calibration approach itself;
// only the profile-creation step at the top is new here) ---

const CALIBRATION_SECONDS = 2;
const CALIBRATION_MARGIN = 3; // ambient noise floor * this = the silence threshold
const MIC_SETTLE_TIMEOUT_MS = 5000;
const DEFAULT_MIC_CALIBRATION_KEY = '__default__';

function micCalibrationKey(profile) {
  return profile.mic_label || DEFAULT_MIC_CALIBRATION_KEY;
}

function waitForFirstAudioFrame(samplesRef) {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => { if (!done) { done = true; clearInterval(poll); resolve(); } };
    const poll = setInterval(() => { if (samplesRef.length > 0) finish(); }, 50);
    setTimeout(finish, MIC_SETTLE_TIMEOUT_MS);
  });
}

calibrateMicBtn.addEventListener('click', async () => {
  calibrateMicBtn.disabled = true;
  calibrateMicStatus.textContent = 'Checking...';

  const profile = await ensureProfileCreated();
  if (!profile) {
    calibrateMicBtn.disabled = false;
    return;
  }

  // Whichever mic is currently selected in the dropdown, captured now
  // (not read back off the profile) - there was no profile yet for the
  // dropdown's own change handler to have saved anything to.
  const selectedOption = micSelect.options[micSelect.selectedIndex];
  const mic_device_id = micSelect.value || null;
  const mic_label = mic_device_id ? selectedOption.textContent : null;

  calibrateMicStatus.textContent = 'Stay quiet for a couple seconds...';

  let context, stream, workletNode;
  let samples = [];
  try {
    const constraints = mic_device_id ? { audio: { deviceId: { exact: mic_device_id } } } : { audio: true };
    stream = await navigator.mediaDevices.getUserMedia(constraints);
    context = new AudioContext({ sampleRate: 16000 });
    await context.audioWorklet.addModule('/pcm-processor.js');
    const source = context.createMediaStreamSource(stream);
    workletNode = new AudioWorkletNode(context, 'pcm-capture-processor');
    workletNode.port.onmessage = (event) => samples.push(event.data);
    source.connect(workletNode);

    await waitForFirstAudioFrame(samples);
    samples = [];
    workletNode.port.onmessage = (event) => samples.push(event.data);

    await new Promise((r) => setTimeout(r, CALIBRATION_SECONDS * 1000));

    stream.getTracks().forEach((t) => t.stop());
    workletNode.port.onmessage = null;
    context.close();

    let total = 0;
    samples.forEach((s) => { total += s.length; });
    if (total === 0) {
      calibrateMicStatus.textContent = "This mic isn't delivering audio - check it's not muted/disabled, then try again.";
      return;
    }
    const combined = new Float32Array(total);
    let offset = 0;
    samples.forEach((s) => { combined.set(s, offset); offset += s.length; });

    let sumSquares = 0;
    for (let i = 0; i < combined.length; i++) sumSquares += combined[i] * combined[i];
    const noiseFloorRms = Math.sqrt(sumSquares / combined.length);
    const threshold = noiseFloorRms * CALIBRATION_MARGIN;

    const profileForKey = { ...profile, mic_label };
    const key = micCalibrationKey(profileForKey);
    const calibrations = { ...(profile.mic_calibrations || {}) };
    calibrations[key] = { ...(calibrations[key] || {}), silence_rms_threshold: threshold, calibrated: true };

    await fetch(`/api/profiles/${profile.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mic_device_id, mic_label, mic_calibrations: calibrations }),
    });
    landingProfile.mic_device_id = mic_device_id;
    landingProfile.mic_label = mic_label;
    landingProfile.mic_calibrations = calibrations;
    calibrated = true;
    saveDraft();
    calibrateMicStatus.textContent = 'Microphone calibrated.';
    updateNextEnabled();
  } catch (e) {
    console.error(e);
    calibrateMicStatus.textContent = 'Calibration failed - check microphone access and try again.';
  } finally {
    calibrateMicBtn.disabled = false;
  }
});

// --- Next ---

nextBtn.addEventListener('click', async () => {
  if (!landingProfile) return; // shouldn't happen - Next is disabled until calibrated, which requires landingProfile
  nextBtn.disabled = true;

  // Written back in case a field changed after calibration finished (e.g.
  // the model dropdown, or a name typo caught late) - calibration itself
  // doesn't need re-doing for any of these.
  await fetch(`/api/profiles/${landingProfile.id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      name: firstNameInput.value.trim(),
      native_language: nativeLanguageInput.value.trim(),
      api_key: apiKeyInput.value.trim(),
      model_name: modelSelect.value,
    }),
  }).catch(() => {});

  sessionStorage.removeItem(DRAFT_KEY);
  window.location.href = `/avatar-select?profile_id=${encodeURIComponent(landingProfile.id)}`;
});

async function init() {
  // Independent per profile, so these should never carry over from the
  // browser's own form-data memory - autocomplete="off" on the inputs
  // handles most cases, this is the belt-and-suspenders guarantee. Done
  // before restoreDraft() so a genuine Back-navigation draft still wins.
  firstNameInput.value = '';
  nativeLanguageInput.value = '';

  await loadModels();
  await restoreDraft();
  await loadMics();
  updateNextEnabled();
}
init();
