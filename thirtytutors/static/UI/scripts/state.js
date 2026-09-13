// ThirtyTutors - shared DOM references + cross-concern state.
//
// This file must load first (every other script reads these at
// top-level). Variables declared here with `let`/`const` are ordinary
// top-level script declarations - browsers share one global scope across
// sibling <script src> tags, so every other file can reference these
// directly with no import/export wiring needed. Keep names unique across
// the whole scripts/ directory.
//
// currentVoiceName is deliberately `var`, not `let` - top-level `var`
// (and function declarations) in classic scripts attach to `window`,
// which is what lets avatarDrawer.js (an ES module, with its own isolated
// module scope) read it as `window.currentVoiceName`. `let`/`const`
// bindings are NOT reachable that way, so this one exception is load-bearing.

// --- DOM references ---

const talkBtn = document.getElementById('talkBtn');
const talkHint = document.getElementById('talkHint');
const handsFreeBtn = document.getElementById('handsFreeBtn');
const connectionDot = document.getElementById('connectionDot');
const modelDot = document.getElementById('modelDot');
const modelNameText = document.getElementById('modelNameText');
const statusToast = document.getElementById('statusToast');
const statusToastIcon = document.getElementById('statusToastIcon');
const statusToastMessage = document.getElementById('statusToastMessage');
const statusToastRetryBtn = document.getElementById('statusToastRetryBtn');
const transcriptArea = document.getElementById('transcriptArea');
const emptyState = document.getElementById('emptyState');
const waveformCanvas = document.getElementById('waveform');
const waveformCtx = waveformCanvas.getContext('2d');
const sessionStatusText = document.getElementById('sessionStatusText');

// --- Shared mutable state ---
// (profile/conversation identity and the live socket - referenced across
// profiles.js, conversations.js, audio.js, websocket.js, and, via
// window.currentVoiceName, avatarDrawer.js)
//
// currentProfile/conversationsCache/currentConversationId are declared in
// profileMenu.js instead of here now (loaded globally in index.html,
// before this file) - the top-bar profile menu needs currentProfile on
// every page, not just this one. They're the same shared top-level
// bindings either way; this file just uses them rather than declaring
// them.
var currentVoiceName = null;
var currentVoiceAlias = null;

let ws = null;
let reconnectTimer = null;
let modelLabels = {}; // model id -> display label, fetched once in websocket.js

// Set by quizDrawer.js while the quiz drawer is open; read by audio.js's
// push-to-talk/hands-free gating (see the 6.4 no-mid-quiz-interruption
// decision). Declared here rather than in quizDrawer.js itself so it's
// available regardless of script load order, same reasoning as every
// other shared flag on this page.
let quizActive = false;
