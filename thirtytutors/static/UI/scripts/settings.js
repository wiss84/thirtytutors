// ThirtyTutors - Settings modal (General/Account/Voice/Learning/Stats/Data
// controls/Updates/About panes) - now loaded globally (index.html), not
// just on the learning page, since the profile-menu button that opens it
// (see profileMenu.js) is itself global.
//
// Reads/writes `currentProfile` directly (declared in profileMenu.js,
// shared top-level scope - see that file's header comment for why it,
// not state.js, is the canonical place currentProfile lives now).
// `currentConversationId`/`conversationsCache` are ALSO declared globally
// (profileMenu.js) but only ever genuinely populated on the learning page
// (conversations.js) - on every other page they stay at their empty
// defaults, so the Learning tab's difficulty control only shows real data
// on the learning page (a deliberate, accepted scope line: giving every
// page its own full conversation-list fetch just for that one control
// wasn't worth the added surface area). The Data controls export list is
// NOT scoped this way - it does its own independent fetch (see
// renderExportList below), since that one needs to work from any page
// the modal can be opened from, not just the learning page.
//
// The dropdown only ever opens the modal now - the theme toggle used to
// live there too, re-clicking the shared #themeToggleBtn, but that button
// (and the dropdown item) were removed: theme is now a System/Light/Dark
// segmented control on the General tab (see below), calling theme.js's
// setThemeMode() directly.

const settingsOverlay = document.getElementById('settingsOverlay');
const closeSettingsBtn = document.getElementById('closeSettingsBtn');
const settingsCatBtns = document.querySelectorAll('.settings-cat-btn');
const settingsPanes = document.querySelectorAll('.settings-pane');

// General
const settingsNameInput = document.getElementById('settingsNameInput');
const settingsNativeLanguageInput = document.getElementById('settingsNativeLanguageInput');
const settingsGeneralStatus = document.getElementById('settingsGeneralStatus');
const settingsSaveGeneralBtn = document.getElementById('settingsSaveGeneralBtn');

attachLanguageAutocomplete(settingsNativeLanguageInput);

// Account
const settingsApiKeyInput = document.getElementById('settingsApiKeyInput');
const settingsToggleApiKeyBtn = document.getElementById('settingsToggleApiKeyBtn');
const settingsSaveApiKeyBtn = document.getElementById('settingsSaveApiKeyBtn');
const settingsApiKeyStatus = document.getElementById('settingsApiKeyStatus');

const settingsLangfusePublicKeyInput = document.getElementById('settingsLangfusePublicKeyInput');
const settingsToggleLangfusePublicKeyBtn = document.getElementById('settingsToggleLangfusePublicKeyBtn');
const settingsLangfuseSecretKeyInput = document.getElementById('settingsLangfuseSecretKeyInput');
const settingsToggleLangfuseSecretKeyBtn = document.getElementById('settingsToggleLangfuseSecretKeyBtn');
const settingsLangfuseBaseUrlInput = document.getElementById('settingsLangfuseBaseUrlInput');
const settingsSaveLangfuseBtn = document.getElementById('settingsSaveLangfuseBtn');
const settingsLangfuseStatus = document.getElementById('settingsLangfuseStatus');

// Voice & hands-free
const settingsMicSelect = document.getElementById('settingsMicSelect');
const settingsRefreshMicsBtn = document.getElementById('settingsRefreshMicsBtn');
const settingsRedoHandsfreeBtn = document.getElementById('settingsRedoHandsfreeBtn');
const settingsMicStatusList = document.getElementById('settingsMicStatusList');

// Learning
const settingsNotesList = document.getElementById('settingsNotesList');
const settingsDifficultyToggle = document.getElementById('settingsDifficultyToggle');

// Data controls
const settingsOpenDataFolderBtn = document.getElementById('settingsOpenDataFolderBtn');
const settingsExportList = document.getElementById('settingsExportList');
const settingsDeleteProfileBtn = document.getElementById('settingsDeleteProfileBtn');
const settingsExportProfileBtn = document.getElementById('settingsExportProfileBtn');
const settingsImportProfileBtn = document.getElementById('settingsImportProfileBtn');
const settingsImportProfileFile = document.getElementById('settingsImportProfileFile');
const settingsBackupStatus = document.getElementById('settingsBackupStatus');
const settingsAutoBackupToggle = document.getElementById('settingsAutoBackupToggle');
const settingsAutoBackupInterval = document.getElementById('settingsAutoBackupInterval');
const settingsOpenBackupsFolderBtn = document.getElementById('settingsOpenBackupsFolderBtn');

// About
const settingsVersionText = document.getElementById('settingsVersionText');
const settingsCreditsText = document.getElementById('settingsCreditsText');

// Updates
const settingsAppVersionText = document.getElementById('settingsAppVersionText');
const settingsAssetsVersionText = document.getElementById('settingsAssetsVersionText');
const settingsMarketingVersionText = document.getElementById('settingsMarketingVersionText');
const settingsCheckUpdatesBtn = document.getElementById('settingsCheckUpdatesBtn');
const settingsUpdateActionBtn = document.getElementById('settingsUpdateActionBtn');
const settingsUpdateStatus = document.getElementById('settingsUpdateStatus');

// --- Modal open/close + category switching ---

function selectSettingsCategory(cat) {
  settingsCatBtns.forEach((b) => b.classList.toggle('selected', b.dataset.cat === cat));
  settingsPanes.forEach((p) => { p.hidden = p.dataset.pane !== cat; });
}

settingsCatBtns.forEach((btn) => {
  btn.addEventListener('click', () => selectSettingsCategory(btn.dataset.cat));
});

closeSettingsBtn.addEventListener('click', closeSettingsModal);
settingsOverlay.addEventListener('click', (e) => {
  if (e.target === settingsOverlay) closeSettingsModal();
});

function closeSettingsModal() {
  settingsOverlay.classList.remove('visible');
}

async function openSettingsModal() {
  if (!currentProfile) return;
  selectSettingsCategory('general');
  settingsOverlay.classList.add('visible');
  // Forces an immediate repaint - see profileDetail.js's openProfileDetail
  // for the full explanation (a display:none -> flex toggle on a
  // position:fixed overlay wasn't reliably painting until something else
  // forced a recomposite).
  void settingsOverlay.offsetHeight;

  populateGeneralPane();
  populateAccountPane();
  populateLearningPane();
  populateStatsPane();
  populateBackupPane();
  renderExportList();
  loadMicsForSettings();
  loadMicStatusList();
  loadAboutPane();
  populateUpdatesPane();
}

// --- General ---
// Explicit Save button rather than save-on-blur - both fields are sent
// together on click, and the button stays disabled until something's
// actually been edited so it's obvious there's nothing unsaved when it's
// greyed out.

function populateGeneralPane() {
  settingsNameInput.value = currentProfile.name || '';
  settingsNativeLanguageInput.value = currentProfile.native_language || '';
  settingsGeneralStatus.textContent = '';
  settingsSaveGeneralBtn.disabled = true;
  refreshThemeSegmentedControl();
}

function markGeneralDirty() {
  settingsSaveGeneralBtn.disabled = false;
  settingsGeneralStatus.textContent = '';
}
settingsNameInput.addEventListener('input', markGeneralDirty);
settingsNativeLanguageInput.addEventListener('input', markGeneralDirty);

settingsSaveGeneralBtn.addEventListener('click', async () => {
  const name = settingsNameInput.value.trim();
  const native_language = settingsNativeLanguageInput.value.trim();
  if (!name || !native_language) {
    settingsGeneralStatus.textContent = 'Neither field can be empty.';
    return;
  }
  settingsSaveGeneralBtn.disabled = true;
  settingsGeneralStatus.textContent = 'Saving...';
  try {
    await fetch(`/api/profiles/${currentProfile.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, native_language }),
    });
    currentProfile.name = name;
    currentProfile.native_language = native_language;
    refreshProfileMenuButton();
    settingsGeneralStatus.textContent = 'Saved.';
  } catch (e) {
    settingsGeneralStatus.textContent = 'Could not save - check your connection.';
    settingsSaveGeneralBtn.disabled = false;
  }
});

// --- Theme (System / Light / Dark segmented control) ---

const settingsThemeToggle = document.getElementById('settingsThemeToggle');

function refreshThemeSegmentedControl() {
  const mode = currentThemeMode();
  settingsThemeToggle.querySelectorAll('.theme-option').forEach((btn) => {
    btn.classList.toggle('selected', btn.dataset.theme === mode);
  });
}

settingsThemeToggle.querySelectorAll('.theme-option').forEach((btn) => {
  btn.addEventListener('click', () => setThemeMode(btn.dataset.theme));
});

// --- Account ---

function populateAccountPane() {
  settingsApiKeyInput.value = currentProfile.api_key || '';
  settingsApiKeyInput.type = 'password';
  settingsToggleApiKeyBtn.textContent = '\ud83d\udc41\ufe0f';
  settingsApiKeyStatus.textContent = '';

  settingsLangfusePublicKeyInput.value = currentProfile.langfuse_public_key || '';
  settingsLangfusePublicKeyInput.type = 'password';
  settingsToggleLangfusePublicKeyBtn.textContent = '\ud83d\udc41\ufe0f';
  settingsLangfuseSecretKeyInput.value = currentProfile.langfuse_secret_key || '';
  settingsLangfuseSecretKeyInput.type = 'password';
  settingsToggleLangfuseSecretKeyBtn.textContent = '\ud83d\udc41\ufe0f';
  settingsLangfuseBaseUrlInput.value = currentProfile.langfuse_base_url || '';
  settingsLangfuseStatus.textContent = '';
}

settingsToggleApiKeyBtn.addEventListener('click', () => {
  const showing = settingsApiKeyInput.type === 'text';
  settingsApiKeyInput.type = showing ? 'password' : 'text';
  settingsToggleApiKeyBtn.textContent = showing ? '\ud83d\udc41\ufe0f' : '\ud83d\ude48';
});

settingsSaveApiKeyBtn.addEventListener('click', async () => {
  const value = settingsApiKeyInput.value.trim();
  if (!value) { settingsApiKeyStatus.textContent = 'API key cannot be empty.'; return; }
  settingsSaveApiKeyBtn.disabled = true;
  settingsApiKeyStatus.textContent = 'Saving...';
  try {
    await fetch(`/api/profiles/${currentProfile.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key: value }),
    });
    currentProfile.api_key = value;
    settingsApiKeyStatus.textContent = 'Saved - takes effect on your next connect.';
  } catch (e) {
    settingsApiKeyStatus.textContent = 'Could not save - check your connection.';
  } finally {
    settingsSaveApiKeyBtn.disabled = false;
  }
});

settingsToggleLangfusePublicKeyBtn.addEventListener('click', () => {
  const showing = settingsLangfusePublicKeyInput.type === 'text';
  settingsLangfusePublicKeyInput.type = showing ? 'password' : 'text';
  settingsToggleLangfusePublicKeyBtn.textContent = showing ? '\ud83d\udc41\ufe0f' : '\ud83d\ude48';
});

settingsToggleLangfuseSecretKeyBtn.addEventListener('click', () => {
  const showing = settingsLangfuseSecretKeyInput.type === 'text';
  settingsLangfuseSecretKeyInput.type = showing ? 'password' : 'text';
  settingsToggleLangfuseSecretKeyBtn.textContent = showing ? '\ud83d\udc41\ufe0f' : '\ud83d\ude48';
});

settingsSaveLangfuseBtn.addEventListener('click', async () => {
  const publicKey = settingsLangfusePublicKeyInput.value.trim();
  const secretKey = settingsLangfuseSecretKeyInput.value.trim();
  if (!publicKey || !secretKey) {
    settingsLangfuseStatus.textContent = 'Both keys are required to enable Langfuse.';
    return;
  }
  settingsSaveLangfuseBtn.disabled = true;
  settingsLangfuseStatus.textContent = 'Saving...';
  try {
    const payload = {
      langfuse_public_key: publicKey,
      langfuse_secret_key: secretKey,
      langfuse_base_url: settingsLangfuseBaseUrlInput.value.trim() || null,
    };
    await fetch(`/api/profiles/${currentProfile.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    currentProfile.langfuse_public_key = publicKey;
    currentProfile.langfuse_secret_key = secretKey;
    currentProfile.langfuse_base_url = settingsLangfuseBaseUrlInput.value.trim() || null;
    settingsLangfuseStatus.textContent = 'Saved - takes effect on your next connect.';
  } catch (e) {
    settingsLangfuseStatus.textContent = 'Could not save - check your connection.';
  } finally {
    settingsSaveLangfuseBtn.disabled = false;
  }
});

// --- Voice & hands-free ---
// Mic picker + "Default microphone" resolution mirrors profileDetail.js's
// identical logic (kept as its own copy rather than shared code, since
// that file's DOM elements only exist on /profiles - see its own header
// comment for the same reasoning about profileDetail.js vs handsfreeSetup.js).

function dedupeMicsByGroupSettings(mics) {
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

async function resolveAndPinDefaultMicSettings(mics, resolvedDefaultDeviceId) {
  if (!resolvedDefaultDeviceId) return;
  const resolved = mics.find((d) => d.deviceId === resolvedDefaultDeviceId);
  if (!resolved) return;
  currentProfile.mic_device_id = resolved.deviceId;
  currentProfile.mic_label = resolved.label;
  settingsMicSelect.value = resolved.deviceId;
  await fetch(`/api/profiles/${currentProfile.id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mic_device_id: resolved.deviceId, mic_label: resolved.label }),
  }).catch(() => {});
}

async function loadMicsForSettings() {
  let tempStream;
  try {
    tempStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    return; // permission denied - dropdown just stays at "Default microphone"
  }
  const resolvedDefaultDeviceId = tempStream.getAudioTracks()[0]?.getSettings().deviceId || null;
  tempStream.getTracks().forEach((t) => t.stop());

  const devices = await navigator.mediaDevices.enumerateDevices();
  const mics = dedupeMicsByGroupSettings(devices.filter((d) => d.kind === 'audioinput'));
  settingsMicSelect.innerHTML = '<option value="">Default microphone</option>';
  mics.forEach((d, i) => {
    const opt = document.createElement('option');
    opt.value = d.deviceId;
    opt.textContent = d.label || `Microphone ${i + 1}`;
    settingsMicSelect.appendChild(opt);
  });
  if (currentProfile.mic_device_id && mics.some((d) => d.deviceId === currentProfile.mic_device_id)) {
    settingsMicSelect.value = currentProfile.mic_device_id;
  } else if (currentProfile.mic_label) {
    const match = mics.find((d) => d.label === currentProfile.mic_label);
    if (match) settingsMicSelect.value = match.deviceId;
  }

  if (!currentProfile.mic_device_id) {
    await resolveAndPinDefaultMicSettings(mics, resolvedDefaultDeviceId);
  }
}

settingsRefreshMicsBtn.addEventListener('click', loadMicsForSettings);

settingsMicSelect.addEventListener('change', async () => {
  if (!settingsMicSelect.value) {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const mics = dedupeMicsByGroupSettings(devices.filter((d) => d.kind === 'audioinput'));
    let tempStream;
    try {
      tempStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      return;
    }
    const resolvedDefaultDeviceId = tempStream.getAudioTracks()[0]?.getSettings().deviceId || null;
    tempStream.getTracks().forEach((t) => t.stop());
    await resolveAndPinDefaultMicSettings(mics, resolvedDefaultDeviceId);
    return;
  }
  const selectedOption = settingsMicSelect.options[settingsMicSelect.selectedIndex];
  const mic_device_id = settingsMicSelect.value;
  const mic_label = selectedOption.textContent;
  currentProfile.mic_device_id = mic_device_id;
  currentProfile.mic_label = mic_label;
  fetch(`/api/profiles/${currentProfile.id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mic_device_id, mic_label }),
  }).catch(() => {});
});

settingsRedoHandsfreeBtn.addEventListener('click', () => {
  // handsfree-setup reads the active profile from localStorage itself
  // (see handsfreeSetup.js's init) - already set by this point since
  // this page couldn't have loaded without it.
  window.location.href = '/handsfree-setup';
});

async function loadMicStatusList() {
  settingsMicStatusList.innerHTML = '<p class="mic-status-empty">Loading...</p>';
  try {
    const res = await fetch(`/api/profiles/${currentProfile.id}/mic-status`);
    const data = await res.json();
    renderMicStatusList(data.mics || []);
  } catch (e) {
    settingsMicStatusList.innerHTML = '<p class="mic-status-empty">Could not load mic status.</p>';
  }
}

function renderMicStatusList(mics) {
  settingsMicStatusList.innerHTML = '';
  if (mics.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'mic-status-empty';
    empty.textContent = "No mics set up yet - use \"Redo hands-free setup\" above to calibrate one.";
    settingsMicStatusList.appendChild(empty);
    return;
  }
  mics.forEach((m) => {
    const row = document.createElement('div');
    row.className = 'mic-status-row';

    const label = document.createElement('span');
    label.className = 'mic-status-label';
    label.textContent = m.label;
    label.title = m.label;

    const badges = document.createElement('span');
    badges.className = 'mic-status-badges';
    [['calibrated', 'Calibrated'], ['enrolled', 'Enrolled'], ['tested', 'Tested']].forEach(([key, text]) => {
      const badge = document.createElement('span');
      badge.className = 'mic-status-badge' + (m[key] ? ' done' : '');
      badge.textContent = (m[key] ? '\u2713 ' : '\u2013 ') + text;
      badges.appendChild(badge);
    });

    row.appendChild(label);
    row.appendChild(badges);
    settingsMicStatusList.appendChild(row);
  });
}

// --- Learning ---
// The difficulty toggle is dual-purpose: it sets the profile's
// default_difficulty for languages added later, AND - if a conversation is
// currently active - patches that conversation's own stored difficulty
// too, so picking a new one here isn't just a future convenience. It
// still can't apply mid-turn (the value's baked into the system
// instruction at connect time - see live_session.py's build_config), so
// the honest framing is "takes effect next reconnect", not instant.

function currentConversation() {
  return conversationsCache.find((c) => c.id === currentConversationId);
}

// One row per language/conversation, not a single button - a profile can
// have several, and there's no one "active" conversation to default to
// outside the learning page (currentConversationId only gets populated
// there - see this file's header comment). Fetches its own copy of the
// conversation list for the same reason the Data controls export list
// just below does (see that section's own comment) - works from any page
// the modal can be opened from.
async function renderNotesLanguageList() {
  settingsNotesList.innerHTML = '<p class="export-list-empty">Loading...</p>';
  let conversations;
  try {
    const res = await fetch(`/api/profiles/${currentProfile.id}/conversations`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    conversations = data.conversations || [];
  } catch (e) {
    settingsNotesList.innerHTML = '';
    const err = document.createElement('p');
    err.className = 'export-list-empty';
    err.textContent = 'Could not load languages - check your connection.';
    settingsNotesList.appendChild(err);
    return;
  }

  settingsNotesList.innerHTML = '';
  if (conversations.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'export-list-empty';
    empty.textContent = 'No languages yet.';
    settingsNotesList.appendChild(empty);
    return;
  }
  conversations.forEach((conv) => {
    const label = conversationLabel(conv);
    const row = document.createElement('div');
    row.className = 'export-row';

    const labelEl = document.createElement('span');
    labelEl.className = 'export-row-label';
    labelEl.textContent = label;
    labelEl.title = label;

    const viewBtn = document.createElement('button');
    viewBtn.className = 'pill-btn';
    viewBtn.type = 'button';
    viewBtn.textContent = 'View';
    viewBtn.addEventListener('click', () => {
      closeSettingsModal();
      openNotesModal(currentProfile.id, conv.id, label);
    });

    row.appendChild(labelEl);
    row.appendChild(viewBtn);
    settingsNotesList.appendChild(row);
  });
}

function populateLearningPane() {
  const conv = currentConversation();
  const difficulty = (conv && conv.config && conv.config.difficulty) || currentProfile.default_difficulty || 'intermediate';
  settingsDifficultyToggle.querySelectorAll('.difficulty-option').forEach((btn) => {
    btn.classList.toggle('selected', btn.dataset.difficulty === difficulty);
  });
  renderNotesLanguageList();
}

settingsDifficultyToggle.querySelectorAll('.difficulty-option').forEach((btn) => {
  btn.addEventListener('click', async () => {
    const difficulty = btn.dataset.difficulty;
    settingsDifficultyToggle.querySelectorAll('.difficulty-option').forEach((b) => {
      b.classList.toggle('selected', b === btn);
    });

    currentProfile.default_difficulty = difficulty;
    const requests = [
      fetch(`/api/profiles/${currentProfile.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ default_difficulty: difficulty }),
      }),
    ];

    const conv = currentConversation();
    if (conv) {
      conv.config = { ...conv.config, difficulty };
      requests.push(fetch(`/api/profiles/${currentProfile.id}/conversations/${conv.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ difficulty }),
      }));
    }
    await Promise.all(requests).catch(() => {});
  });
});

// --- Data controls: open data folder ---

settingsOpenDataFolderBtn.addEventListener('click', () => {
  fetch('/api/open-data-folder', { method: 'POST' }).catch(() => {});
});

// --- Data controls: export learnt notes ---
// One row per conversation - Data controls is profile-scoped, so
// exporting is offered for every language under this profile, not just
// whichever one happens to be open right now. Fetches its own copy of
// the conversation list rather than reading conversationsCache - that
// cache is only ever genuinely populated on the learning page (see
// profileMenu.js's header comment), so relying on it here left this list
// permanently empty on every other page the Settings modal can be opened
// from.

function conversationLabel(conv) {
  return conv.name || conv.config?.target_language || 'Conversation';
}

function exportDocxUrl(conversationId) {
  return `/api/profiles/${currentProfile.id}/conversations/${conversationId}/notes/export.docx`;
}

async function renderExportList() {
  settingsExportList.innerHTML = '<p class="export-list-empty">Loading...</p>';
  let conversations;
  try {
    const res = await fetch(`/api/profiles/${currentProfile.id}/conversations`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    conversations = data.conversations || [];
  } catch (e) {
    settingsExportList.innerHTML = '';
    const err = document.createElement('p');
    err.className = 'export-list-empty';
    err.textContent = 'Could not load languages - check your connection.';
    settingsExportList.appendChild(err);
    return;
  }

  settingsExportList.innerHTML = '';
  if (conversations.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'export-list-empty';
    empty.textContent = 'No languages yet.';
    settingsExportList.appendChild(empty);
    return;
  }
  conversations.forEach((conv) => {
    const row = document.createElement('div');
    row.className = 'export-row';

    const label = document.createElement('span');
    label.className = 'export-row-label';
    label.textContent = conversationLabel(conv);
    label.title = label.textContent;

    const actions = document.createElement('span');
    actions.className = 'export-row-actions';

    const printBtn = document.createElement('button');
    printBtn.className = 'pill-btn';
    printBtn.type = 'button';
    printBtn.textContent = 'Print';
    printBtn.addEventListener('click', () => printConversationNotes(conv.id, conversationLabel(conv)));

    const exportBtn = document.createElement('button');
    exportBtn.className = 'pill-btn';
    exportBtn.type = 'button';
    exportBtn.textContent = 'Export as Word';
    exportBtn.addEventListener('click', async () => {
      try {
        const res = await fetch(exportDocxUrl(conv.id));
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        const disposition = res.headers.get('Content-Disposition');
        const filenameMatch = disposition && disposition.match(/filename="?([^"]+)"?/);
        a.download = filenameMatch ? filenameMatch[1] : 'notes.docx';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
      } catch (e) {
        alert('Could not export notes - check your connection and try again.');
      }
    });

    actions.appendChild(printBtn);
    actions.appendChild(exportBtn);
    row.appendChild(label);
    row.appendChild(actions);
    settingsExportList.appendChild(row);
  });
}

let notesPrintArea = null;

// Local copy of notes.js's identical helper - Data controls' Print
// button is reachable from the Settings modal on every page (it's global,
// see this file's header comment), but notes.js itself is only loaded on
// the learning page and /profiles. Calling the global version from here
// worked by accident on those two pages and threw a silent
// ReferenceError (killing execution before window.print() ever ran) on
// every other page - landing, get-started, avatar-select, quiz-mode,
// whats-new - exactly matching "Export works, Print doesn't": the docx
// export never touches this function, only the print path does.
function formatTimestamp(value) {
  if (!value) return '';
  // ts on vocab_mistakes rows is unix seconds; ts on lesson_log rows is an
  // ISO string - Date() handles both, but only multiply the numeric case.
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  if (isNaN(date.getTime())) return '';
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function buildPrintHtml(label, data) {
  const esc = (s) => String(s ?? '').replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
  const vocabHtml = (data.vocab_mistakes || []).length
    ? data.vocab_mistakes.map((v) => `<li><strong>${esc(v.term)}</strong>${v.note ? ' - ' + esc(v.note) : ''} <em>(seen ${v.occurrences}x)</em></li>`).join('')
    : '<li>Nothing tracked yet.</li>';
  const lessonHtml = (data.lesson_log || []).length
    ? data.lesson_log.map((l) => `<li>${esc(formatTimestamp(l.ts))}: ${esc(l.summary)}</li>`).join('')
    : '<li>No lesson log entries yet.</li>';
  return `<h1>${esc(label)} - Learnt Notes</h1>` +
    `<h2>Vocabulary &amp; Mistakes</h2><ul>${vocabHtml}</ul>` +
    `<h2>Lesson Log</h2><ul>${lessonHtml}</ul>`;
}

async function printConversationNotes(conversationId, label) {
  let data;
  try {
    const res = await fetch(`/api/profiles/${currentProfile.id}/conversations/${conversationId}/notes`);
    data = await res.json();
  } catch (e) {
    alert('Could not load notes to print - check your connection and try again.');
    return;
  }

  if (!notesPrintArea) {
    notesPrintArea = document.createElement('div');
    notesPrintArea.id = 'notesPrintArea';
    document.body.appendChild(notesPrintArea);
  }
  notesPrintArea.innerHTML = buildPrintHtml(label, data);
  window.print();
}

// --- Data controls: profile backup (export/import/auto-backup) ---
// A full backup (profile identity + every conversation/quiz session under
// it + voice enrollment) is a different, bigger thing than the per-
// conversation notes export just above - see backup.py for what's
// actually in the zip.

function populateBackupPane() {
  settingsBackupStatus.textContent = '';
  settingsAutoBackupToggle.checked = !!currentProfile.auto_backup_enabled;
  settingsAutoBackupInterval.value = String(currentProfile.auto_backup_interval_days || 7);
  settingsAutoBackupInterval.disabled = !settingsAutoBackupToggle.checked;
}

settingsExportProfileBtn.addEventListener('click', async () => {
  settingsExportProfileBtn.disabled = true;
  settingsBackupStatus.textContent = 'Building backup...';
  try {
    const res = await fetch(`/api/profiles/${currentProfile.id}/export`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    const disposition = res.headers.get('Content-Disposition');
    const filenameMatch = disposition && disposition.match(/filename="?([^"]+)"?/);
    a.download = filenameMatch ? filenameMatch[1] : 'ThirtyTutors-backup.zip';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    settingsBackupStatus.textContent = 'Backup downloaded.';
  } catch (e) {
    settingsBackupStatus.textContent = 'Could not build backup - check your connection and try again.';
  } finally {
    settingsExportProfileBtn.disabled = false;
  }
});

settingsImportProfileBtn.addEventListener('click', () => settingsImportProfileFile.click());

settingsImportProfileFile.addEventListener('change', async () => {
  const file = settingsImportProfileFile.files[0];
  settingsImportProfileFile.value = ''; // always reset - re-selecting the same file should still fire 'change' next time
  if (!file) return;
  if (!confirm('Importing a backup replaces this profile\'s data if it already exists locally. Continue?')) return;

  settingsImportProfileBtn.disabled = true;
  settingsBackupStatus.textContent = 'Importing...';
  try {
    const formData = new FormData();
    formData.append('file', file);
    const res = await fetch('/api/profiles/import', { method: 'POST', body: formData });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    settingsBackupStatus.textContent = `Imported "${data.profile?.name || 'profile'}". Opening profile picker...`;
    // Navigates to /profiles rather than reloading in place - an imported
    // backup might be a DIFFERENT profile than the one currently open here
    // (bringing a profile over from another machine), so "go pick it from
    // the profile list" is the right next step either way, not assuming
    // it should silently become the active one.
    setTimeout(() => { window.location.href = '/profiles'; }, 1200);
  } catch (e) {
    settingsBackupStatus.textContent = "Could not import that file - make sure it's a ThirtyTutors backup zip.";
    settingsImportProfileBtn.disabled = false;
  }
});

settingsAutoBackupToggle.addEventListener('change', async () => {
  const auto_backup_enabled = settingsAutoBackupToggle.checked;
  settingsAutoBackupInterval.disabled = !auto_backup_enabled;
  currentProfile.auto_backup_enabled = auto_backup_enabled;
  fetch(`/api/profiles/${currentProfile.id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ auto_backup_enabled }),
  }).catch(() => {});
});

settingsAutoBackupInterval.addEventListener('change', async () => {
  const auto_backup_interval_days = Number(settingsAutoBackupInterval.value);
  currentProfile.auto_backup_interval_days = auto_backup_interval_days;
  fetch(`/api/profiles/${currentProfile.id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ auto_backup_interval_days }),
  }).catch(() => {});
});

settingsOpenBackupsFolderBtn.addEventListener('click', () => {
  fetch('/api/open-backups-folder', { method: 'POST' }).catch(() => {});
});

// --- Data controls: delete profile ---

settingsDeleteProfileBtn.addEventListener('click', async () => {
  if (!currentProfile) return;
  if (!confirm(`Delete profile "${currentProfile.name}"? This deletes every language and its notes, and cannot be undone.`)) return;
  settingsDeleteProfileBtn.disabled = true;
  try {
    await fetch(`/api/profiles/${currentProfile.id}`, { method: 'DELETE' });
    localStorage.removeItem('tutorProfileId');
    window.location.href = '/profiles';
  } catch (e) {
    settingsDeleteProfileBtn.disabled = false;
    alert('Could not delete this profile - check your connection and try again.');
  }
});

// --- About ---

let appInfoCache = null;

async function loadAboutPane() {
  if (appInfoCache) {
    renderAboutPane(appInfoCache);
    return;
  }
  try {
    const res = await fetch('/api/app-info');
    appInfoCache = await res.json();
    renderAboutPane(appInfoCache);
  } catch (e) {
    settingsVersionText.textContent = 'ThirtyTutors';
  }
}

function renderAboutPane(info) {
  settingsVersionText.textContent = `ThirtyTutors v${info.version}`;
  settingsCreditsText.textContent = info.credits && info.credits.length
    ? `Built with ${info.credits.join(', ')}.`
    : '';
}

// --- Updates ---
// Reads the same latestUpdateStatus/describeUpdate/runUpdateAction the
// top-bar bell and profile-dropdown item use (update.js, loaded globally
// in index.html before this file) - this tab is just another view onto
// the same state, not a separate check.

function renderUpdatesPane() {
  const app = latestUpdateStatus && latestUpdateStatus.app;
  const assets = latestUpdateStatus && latestUpdateStatus.assets;
  const marketing = latestUpdateStatus && latestUpdateStatus.marketing;

  settingsAppVersionText.textContent = app
    ? (app.update_available
        ? `v${app.current} \u2192 v${app.latest} available`
        : `v${app.current} (up to date)`)
    : 'Could not check.';

  settingsAssetsVersionText.textContent = assets
    ? (assets.update_available ? 'Update available' : 'Up to date')
    : 'Could not check.';

  settingsMarketingVersionText.textContent = marketing
    ? (marketing.update_available ? 'Update available' : 'Up to date')
    : 'Could not check.';

  const info = describeUpdate(latestUpdateStatus);
  settingsUpdateActionBtn.hidden = !info;
  if (info) {
    settingsUpdateActionBtn.textContent = info.actionLabel;
    settingsUpdateActionBtn.disabled = false;
  }
  settingsUpdateStatus.textContent = '';
}

async function populateUpdatesPane() {
  settingsAppVersionText.textContent = 'Checking...';
  settingsAssetsVersionText.textContent = 'Checking...';
  settingsMarketingVersionText.textContent = 'Checking...';
  settingsUpdateActionBtn.hidden = true;
  settingsUpdateStatus.textContent = '';
  await loadUpdateStatus(false); // shares the cache - opening this tab right after the silent on-load check won't re-hit PyPI
  renderUpdatesPane();
}

settingsCheckUpdatesBtn.addEventListener('click', async () => {
  settingsCheckUpdatesBtn.disabled = true;
  settingsAppVersionText.textContent = 'Checking...';
  settingsAssetsVersionText.textContent = 'Checking...';
  settingsMarketingVersionText.textContent = 'Checking...';
  await loadUpdateStatus(true); // force - bypasses the cache, this is the explicit manual check
  renderUpdatesPane();
  settingsCheckUpdatesBtn.disabled = false;
});

settingsUpdateActionBtn.addEventListener('click', () => {
  runUpdateAction((text) => { settingsUpdateStatus.textContent = text; }, settingsUpdateActionBtn);
});

// profileMenu.js's initProfileMenu() owns the initial paint of the
// profile-menu button/dropdown - nothing to do here on load.
