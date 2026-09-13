// ThirtyTutors - avatar drawer for the live learning page. Toggled by the
// camera button in the top bar; shows the current conversation's tutor
// avatar and, once loaded, becomes the actual audio output path so its
// mouth moves in sync with the real conversation (not a canned sample -
// see registerAvatarAudioSink in audio.js for why that requires routing
// playback through this avatar's own AudioContext).
//
// This is an ES module (needs bare imports for three/talkinghead), unlike
// every other learning-page script - so it can't see their `let`/`const`
// globals directly. It reaches them via `window.X`, which only works for
// values those scripts expose that way on purpose (currentVoiceName is
// declared with `var` in state.js specifically for this; functions like
// registerAvatarAudioSink are plain top-level declarations, which are
// `window` properties automatically).

import { TalkingHead } from 'talkinghead';
import { HeadAudio } from '/vendor/headaudio/modules/headaudio.mjs';

const cameraToggleBtn = document.getElementById('cameraToggleBtn');
const bodyEl = document.getElementById('body');
const avatarDrawer = document.getElementById('avatarDrawer');
const avatarDrawerFrame = document.getElementById('avatarDrawerFrame');
const avatarDrawerPreview = document.getElementById('avatarDrawerPreview');
const avatarDrawerHint = document.getElementById('avatarDrawerHint');
const avatarMaximizeBtn = document.getElementById('avatarMaximizeBtn');
const avatarViewSelect = document.getElementById('avatarViewSelect');

// See avatarPreview.js for the full reasoning - this isn't primarily an
// amplitude issue, it's a SPEED one: setValue()'s default smoothing (tuned
// for idle motion) can't keep up with phoneme-speed viseme swings, so
// visemes get the same fast-acceleration override TalkingHead's own code
// already gives eye blinks. MOUTH_BOOST is a smaller amplitude nudge on
// top of that.
const MOUTH_BOOST = 1.5;
const VISEME_ACC = 0.15;
const VISEME_MAXV = 15;

let head = null;
let headaudio = null;
let avatarReady = false;
let isMaximized = false;

// Exposed on window (see the module-vs-classic-script note at the top of
// this file) so websocket.js and audio.js - both plain classic scripts -
// can drive the avatar's expression. No-ops until the drawer has been
// opened at least once (head is null until ensureAvatarReady() runs, e.g.
// on cameraToggleBtn's first click) - callers don't need to check
// readiness themselves.
//
// Each mood also fires a matching hand/arm gesture where one makes sense -
// mapping decided by trying every one of TalkingHead's 8 built-in gestures
// on static/avatar_test/test.html (see
// design_plans/ROLEPLAY_HANDSFREE_AND_GESTURES.md). 'sad' maps to
// thumbdown despite reading a little blunt for what's meant to be a gentle
// correction - the library has nothing softer (no headshake/wince
// equivalent among the 8), and thumbdown was judged the closest fit.
// 'neutral' intentionally has no entry, so no gesture fires for it - just
// the facial mood change.
const MOOD_GESTURES = {
  happy: 'thumbup',
  love: 'namaste',
  fear: 'shrug',
  sad: 'thumbdown',
};
const MOOD_GESTURE_DURATION_S = 2.5;

function setAvatarMood(mood) {
  if (!head) return;
  if (typeof head.setMood === 'function') head.setMood(mood);
  const gesture = MOOD_GESTURES[mood];
  if (gesture && typeof head.playGesture === 'function') head.playGesture(gesture, MOOD_GESTURE_DURATION_S);
  installSafePoseLoop();
}
window.setAvatarMood = setAvatarMood;

// TalkingHead's vendored source (vendor/talkinghead/modules/talkinghead.mjs)
// gives each mood its own infinite idle pose-cycling animation as part of
// animMoods[mood].anims (a 'pose'-named entry with alt/probability weights
// picking from poseTemplates every 5-30s) - head.setMood() reinstalls that
// mood's own version of this animation into head.animQueue every single
// time it's called, which is why this has to run after every setMood call
// too, not just once after the avatar loads. Several moods ('sad', 'fear',
// 'love') specifically include 'oneknee'/'kneel'/'sitting' as options, but
// ONLY in the branch that fires when the camera view is 'full' (fullscreen
// - see enterMaximize() below) - which is why kneeling/sitting only ever
// showed up in fullscreen, and only for certain moods.
//
// This removes whatever 'pose' animation is currently queued and replaces
// it with an equivalent infinite loop built the same way (head.animFactory
// with loop=-1, same as setMood's own internal usage), just restricted to
// standing poses only - same mechanism the library already uses, just with
// kneeling/sitting/lying excluded regardless of mood or view.
const SAFE_POSE_NAMES = ['side', 'hip', 'straight', 'wide'];
function installSafePoseLoop() {
  if (!head) return;
  const i = head.animQueue.findIndex((x) => x.template.name === 'pose');
  if (i !== -1) head.animQueue.splice(i, 1);
  head.animQueue.push(
    head.animFactory(
      { name: 'pose', alt: SAFE_POSE_NAMES.map((name) => ({ delay: [5000, 30000], vs: { pose: [name] } })) },
      -1
    )
  );
}

// Same VRoid/ARKit blend-shape gap workaround as avatarPreview.js - see
// that file for the full explanation. Duplicated rather than shared since
// these are two separate pages/documents with no module loader connecting
// them.
const ARKIT_BLEND_SHAPES = [
  'browDownLeft', 'browDownRight', 'browInnerUp', 'browOuterUpLeft', 'browOuterUpRight',
  'cheekPuff', 'cheekSquintLeft', 'cheekSquintRight',
  'eyeBlinkLeft', 'eyeBlinkRight',
  'eyeLookDownLeft', 'eyeLookDownRight', 'eyeLookInLeft', 'eyeLookInRight',
  'eyeLookOutLeft', 'eyeLookOutRight', 'eyeLookUpLeft', 'eyeLookUpRight',
  'eyeSquintLeft', 'eyeSquintRight', 'eyeWideLeft', 'eyeWideRight',
  'jawForward', 'jawLeft', 'jawOpen', 'jawRight',
  'mouthClose', 'mouthDimpleLeft', 'mouthDimpleRight', 'mouthFrownLeft', 'mouthFrownRight',
  'mouthFunnel', 'mouthLeft', 'mouthLowerDownLeft', 'mouthLowerDownRight',
  'mouthPressLeft', 'mouthPressRight', 'mouthPucker', 'mouthRight',
  'mouthRollLower', 'mouthRollUpper', 'mouthShrugLower', 'mouthShrugUpper',
  'mouthSmileLeft', 'mouthSmileRight', 'mouthStretchLeft', 'mouthStretchRight',
  'mouthUpperUpLeft', 'mouthUpperUpRight', 'noseSneerLeft', 'noseSneerRight', 'tongueOut',
  'eyesLookDown', 'eyesLookUp',
];

function stubMissingBlendShapes() {
  ARKIT_BLEND_SHAPES.forEach((name) => {
    if (!(name in head.mtAvatar)) {
      const entry = {
        fixed: null, realtime: null, system: null, systemd: null, newvalue: null, ref: null,
        min: head.mtMinExceptions.hasOwnProperty(name) ? head.mtMinExceptions[name] : head.mtMinDefault,
        max: head.mtMaxExceptions.hasOwnProperty(name) ? head.mtMaxExceptions[name] : head.mtMaxDefault,
        easing: head.mtEasingDefault, base: null, v: 0, needsUpdate: false,
        acc: (head.mtAccExceptions.hasOwnProperty(name) ? head.mtAccExceptions[name] : head.mtAccDefault) / 1000,
        maxv: (head.mtMaxVExceptions.hasOwnProperty(name) ? head.mtMaxVExceptions[name] : head.mtMaxVDefault) / 1000,
        limit: head.mtLimits.hasOwnProperty(name) ? head.mtLimits[name] : null,
        onchange: head.mtOnchange.hasOwnProperty(name) ? head.mtOnchange[name] : null,
        baseline: head.mtBaselineExceptions.hasOwnProperty(name) ? head.mtBaselineExceptions[name] : head.mtBaselineDefault,
        ms: [], is: [],
      };
      entry.value = entry.baseline;
      entry.applied = entry.baseline;
      head.mtAvatar[name] = entry;
    }
  });
}

async function ensureAvatarReady() {
  if (avatarReady) return;
  avatarDrawerHint.textContent = 'Loading avatar...';

  head = new TalkingHead(avatarDrawerPreview, {
    cameraView: 'upper',
    dracoEnabled: true,
    dracoDecoderPath: '/vendor/three/jsm/libs/draco/',
    lipsyncLang: 'en',
    lipsyncModules: ['en'],
    avatarMood: 'neutral',
  });

  await head.audioCtx.audioWorklet.addModule('/vendor/headaudio/modules/headworklet.mjs');
  headaudio = new HeadAudio(head.audioCtx, {
    processorOptions: {},
    parameterData: { vadGateActiveDb: -40, vadGateInactiveDb: -60 },
  });
  await headaudio.loadModel('/vendor/headaudio/dist/model-en-mixed.bin');
  headaudio.onvalue = (key, value) => {
    if (key.startsWith('viseme_') && key !== 'viseme_sil') {
      value = Math.min(1, value * MOUTH_BOOST);
    }
    head.setValue(key, value);
  };
  head.opt.update = headaudio.update.bind(headaudio);

  // Must happen before showAvatar() - see the reasoning at the top of the
  // file. showAvatar() (and stubMissingBlendShapes) build each mtAvatar
  // entry's acc/maxv from these exception dicts at creation time.
  head.visemeNames.forEach((v) => {
    head.mtAccExceptions['viseme_' + v] = VISEME_ACC;
    head.mtMaxVExceptions['viseme_' + v] = VISEME_MAXV;
  });
  head.mtAccExceptions['jawOpen'] = VISEME_ACC;
  head.mtMaxVExceptions['jawOpen'] = VISEME_MAXV;

  const voiceName = window.currentVoiceName || 'Kore';
  applyTutorBackground(voiceName);
  try {
    await head.showAvatar({ url: `/avatar/${voiceName}_th.glb` });
    stubMissingBlendShapes();
    installSafePoseLoop(); // showAvatar() ends by calling setMood() itself, which installs the mood's own (unsafe) pose loop - strip it immediately
    avatarDrawerHint.textContent = '';
  } catch (e) {
    avatarDrawerHint.textContent = 'Avatar could not load.';
    console.error(e);
  }

  // From here on, TTS playback routes through this avatar's AudioContext
  // instead of the standalone one audio.js otherwise creates - see
  // registerAvatarAudioSink there for why.
  window.registerAvatarAudioSink(head.audioCtx, headaudio);

  avatarReady = true;
}

// Per-tutor background photo BEHIND the avatar itself, inside
// #avatarDrawerFrame (covers both the small drawer view and fullscreen -
// same element in both, fullscreen just resizes it via CSS, see
// avatar_drawer.css) - not the wider learning-page background. See
// static/backgrounds/ (gitignored, not part of the real asset pipeline
// yet - this whole thing goes away once the real version lands, loaded
// from a downloaded/versioned background-photos bundle, same asset-
// pipeline pattern as avatars/voices/tile-photos, surfaced through the
// Settings > Updates "Update & Relaunch" flow rather than a static file
// dropped directly into the package tree). Every VOICE_OPTIONS name has a
// matching {name}_background.webp in that folder, so the URL is derived
// straight from voiceName rather than kept as a hand-maintained map - if a
// future voice is added without artwork yet, a missing background image
// just fails to load silently (CSS background-image, not <img> - no
// broken-image icon), falling back to the frame's plain panel color
// exactly as before. Relies on TalkingHead's canvas already rendering
// with a transparent background - #avatarDrawerFrame's own background-
// color (var(--panel-raised)) was already the only thing visible behind
// the avatar before this, which only makes sense if the canvas itself has
// no opaque clear color of its own.
function applyTutorBackground(voiceName) {
  avatarDrawerFrame.style.backgroundImage = voiceName ? `url('/backgrounds/${voiceName}_background.webp')` : '';
}

cameraToggleBtn.addEventListener('click', async () => {
  const opening = !avatarDrawer.classList.contains('open');
  if (opening) {
    avatarDrawer.classList.add('open');
    avatarMaximizeBtn.style.display = 'inline-flex';
    try {
      await ensureAvatarReady();
      if (head.audioCtx.state === 'suspended') await head.audioCtx.resume();
    } catch (e) {
      avatarDrawerHint.textContent = 'Avatar preview could not start.';
      console.error(e);
    }
  } else {
    avatarDrawer.classList.remove('open');
    avatarMaximizeBtn.style.display = 'none';
    avatarViewSelect.style.display = 'none';
    if (isMaximized) exitMaximize();
  }
});

function enterMaximize() {
  isMaximized = true;
  bodyEl.classList.add('avatar-fullscreen');
  avatarMaximizeBtn.textContent = '⤡';
  avatarMaximizeBtn.title = 'Minimize avatar';
  avatarViewSelect.style.display = 'inline-block';
  avatarViewSelect.value = 'upper';
  if (head) head.setView('upper');
}

function exitMaximize() {
  isMaximized = false;
  bodyEl.classList.remove('avatar-fullscreen');
  avatarMaximizeBtn.textContent = '⤢';
  avatarMaximizeBtn.title = 'Maximize avatar';
  avatarViewSelect.style.display = 'none';
  if (head) head.setView('upper');
}

avatarMaximizeBtn.addEventListener('click', () => {
  if (isMaximized) exitMaximize(); else enterMaximize();
});

avatarViewSelect.addEventListener('change', () => {
  if (head) head.setView(avatarViewSelect.value);
});
