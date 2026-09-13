// ThirtyTutors - avatar + voice selection page. Renders the female/male
// tutor grids from /api/voices, gates tiles on /api/avatars (only voices
// with a real .glb get a live preview - the rest show "Coming soon" until
// more avatars exist), previews the selection via avatarPreview.js.
//
// Two modes, both landing on this same page:
//  - New profile (no ?profile_id in the URL): came from /get-started,
//    which stashed name/native_language/api_key/model_name in
//    sessionStorage. Next creates the profile AND its first conversation.
//  - Existing profile (?profile_id=<id> in the URL): came from "+ Learn a
//    new language" in a profile's detail modal on /profiles. Next only
//    creates a new conversation under that profile - no profile fields to
//    collect, and Back returns to /profiles (reopening that same profile's
//    detail modal) rather than /get-started.

import { initAvatarHead, loadAvatarAndPlaySample, playGreeting, playPoseShowcase } from '/UI/scripts/avatarPreview.js';

const femaleGrid = document.getElementById('femaleGrid');
const maleGrid = document.getElementById('maleGrid');
const avatarPreviewEl = document.getElementById('avatarPreview');
const avatarPreviewHint = document.getElementById('avatarPreviewHint');
const avatarVoiceMeta = document.getElementById('avatarVoiceMeta');
const targetLanguageInput = document.getElementById('targetLanguageInput');
const scenarioSelect = document.getElementById('scenarioSelect');
const scenarioDescription = document.getElementById('scenarioDescription');
const difficultyToggle = document.getElementById('difficultyToggle');
const backBtn = document.getElementById('avatarBackBtn');
const nextBtn = document.getElementById('avatarNextBtn');
const watchAvatarSelectTutorialBtn = document.getElementById('watchAvatarSelectTutorialBtn');
const avatarSelectTutorialOverlay = document.getElementById('avatarSelectTutorialOverlay');
const avatarSelectTutorialVideo = document.getElementById('avatarSelectTutorialVideo');
const closeAvatarSelectTutorialBtn = document.getElementById('closeAvatarSelectTutorialBtn');

attachLanguageAutocomplete(targetLanguageInput);

const DRAFT_KEY = 'landingDraft';
const existingProfileId = new URLSearchParams(window.location.search).get('profile_id');
const DEFAULT_DIFFICULTY = 'intermediate';

let selectedVoice = null;
let availableAvatars = [];
let voicesData = [];
let scenariosData = [];
let selectedDifficulty = DEFAULT_DIFFICULTY;

function updateNextEnabled() {
  nextBtn.disabled = !(selectedVoice && targetLanguageInput.value.trim());
}

function renderGrid(container, voices) {
  container.innerHTML = '';
  voices.forEach((v) => {
    const isAvailable = availableAvatars.includes(v.name);

    const tile = document.createElement('button');
    tile.type = 'button';
    tile.className = 'tutor-tile' + (isAvailable ? '' : ' coming-soon');
    tile.disabled = !isAvailable;
    tile.dataset.voice = v.name;

    const img = document.createElement('img');
    img.src = `/photos/${v.name}.webp`;
    img.alt = v.alias || v.name;

    const fallback = document.createElement('div');
    fallback.className = 'tutor-tile-fallback';
    fallback.textContent = (v.alias || v.name)[0];
    img.addEventListener('error', () => {
      img.remove();
      fallback.style.display = 'flex';
    });

    const label = document.createElement('span');
    label.className = 'tutor-name';
    label.textContent = v.alias || v.name;

    tile.appendChild(img);
    tile.appendChild(fallback);
    tile.appendChild(label);

    if (isAvailable) {
      tile.addEventListener('click', () => selectAvatar(v.name, tile));
    } else {
      const badge = document.createElement('span');
      badge.className = 'tutor-badge';
      badge.textContent = 'Coming soon';
      tile.appendChild(badge);
    }

    container.appendChild(tile);
  });
}

async function selectAvatar(voiceName, tileEl) {
  selectedVoice = voiceName;
  document.querySelectorAll('.tutor-tile.selected').forEach((t) => t.classList.remove('selected'));
  if (tileEl) tileEl.classList.add('selected');

  const voiceInfo = voicesData.find((v) => v.name === voiceName);
  const displayName = voiceInfo ? (voiceInfo.alias || voiceName) : voiceName;
  avatarVoiceMeta.innerHTML = voiceInfo
    ? `<span>Tone: <strong>${voiceInfo.descriptor}</strong></span><span>Pitch: <strong>${voiceInfo.pitch}</strong></span>`
    : '';

  avatarPreviewHint.textContent = `Loading ${displayName}...`;
  try {
    await loadAvatarAndPlaySample(voiceName, (pct) => {
      avatarPreviewHint.textContent = `Loading ${displayName}... ${pct}%`;
    });
    avatarPreviewHint.textContent = displayName;
    playGreeting();
    playPoseShowcase(voiceInfo ? voiceInfo.gender : null);
  } catch (e) {
    avatarPreviewHint.textContent = 'Could not load this avatar - try another.';
    console.error(e);
  }
  updateNextEnabled();
}

targetLanguageInput.addEventListener('input', updateNextEnabled);

function renderScenarios() {
  scenarioSelect.innerHTML = '';
  scenariosData.forEach((s) => {
    const opt = document.createElement('option');
    opt.value = s.id;
    opt.textContent = s.label;
    scenarioSelect.appendChild(opt);
  });
  updateScenarioDescription();
}

function updateScenarioDescription() {
  const s = scenariosData.find((x) => x.id === scenarioSelect.value);
  scenarioDescription.textContent = s ? s.description : '';
}

scenarioSelect.addEventListener('change', updateScenarioDescription);

function selectDifficulty(difficulty) {
  selectedDifficulty = difficulty;
  difficultyToggle.querySelectorAll('.difficulty-option').forEach((btn) => {
    btn.classList.toggle('selected', btn.dataset.difficulty === difficulty);
  });
}

difficultyToggle.querySelectorAll('.difficulty-option').forEach((btn) => {
  btn.addEventListener('click', () => selectDifficulty(btn.dataset.difficulty));
});

// Scenario (skipped when it's the default Free Learning setting - nothing
// interesting to show) + difficulty, both fixed at creation like every
// other conversation setting.
function buildConversationName() {
  const target = targetLanguageInput.value.trim() || 'Conversation';
  const difficultyLabel = selectedDifficulty.charAt(0).toUpperCase() + selectedDifficulty.slice(1);
  const scenarioId = scenarioSelect.value;
  const scenario = scenariosData.find((s) => s.id === scenarioId);
  const parts = [target, difficultyLabel];
  if (scenario && scenario.label !== 'Free Learning') parts.push(scenario.label);
  return parts.join(' \u00b7 ');
}

backBtn.addEventListener('click', () => {
  // Returning to a profile's detail modal (not just the bare /profiles
  // list) needs the id passed along so that page knows to reopen it -
  // see profilesPage.js's handling of ?open=.
  window.location.href = existingProfileId
    ? `/profiles?open=${encodeURIComponent(existingProfileId)}`
    : '/get-started';
});

// --- Tutorial video popup ---

function openAvatarSelectTutorial() {
  avatarSelectTutorialOverlay.classList.add('visible');
  avatarSelectTutorialVideo.currentTime = 0;
  avatarSelectTutorialVideo.play().catch(() => {}); // autoplay can still be blocked on some platforms - the video still has its own controls either way
}

function closeAvatarSelectTutorial() {
  avatarSelectTutorialOverlay.classList.remove('visible');
  avatarSelectTutorialVideo.pause();
}

watchAvatarSelectTutorialBtn.addEventListener('click', openAvatarSelectTutorial);
closeAvatarSelectTutorialBtn.addEventListener('click', closeAvatarSelectTutorial);
avatarSelectTutorialOverlay.addEventListener('click', (e) => {
  if (e.target === avatarSelectTutorialOverlay) closeAvatarSelectTutorial();
});

async function createConversationForProfile(profileId, nativeLanguage, modelName) {
  const convRes = await fetch(`/api/profiles/${profileId}/conversations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      voice_name: selectedVoice,
      native_language: nativeLanguage,
      target_language: targetLanguageInput.value.trim(),
      model_name: modelName,
      scenario: scenarioSelect.value,
      difficulty: selectedDifficulty,
      name: buildConversationName(),
    }),
  });
  if (!convRes.ok) throw new Error('Could not create the conversation.');
}

// Voice enrollment/threshold calibration are no longer part of onboarding -
// they happen later, on /handsfree-setup, triggered from the learning
// page's hands-free mic button. See landing.js's header comment.

nextBtn.addEventListener('click', async () => {
  nextBtn.disabled = true;
  backBtn.disabled = true;
  const originalLabel = nextBtn.textContent;

  try {
    if (existingProfileId) {
      // --- Existing profile: just add a new conversation ---
      nextBtn.textContent = 'Starting session...';
      const profileRes = await fetch(`/api/profiles/${existingProfileId}`);
      if (!profileRes.ok) throw new Error('Profile not found.');
      const profile = await profileRes.json();

      await createConversationForProfile(existingProfileId, profile.native_language, profile.model_name);
      // The new conversation is already set as this profile's active one
      // server-side (see create_conversation_endpoint) - this just needed
      // to be set too, or the learning page has no profile to load and
      // bounces straight back to /profiles.
      localStorage.setItem('tutorProfileId', existingProfileId);
      window.location.href = '/';
    } else {
      // --- New profile onboarding, from /get-started's draft ---
      const raw = sessionStorage.getItem(DRAFT_KEY);
      if (!raw) { window.location.href = '/get-started'; return; }
      const draft = JSON.parse(raw);

      nextBtn.textContent = 'Creating profile...';
      const profileRes = await fetch('/api/profiles', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: draft.name, api_key: draft.api_key }),
      });
      if (!profileRes.ok) throw new Error('Could not create profile.');
      const profile = await profileRes.json();

      await createConversationForProfile(profile.id, draft.native_language, draft.model_name);

      sessionStorage.removeItem(DRAFT_KEY);
      localStorage.setItem('tutorProfileId', profile.id);
      window.location.href = '/';
    }
  } catch (e) {
    console.error(e);
    avatarPreviewHint.textContent = 'Something went wrong - try again.';
    nextBtn.disabled = false;
    backBtn.disabled = false;
    nextBtn.textContent = originalLabel;
  }
});

async function init() {
  if (!existingProfileId && !sessionStorage.getItem(DRAFT_KEY)) {
    window.location.href = '/get-started';
    return;
  }

  // Independent per profile/conversation, so it should never carry over
  // from a previous session or the browser's own form-data memory -
  // autocomplete="off" on the input handles most cases, this is the
  // belt-and-suspenders guarantee.
  targetLanguageInput.value = '';

  const [voicesRes, avatarsRes, scenariosRes] = await Promise.all([fetch('/api/voices'), fetch('/api/avatars'), fetch('/api/scenarios')]);
  const voicesJson = await voicesRes.json();
  const avatarsJson = await avatarsRes.json();
  const scenariosJson = await scenariosRes.json();
  voicesData = voicesJson.voices;
  availableAvatars = avatarsJson.available;
  scenariosData = scenariosJson.scenarios;

  renderScenarios();
  if (scenariosJson.default) scenarioSelect.value = scenariosJson.default;
  updateScenarioDescription();

  // Existing profile: seed the toggle from its own default (set via the
  // Settings modal's Learning tab - see settings.js) instead of the app's
  // hardcoded default, so a profile that's chosen "beginner" once doesn't
  // have to re-pick it for every new language.
  let initialDifficulty = DEFAULT_DIFFICULTY;
  if (existingProfileId) {
    try {
      const profileRes = await fetch(`/api/profiles/${existingProfileId}`);
      if (profileRes.ok) {
        const profile = await profileRes.json();
        initialDifficulty = profile.default_difficulty || DEFAULT_DIFFICULTY;
      }
    } catch (e) { /* fall back to DEFAULT_DIFFICULTY */ }
  }
  selectDifficulty(initialDifficulty);

  renderGrid(femaleGrid, voicesData.filter((v) => v.gender === 'Female'));
  renderGrid(maleGrid, voicesData.filter((v) => v.gender === 'Male'));

  try {
    await initAvatarHead(avatarPreviewEl);
  } catch (e) {
    avatarPreviewHint.textContent = 'Avatar preview could not start - you can still pick a tutor.';
    console.error(e);
  }

  // Nothing pre-selected - the preview stays empty (default hint text)
  // until the user actually clicks a tutor.
  if (!availableAvatars.length) {
    avatarPreviewHint.textContent = 'No avatars ready yet - check back soon.';
  }

  updateNextEnabled();
}

init();
