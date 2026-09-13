// Update notification bell (top bar, base template - see index.html) +
// the learning page's profile dropdown persistent item (if present on
// this page - see settings.js's #settingsDropdown) + the Settings modal's
// Updates tab (also settings.js). All three read the same
// /api/update-status endpoint and share the action logic here, so
// "Update & Relaunch" behaves identically no matter which entry point
// triggered it. Loaded unconditionally in index.html (like theme.js), so
// it runs on every page, not just the learning page - see the design
// discussion this replaced (a badge tucked inside the learning-page-only
// profile dropdown would never be seen from the /profiles launch screen).
//
// Plain global functions/vars, not a module - same reasoning as every
// other <script src> pair on this page (see settings.js's own header
// comment): they all share one global lexical scope, and settings.js
// (loaded later on the learning page) calls straight into loadUpdateStatus/
// runUpdateAction/describeUpdate below for its Updates tab.
//
// sessionStorage (not localStorage) for the "skipped" flag: it needs to
// persist across this app's own page-to-page navigation (a real page
// reload every time - /profiles, /learning, etc. are separate server-
// rendered routes, not an SPA) for as long as this one launch's webview
// window stays open, but must NOT survive to the next app launch - a new
// pywebview window is a genuinely new browsing session, so sessionStorage
// resets there on its own without any extra code.

const updateBellBtn = document.getElementById('updateBellBtn');
const updateBellDot = document.getElementById('updateBellDot');
const updateBellDropdown = document.getElementById('updateBellDropdown');
const updateNotificationList = document.getElementById('updateNotificationList');
const updateBellEmptyText = document.getElementById('updateBellEmptyText');

const updateDetailOverlay = document.getElementById('updateDetailOverlay');
const updateDetailTitle = document.getElementById('updateDetailTitle');
const updateDetailMessage = document.getElementById('updateDetailMessage');
const updateDetailProgressWrap = document.getElementById('updateDetailProgressWrap');
const updateDetailProgressFill = document.getElementById('updateDetailProgressFill');
const updateDetailActionBtn = document.getElementById('updateDetailActionBtn');
const updateDetailSkipBtn = document.getElementById('updateDetailSkipBtn');
const updateDetailStatus = document.getElementById('updateDetailStatus');
const closeUpdateDetailBtn = document.getElementById('closeUpdateDetailBtn');

const firstRunOverlay = document.getElementById('firstRunOverlay');
const firstRunTitle = document.getElementById('firstRunTitle');
const firstRunMessage = document.getElementById('firstRunMessage');
const firstRunProgressWrap = document.getElementById('firstRunProgressWrap');
const firstRunProgressFill = document.getElementById('firstRunProgressFill');
const firstRunStatus = document.getElementById('firstRunStatus');
const firstRunRetryBtn = document.getElementById('firstRunRetryBtn');

let latestUpdateStatus = null; // {app: {current, latest, update_available}, assets: {current, latest, update_available}, marketing: {current, latest, update_available}}
let latestWhatsNewStatus = null; // {available, version}
let latestMilestoneStatus = null; // {new_milestones: [{id, message}, ...]} - profile-scoped, see loadMilestoneStatus

function updateIsSkipped() {
  return sessionStorage.getItem('thirtytutors_update_skipped') === '1';
}

// Summary text only - what's available, not HOW it gets applied (see
// buildUpdateSteps below for that). The actual update always proceeds the
// same deterministic way regardless of which combination is pending:
// download everything that's pending, one step at a time with its own
// visible progress, then relaunch - always, no exceptions, no per-
// combination branching. An app-code update genuinely needs the process to
// restart to load new modules; a plain asset/marketing update doesn't
// strictly need one (StaticFiles serves them straight off disk - see
// main.py), but relaunching anyway keeps this ONE simple code path instead
// of a different one per combination, and it's also the only way someone
// visiting mid-session ever actually SEES a refreshed /landing, since that
// page is only ever organically visited right after each launch.
function describeUpdate(status) {
  if (!status) return null;
  const appUp = status.app && status.app.update_available;
  const assetsUp = status.assets && status.assets.update_available;
  const marketingUp = status.marketing && status.marketing.update_available;
  if (!appUp && !assetsUp && !marketingUp) return null;

  const parts = [];
  if (appUp) parts.push(`a new version of ThirtyTutors (v${status.app.current} \u2192 v${status.app.latest})`);
  if (assetsUp) parts.push('updated avatars/voices/photos');
  if (marketingUp) parts.push('updated landing-page assets');

  const message = parts.length === 1
    ? `Available: ${parts[0]}.`
    : `Available: ${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}.`;

  return { message, actionLabel: 'Update & Relaunch' };
}

// The actual ordered step list to run, built fresh from whatever's
// currently pending - no combination gets its own special-cased logic,
// runUpdateAction below just executes whichever of these exist, in order,
// the same way every time regardless of how many or which ones there are.
// pollProgress:true steps report a live 0-100% while running (see
// pollDownloadProgress) - the app-install step doesn't, since pip gives no
// reliable byte-progress signal to poll, so it just shows its label with
// no percentage.
function buildUpdateSteps(status) {
  const steps = [];
  if (status.app && status.app.update_available) {
    steps.push({
      label: `Installing version ${status.app.latest}...`,
      doneLabel: `\u2713 Installed v${status.app.latest}`,
      pollProgress: false,
      run: () => fetch('/api/update-app', { method: 'POST' }),
    });
  }
  if (status.assets && status.assets.update_available) {
    steps.push({
      label: 'Downloading avatars/voices/photos...',
      doneLabel: '\u2713 Avatars/voices/photos updated',
      pollProgress: true,
      run: () => fetch('/api/update-assets', { method: 'POST' }),
    });
  }
  if (status.marketing && status.marketing.update_available) {
    steps.push({
      label: 'Downloading landing-page assets...',
      doneLabel: '\u2713 Landing-page assets updated',
      pollProgress: true,
      run: () => fetch('/api/update-marketing-assets', { method: 'POST' }),
    });
  }
  return steps;
}

// Polls GET /api/download-progress every 400ms while a pollProgress:true
// step is in flight, translating raw byte counts into a 0-100 percentage
// (or null, in the brief window right at connection start before a
// Content-Length header has been seen yet) for onProgress to render
// however it wants - a real bar in the bell's detail modal, plain "label
// NN%" text everywhere else, see runUpdateAction below. Returns a stop()
// function callers MUST call once their fetch settles (success or
// failure), or this keeps polling forever.
function pollDownloadProgress(onProgress) {
  const interval = setInterval(async () => {
    try {
      const res = await fetch('/api/download-progress');
      const p = await res.json();
      const pct = p.total ? Math.min(100, Math.round((p.downloaded / p.total) * 100)) : null;
      onProgress(pct);
    } catch (e) {
      // A single missed poll isn't worth surfacing - the next tick tries again.
    }
  }, 400);
  return () => clearInterval(interval);
}

// setStatus: (text) => void - required, every entry point (bell modal,
// profile-dropdown item, Settings tab) has at least a plain text line.
// setProgress: (pct: number|null) => void, optional - only the bell's
// detail modal wires up a real visual bar (see its click handler below);
// every other entry point just folds the percentage straight into its own
// setStatus text instead (see pollProgress's onProgress branch above),
// since there's no room for a real bar there.
async function runUpdateAction(setStatus, actionBtn, setProgress) {
  const steps = buildUpdateSteps(latestUpdateStatus);
  if (steps.length === 0) return;
  actionBtn.disabled = true;
  try {
    for (const step of steps) {
      setStatus(step.label);
      if (setProgress) setProgress(step.pollProgress ? 0 : null);
      const stopPolling = step.pollProgress
        ? pollDownloadProgress((pct) => {
            if (setProgress) setProgress(pct);
            else setStatus(pct == null ? step.label : `${step.label} ${pct}%`);
          })
        : null;
      try {
        const res = await step.run();
        if (!res.ok) throw new Error(await res.text());
      } finally {
        if (stopPolling) stopPolling();
      }
      setStatus(step.doneLabel);
      if (setProgress) setProgress(100);
    }
    setStatus('Restarting...');
    if (setProgress) setProgress(null);
    // Doesn't await the response body meaningfully - the window closes
    // itself shortly after the server sends this, so there's nothing
    // further to react to here even on success.
    fetch('/api/restart-app', { method: 'POST' }).catch(() => {});
  } catch (e) {
    setStatus('Update failed - check your connection and try again.');
    if (setProgress) setProgress(null);
    actionBtn.disabled = false;
  }
}

// One entry per pending notification. Each kind owns its own dismiss rule:
// an update stays pending as long as it's genuinely still available (skip
// only quiets the badge for this session, per updateIsSkipped above) -
// what's-new has nothing to "resolve", so visiting its page is what clears
// it (see routes_pages.py's /whats-new, which marks the version seen on
// every request).
function buildNotifications() {
  const notifications = [];

  const updateInfo = describeUpdate(latestUpdateStatus);
  if (updateInfo) {
    notifications.push({
      kind: 'update',
      message: updateInfo.message,
      countsTowardBadge: !updateIsSkipped(),
      onClick: () => {
        closeBellDropdown();
        openUpdateDetail();
      },
    });
  }

  if (latestWhatsNewStatus && latestWhatsNewStatus.available) {
    notifications.push({
      kind: 'whats_new',
      message: `See what's new in v${latestWhatsNewStatus.version}.`,
      countsTowardBadge: true,
      onClick: () => {
        window.location.href = '/whats-new';
      },
    });
  }

  if (latestMilestoneStatus && latestMilestoneStatus.new_milestones) {
    latestMilestoneStatus.new_milestones.forEach((m) => {
      notifications.push({
        kind: 'milestone',
        message: `\ud83c\udfc6 ${m.message}`,
        countsTowardBadge: true,
        onClick: () => {
          closeBellDropdown();
          // Clears this milestone out of the client's own state right
          // away. The server already marks a milestone "seen" the moment
          // loadMilestoneStatus first fetches it (see stats.
          // get_new_milestones's docstring) - a later GET genuinely won't
          // return it again - but nothing was removing it from this
          // already-fetched latestMilestoneStatus array or repainting the
          // bell, so without this it stayed showing as unread for the
          // rest of this page's lifetime even though it had already been
          // viewed and clicked.
          latestMilestoneStatus.new_milestones = latestMilestoneStatus.new_milestones.filter((x) => x !== m);
          refreshUpdateUI();
          // The Stats tab this should open lives inside the Settings modal,
          // which currently only exists on the learning page (settings.js) -
          // openSettingsModal is looked up by name here rather than
          // imported, so this only resolves on pages where that script is
          // actually loaded; elsewhere this just takes them to the
          // learning page instead of silently doing nothing.
          if (typeof openSettingsModal === 'function') {
            openSettingsModal().then(() => selectSettingsCategory('stats'));
          } else {
            window.location.href = '/';
          }
        },
      });
    });
  }

  return notifications;
}

function closeBellDropdown() {
  updateBellDropdown.classList.remove('visible');
}

function openUpdateDetail() {
  const info = describeUpdate(latestUpdateStatus);
  if (!info) return;
  updateDetailTitle.textContent = 'Update available';
  updateDetailMessage.textContent = info.message;
  updateDetailActionBtn.textContent = info.actionLabel;
  updateDetailActionBtn.disabled = false;
  updateDetailStatus.textContent = '';
  updateDetailProgressWrap.hidden = true;
  updateDetailProgressFill.style.width = '0%';
  updateDetailOverlay.classList.add('visible');
  // Forces an immediate repaint - see profileDetail.js's openProfileDetail
  // for the full explanation of this same display:none/flex toggle glitch.
  void updateDetailOverlay.offsetHeight;
}

function closeUpdateDetail() {
  updateDetailOverlay.classList.remove('visible');
}

// Rebuilds every surface's visibility/labels from the current state -
// called after every fetch and after any state change (skip, returning
// from the what's-new page).
//
// The bell itself is ALWAYS visible (a fixed, always-clickable part of
// the top bar - a notification icon that sometimes just isn't there
// isn't recognizable as "check here"). Only its badge count and dropdown
// content change.
//
// The bell dropdown is a plain LIST - one row per pending notification,
// no action buttons in it. Clicking a row runs that notification's own
// onClick (see buildNotifications above) - an update opens the detail
// modal, where "Update & Relaunch"/"Skip for now" actually live; what's-
// new navigates straight to its page.
function refreshUpdateUI() {
  const notifications = buildNotifications();
  const badgeCount = notifications.filter((n) => n.countsTowardBadge).length;

  updateBellDot.hidden = badgeCount === 0;
  updateBellDot.textContent = badgeCount > 9 ? '9+' : String(badgeCount);

  updateNotificationList.innerHTML = '';
  notifications.forEach((n) => {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'update-notification-item';
    row.textContent = n.message;
    row.addEventListener('click', n.onClick);
    updateNotificationList.appendChild(row);
  });
  updateBellEmptyText.hidden = notifications.length > 0;

  // Learning page's profile dropdown (settings.js) - only exists on that
  // page. Shown there whenever an update is available, regardless of
  // skip - this is the deliberately-always-reachable entry point once a
  // person has been shown the bell at least once. What's-new doesn't get
  // an entry here - it's a one-time thing, reachable only through the
  // bell and, permanently, through Settings -> About.
  const info = describeUpdate(latestUpdateStatus);
  const hasUpdate = !!info;
  const settingsDropdown = document.getElementById('settingsDropdown');
  if (settingsDropdown) {
    let item = document.getElementById('updateDropdownItem');
    if (hasUpdate && !item) {
      item = document.createElement('button');
      item.id = 'updateDropdownItem';
      item.className = 'settings-dropdown-item';
      item.type = 'button';
      item.addEventListener('click', () => {
        // No restore-label-on-success logic here (unlike a version of this
        // that used to exist): every successful run now ends in a
        // relaunch (see runUpdateAction) - the window closes itself, so
        // there's nothing left to restore. The only way this ever
        // resolves with the button still enabled is a FAILURE, and
        // overwriting that message back to the generic label would just
        // hide the one piece of information ("why didn't this work")
        // that's actually useful to leave on screen until they retry.
        runUpdateAction((text) => { item.textContent = text; }, item);
      });
      settingsDropdown.insertBefore(item, settingsDropdown.firstChild);
    }
    if (item) {
      item.hidden = !hasUpdate;
      if (info) item.textContent = '\ud83d\udd04 ' + info.actionLabel;
    }
  }
}

async function loadUpdateStatus(force) {
  try {
    const res = await fetch(`/api/update-status${force ? '?force=true' : ''}`);
    latestUpdateStatus = await res.json();
  } catch (e) {
    latestUpdateStatus = null;
  }
  refreshUpdateUI();
  return latestUpdateStatus;
}

// True only on a genuinely fresh install with no avatar/voice/photo bundle
// downloaded yet - assets.current is null specifically when no local
// version marker exists at all (see updater.check_assets_update), which
// is a stronger condition than assets.update_available (that's also true
// for an ordinary stale-but-present bundle). Used only to pick which
// wording maybeStartAutoUpdate shows below, NOT to gate whether it runs -
// see that function for the actual (broader) trigger.
function isFirstRunAssetsMissing(status) {
  return !!(status && status.assets && status.assets.current === null);
}

let autoUpdateStarted = false;

// Runs the exact same download steps as a manual "Update & Relaunch"
// (buildUpdateSteps/runUpdateAction above) - whatever combination of app/
// assets/marketing updates happens to be pending - but triggered
// automatically with no click, behind a full-screen non-dismissible
// overlay instead of the bell's small modal. Ends in the same relaunch
// either way, so the next launch lands back here with nothing pending and
// this becomes a no-op for good (until the next real update ships).
//
// Covers every case describeUpdate does, not just the one-time "assets
// never downloaded at all" case this used to be limited to - that's still
// the one case that gets its own wording below (a first run genuinely
// can't do anything useful without the avatar bundle, so it reads as
// setup rather than an update), everything else shows a generic
// "Updating ThirtyTutors" message built from describeUpdate's own summary
// text, the same text the bell's detail modal would otherwise have shown.
//
// On failure (see runUpdateAction's own catch), this just leaves the
// overlay showing the failure message with Retry available - it does NOT
// fall through to launching the app on the current version, since the
// overlay covers the whole page underneath it either way; a person stuck
// offline can still leave the app running here and it'll succeed the
// moment their connection comes back, same as any other retry.
async function maybeStartAutoUpdate() {
  const info = describeUpdate(latestUpdateStatus);
  if (autoUpdateStarted || !info) return;
  autoUpdateStarted = true;

  const firstRun = isFirstRunAssetsMissing(latestUpdateStatus);
  firstRunTitle.textContent = firstRun ? 'Setting up ThirtyTutors' : 'Updating ThirtyTutors';
  firstRunMessage.textContent = firstRun
    ? "Downloading your 3D tutor avatars and voices - this only happens once. Make sure you're connected to the internet."
    : info.message;

  firstRunRetryBtn.hidden = true;
  firstRunStatus.textContent = '';
  firstRunProgressWrap.hidden = true;
  firstRunProgressFill.style.width = '0%';
  firstRunOverlay.classList.add('visible');
  void firstRunOverlay.offsetHeight; // forces a repaint - see openUpdateDetail above for the same pattern

  await runUpdateAction(
    (text) => { firstRunStatus.textContent = text; },
    firstRunRetryBtn,
    (pct) => {
      firstRunProgressWrap.hidden = pct == null;
      if (pct != null) firstRunProgressFill.style.width = `${pct}%`;
    }
  );
  // Only reachable on failure - a success ends in a relaunch that tears
  // this whole page down before runUpdateAction's promise resolves.
  autoUpdateStarted = false;
  firstRunRetryBtn.hidden = false;
}

firstRunRetryBtn.addEventListener('click', maybeStartAutoUpdate);

async function loadWhatsNewStatus() {
  try {
    const res = await fetch('/api/whats-new-status');
    latestWhatsNewStatus = await res.json();
  } catch (e) {
    latestWhatsNewStatus = null;
  }
  refreshUpdateUI();
  return latestWhatsNewStatus;
}

// Profile-scoped, unlike every other status check on this page - reads
// tutorProfileId straight out of localStorage rather than a currentProfile
// global, since this file runs on pages (e.g. /profiles) that never define
// one. No-ops quietly if no profile has been picked yet (first run, still
// on the landing page) - there's nothing to check milestones against.
async function loadMilestoneStatus() {
  const profileId = localStorage.getItem('tutorProfileId');
  if (!profileId) {
    latestMilestoneStatus = null;
    return;
  }
  try {
    const res = await fetch(`/api/profiles/${profileId}/milestone-status`);
    latestMilestoneStatus = await res.json();
  } catch (e) {
    latestMilestoneStatus = null;
  }
  refreshUpdateUI();
}

updateBellBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  updateBellDropdown.classList.toggle('visible');
  // Forces an immediate repaint - see profileDetail.js's openProfileDetail
  // for the full explanation of this same display:none/block toggle glitch.
  void updateBellDropdown.offsetHeight;
});
document.addEventListener('click', (e) => {
  if (!updateBellDropdown.contains(e.target) && e.target !== updateBellBtn) closeBellDropdown();
});

closeUpdateDetailBtn.addEventListener('click', closeUpdateDetail);
updateDetailOverlay.addEventListener('click', (e) => {
  if (e.target === updateDetailOverlay) closeUpdateDetail();
});
updateDetailSkipBtn.addEventListener('click', () => {
  sessionStorage.setItem('thirtytutors_update_skipped', '1');
  closeUpdateDetail();
  refreshUpdateUI();
});
updateDetailActionBtn.addEventListener('click', () => {
  runUpdateAction(
    (text) => { updateDetailStatus.textContent = text; },
    updateDetailActionBtn,
    (pct) => {
      updateDetailProgressWrap.hidden = pct == null;
      if (pct != null) updateDetailProgressFill.style.width = `${pct}%`;
    }
  );
});

loadUpdateStatus(false).then(maybeStartAutoUpdate);
loadWhatsNewStatus();
loadMilestoneStatus();
