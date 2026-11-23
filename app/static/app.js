/* =========================================================================
   Relay - browser side

   Sections:
     1. State & helpers
     2. REST API calls
     3. WebSocket (real-time events, auto-reconnect)
     4. Sign in / sign out
     5. Rendering: room list, chat, messages, typing
     6. Sending, typing and read events
     7. Photos, videos, voice notes and files
     8. Ticks, replies, editing and deleting
     9. Sounds and notifications
    10. Dialogs (new message, new group, browse groups)
    11. Start-up

   Security note: user content is always inserted with textContent, never
   innerHTML, so nobody can inject HTML/JavaScript through a message (XSS).
   ========================================================================= */

"use strict";

/* ---------------------------------------------------------------------------
   1. State & helpers
   --------------------------------------------------------------------------- */
const state = {
  token: localStorage.getItem("relay_token"),
  me: null,                 // {id, username}
  rooms: new Map(),         // room id -> room
  messages: new Map(),      // room id -> {list: [], hasMore: bool, loaded: bool}
  activeRoomId: null,
  online: new Set(),        // user ids online right now
  typing: new Map(),        // room id -> Map(user id -> {username, timer})
  socket: null,
  reconnectDelay: 1000,
  reconnectTimer: null,
  pingTimer: null,
  everConnected: false,
  isTyping: false,
  typingStopTimer: null,
  lastTypingSent: 0,
  config: { edit_window_minutes: 15, delete_for_everyone_hours: 48 },
  replyTo: null,            // message you're replying to
  editing: null,            // message you're editing
  menuMessage: null,        // message whose menu is open
  soundOn: localStorage.getItem("relay_sound") !== "off",
  staged: [],               // files picked but not sent yet: {file, previewUrl}
  recorder: null,           // {mediaRecorder, stream, chunks, timer, started, cancelled}
};

const $ = (id) => document.getElementById(id);

/** Create an element: el("p", {className: "x"}, "text", childElement) */
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("aria-")) node.setAttribute(key, value);
    else node[key] = value;
  }
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const AVATAR_COLORS = ["#7a4fb5", "#2f7d8c", "#b5543c", "#4f7a3a", "#a3437a", "#3f5fa8", "#8a6a1f"];

function avatarColor(name) {
  let hash = 0;
  for (const ch of name) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return AVATAR_COLORS[hash % AVATAR_COLORS.length];
}

function fillAvatar(node, name, { group = false, userId = null } = {}) {
  node.textContent = (name || "?").charAt(0).toUpperCase();
  node.style.setProperty("--avatar", avatarColor(name || "?"));
  node.classList.toggle("group", group);
  if (userId === null) delete node.dataset.online;
  else node.dataset.online = String(state.online.has(userId));
  node.setAttribute("aria-hidden", "true");
}

function otherMember(room) {
  return room.members.find((m) => m.id !== state.me.id) || room.members[0];
}

function roomTitle(room) {
  return room.is_direct ? otherMember(room).username : room.name;
}

function formatTime(iso) {
  return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function formatDay(iso) {
  const date = new Date(iso);
  const today = new Date();
  const yesterday = new Date();
  yesterday.setDate(today.getDate() - 1);
  if (date.toDateString() === today.toDateString()) return "Today";
  if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
  return date.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
}

function shortWhen(iso) {
  const date = new Date(iso);
  return date.toDateString() === new Date().toDateString()
    ? formatTime(iso)
    : date.toLocaleDateString([], { day: "numeric", month: "short" });
}

function lastSeenText(iso) {
  if (!iso) return "Offline";
  const day = formatDay(iso);
  const time = formatTime(iso);
  if (day === "Today") return `Last seen today at ${time}`;
  if (day === "Yesterday") return `Last seen yesterday at ${time}`;
  return `Last seen ${new Date(iso).toLocaleDateString([], { day: "numeric", month: "short" })}`;
}

function findMember(userId) {
  for (const room of state.rooms.values()) {
    const member = room.members.find((m) => m.id === userId);
    if (member) return member;
  }
  return null;
}

let toastTimer = null;
function toast(text) {
  const box = $("toast");
  box.textContent = text;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (box.hidden = true), 3500);
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function fileIcon(name, type = "") {
  if (type.startsWith("image/")) return "🖼️";
  if (type.startsWith("video/")) return "🎥";
  if (type.startsWith("audio/")) return "🎵";
  if (/\.pdf$/i.test(name)) return "📕";
  if (/\.(zip|rar|7z)$/i.test(name)) return "🗜️";
  if (/\.(xlsx?|csv)$/i.test(name)) return "📊";
  return "📄";
}

/** Short text for the conversation list, e.g. "📷 Photo" or "🎤 Voice message". */
function previewText(message) {
  if (message.deleted) return message.sender.id === state.me.id ? "You deleted this message" : "This message was deleted";
  const a = message.attachment;
  if (!a) return message.content;
  const label = a.kind === "image" ? "📷 Photo"
    : a.kind === "video" ? "🎥 Video"
    : a.kind === "audio" ? (a.name.startsWith("voice-message") ? "🎤 Voice message" : "🎵 Audio")
    : `📄 ${a.name}`;
  return message.content ? `${label}: ${message.content}` : label;
}

/** Media URLs carry the login token, because <img>/<video> can't send headers. */
function fileUrl(attachment) {
  return `${attachment.url}?token=${encodeURIComponent(state.token)}`;
}

function newClientId() {
  return `c${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

/* ---------------------------------------------------------------------------
   2. REST API calls
   --------------------------------------------------------------------------- */
function errorMessage(data, status) {
  if (data && typeof data.detail === "string") return data.detail;
  if (status === 429) return "Too many attempts. Wait a little and try again.";
  return status >= 500 ? "The server ran into a problem. Try again." : "That didn’t work. Try again.";
}

async function api(path, { method = "GET", body } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (state.token) headers.Authorization = `Bearer ${state.token}`;

  let response;
  try {
    response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new Error("Can’t reach the server. Check that it’s running.");
  }
  let data = null;
  try { data = await response.json(); } catch { /* empty body */ }

  if (response.status === 401 && state.token) {
    signOut("Your session has expired. Sign in again.");
  }
  if (!response.ok) {
    const error = new Error(errorMessage(data, response.status));
    error.status = response.status;
    error.fields = (data && data.errors) || {};  // e.g. {username: "...", password: "..."}
    throw error;
  }
  return data;
}

/* ---------------------------------------------------------------------------
   3. WebSocket
   --------------------------------------------------------------------------- */
function connectSocket() {
  clearTimeout(state.reconnectTimer);
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${location.host}/ws?token=${encodeURIComponent(state.token)}`);
  state.socket = socket;

  socket.addEventListener("open", () => {
    $("connection-banner").hidden = true;
    state.reconnectDelay = 1000;
    clearInterval(state.pingTimer);
    state.pingTimer = setInterval(() => send({ type: "ping" }), 25000); // keeps the connection alive
    if (state.everConnected) resyncAfterReconnect().then(acknowledgeAllRooms);
    else acknowledgeAllRooms();
    state.everConnected = true;
  });

  socket.addEventListener("message", (event) => {
    try { handleEvent(JSON.parse(event.data)); } catch (error) { console.error(error); }
  });

  socket.addEventListener("close", (event) => {
    clearInterval(state.pingTimer);
    if (state.socket !== socket) return;        // an old socket we replaced on purpose
    if (event.code === 4401) { signOut("Your session has expired. Sign in again."); return; }
    if (!state.token) return;                   // signed out
    $("connection-banner").hidden = false;
    state.reconnectTimer = setTimeout(connectSocket, state.reconnectDelay);
    state.reconnectDelay = Math.min(state.reconnectDelay * 2, 15000); // 1s, 2s, 4s ... max 15s
  });
}

function send(data) {
  if (state.socket && state.socket.readyState === WebSocket.OPEN) {
    state.socket.send(JSON.stringify(data));
    return true;
  }
  return false;
}

/** After a reconnect, fetch anything we missed while offline. */
async function resyncAfterReconnect() {
  await loadRooms();
  const roomId = state.activeRoomId;
  state.messages.clear();
  if (roomId && state.rooms.has(roomId)) await openRoom(roomId, { keepScroll: false });
}

function handleEvent(event) {
  switch (event.type) {
    case "online_users":
      state.online = new Set(event.user_ids);
      renderRoomList();
      renderChatHeader();
      break;

    case "presence":
      if (event.online) state.online.add(event.user_id);
      else state.online.delete(event.user_id);
      for (const room of state.rooms.values()) { // remember "last seen" everywhere they appear
        const member = room.members.find((m) => m.id === event.user_id);
        if (member && event.last_seen) member.last_seen = event.last_seen;
      }
      renderRoomList();
      renderChatHeader();
      break;

    case "message_updated":
      applyMessageUpdate(event.message);
      break;

    case "message_hidden":
      removeHiddenMessage(event.room_id, event.message_id);
      break;

    case "receipt":
      applyReceipt(event);
      break;

    case "message":
      receiveMessage(event.message, event.client_id);
      break;

    case "typing":
      receiveTyping(event.room_id, event.user, event.is_typing);
      break;

    case "room_added":
      state.rooms.set(event.room.id, event.room);
      renderRoomList();
      break;

    case "member_joined":
    case "member_left": {
      const room = state.rooms.get(event.room_id);
      if (!room) break;
      room.members = room.members.filter((m) => m.id !== event.user.id);
      room.receipts = (room.receipts || []).filter((r) => r.user_id !== event.user.id);
      if (event.type === "member_joined") {
        room.members.push(event.user);
        if (event.receipt) room.receipts.push(event.receipt);
      }
      renderChatHeader();
      if (room.id === state.activeRoomId) refreshTicks();
      break;
    }

    case "error":
      if (event.client_id) markFailed(event.client_id, event.detail);
      else toast(event.detail);
      break;
  }
}

/* ---------------------------------------------------------------------------
   4. Sign in / sign out
   --------------------------------------------------------------------------- */
/* The browser checks the same rules as the server (schemas.py), so problems
   show up right away. The server still checks everything again. */

let authMode = "login";
const touched = new Set();      // fields the person has already left once
let availabilityTimer = null;
let availabilityCheck = 0;      // used to ignore out-of-date answers
let availability = null;        // last answer: {username, available, message}

const AUTH_TEXT = {
  login: {
    title: "Welcome back", subtitle: "Sign in to pick up your conversations.",
    submit: "Sign in", busy: "Signing in…", switchText: "New to Relay?", switchButton: "Create an account",
  },
  register: {
    title: "Create your account", subtitle: "Choose a username people will find you by.",
    submit: "Create account", busy: "Creating account…", switchText: "Already have an account?", switchButton: "Sign in",
  },
};
const USERNAME_HINT = "3–20 characters: letters, numbers and _. Start with a letter.";
const FIELD_INPUTS = { username: "auth-username", password: "auth-password", confirm: "auth-confirm" };

const cleanUsername = () => $("auth-username").value.trim().toLowerCase();

function usernameProblem(value) { // same rules and wording as check_username() in schemas.py
  const name = value.trim().toLowerCase();
  if (!name) return "Enter a username.";
  if (/\s/.test(name)) return "Username can't contain spaces.";
  if (name.length < 3) return "Username must be at least 3 characters.";
  if (name.length > 20) return "Username can be at most 20 characters.";
  if (!/^[a-z0-9_]+$/.test(name)) return "Username can only use letters, numbers and underscores (_).";
  if (!/^[a-z]/.test(name)) return "Username must start with a letter.";
  return "";
}

const PASSWORD_MIN = 8;
const PASSWORD_MAX = 16;
const charCount = (text) => [...text].length; // counts an emoji as 1 character, like Python

function passwordRules(password, username) {
  const name = username.trim().toLowerCase();
  return {
    length: charCount(password) >= PASSWORD_MIN && charCount(password) <= PASSWORD_MAX,
    letter: /[A-Za-z]/.test(password),
    number: /\d/.test(password),
    spaces: password.length > 0 && !/\s/.test(password),
    username: name.length < 3 || !password.toLowerCase().includes(name),
  };
}

function passwordProblem(password, username) { // same order as check_password() in schemas.py
  if (!password) return "Enter a password.";
  if (/\s/.test(password)) return "Password can't contain spaces.";
  if (charCount(password) < PASSWORD_MIN) return `Password must be at least ${PASSWORD_MIN} characters.`;
  if (charCount(password) > PASSWORD_MAX) return `Password can be at most ${PASSWORD_MAX} characters.`;
  if (!/[A-Za-z]/.test(password)) return "Password must include at least one letter.";
  if (!/\d/.test(password)) return "Password must include at least one number.";
  if (!usernameProblem(username) && password.toLowerCase().includes(username.trim().toLowerCase())) {
    return "Password can't contain your username.";
  }
  return "";
}

function confirmProblem() {
  const confirm = $("auth-confirm").value;
  if (!confirm) return "Confirm your password.";
  if (confirm !== $("auth-password").value) return "Passwords don't match.";
  return "";
}

/** Show a message under a field. kind: "error" | "ok" | "hint" */
function setFieldMessage(field, text, kind = "hint") {
  const msg = $(`${field}-msg`);
  msg.textContent = text;
  msg.className = `field-msg${kind === "error" ? " is-error" : kind === "ok" ? " is-ok" : ""}`;
  $(FIELD_INPUTS[field]).setAttribute("aria-invalid", String(kind === "error"));
}

function showAuthAlert(text) {
  $("auth-alert").textContent = text;
  $("auth-alert").hidden = !text;
}

/* ---------- Username ---------- */
function onUsernameInput() {
  if (authMode === "login") {           // sign in: just clear an old error while typing
    if ($("auth-username").getAttribute("aria-invalid") === "true") setFieldMessage("username", "");
    return;
  }
  clearTimeout(availabilityTimer);
  availability = null;
  const problem = usernameProblem($("auth-username").value);
  if (problem) {
    const showError = touched.has("username") && $("auth-username").value.trim();
    setFieldMessage("username", showError ? problem : USERNAME_HINT, showError ? "error" : "hint");
  } else {
    setFieldMessage("username", "Checking if it’s available…");
    availabilityTimer = setTimeout(checkAvailability, 350);
  }
  updatePasswordRules();                // "doesn't contain your username" depends on it
}

async function checkAvailability() {
  const username = cleanUsername();
  const ticket = ++availabilityCheck;
  try {
    const result = await api(`/api/auth/check-username?username=${encodeURIComponent(username)}`);
    if (ticket !== availabilityCheck || result.username !== cleanUsername() || authMode !== "register") return;
    availability = result;
    setFieldMessage("username", result.message, result.available ? "ok" : "error");
  } catch {
    if (ticket === availabilityCheck) setFieldMessage("username", USERNAME_HINT); // server checks on submit
  }
}

function onUsernameBlur() {
  touched.add("username");
  if (authMode !== "register" || !$("auth-username").value.trim()) return;
  const problem = usernameProblem($("auth-username").value);
  if (problem) setFieldMessage("username", problem, "error");
}

/* ---------- Passwords ---------- */
function updatePasswordRules() {
  if (authMode !== "register") return;
  const rules = passwordRules($("auth-password").value, $("auth-username").value);
  for (const item of $("password-rules").children) item.classList.toggle("met", rules[item.dataset.rule]);
}

function onPasswordInput() {
  if (authMode === "login") {
    if ($("auth-password").getAttribute("aria-invalid") === "true") setFieldMessage("password", "");
    return;
  }
  updatePasswordRules();
  if (touched.has("password")) {
    const problem = passwordProblem($("auth-password").value, $("auth-username").value);
    setFieldMessage("password", problem, problem ? "error" : "hint");
    $("password-rules").classList.toggle("show-missing", Boolean(problem));
  }
  if (touched.has("confirm") && $("auth-confirm").value) onConfirmInput();
}

function onPasswordBlur() {
  if (authMode !== "register" || !$("auth-password").value) return;
  touched.add("password");
  onPasswordInput();
}

function onConfirmInput() {
  if (!touched.has("confirm")) {
    if ($("auth-confirm").value === $("auth-password").value && $("auth-confirm").value) {
      setFieldMessage("confirm", "Passwords match.", "ok");
    } else {
      setFieldMessage("confirm", "");
    }
    return;
  }
  const problem = confirmProblem();
  setFieldMessage("confirm", problem || "Passwords match.", problem ? "error" : "ok");
}

function onConfirmBlur() {
  if (!$("auth-confirm").value) return;
  touched.add("confirm");
  onConfirmInput();
}

function updateCapsLock(event) {
  if (event.getModifierState) $("caps-msg").hidden = !event.getModifierState("CapsLock");
}

function toggleReveal(button) {
  const input = $(button.dataset.target);
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  button.textContent = show ? "Hide" : "Show";
  button.setAttribute("aria-pressed", String(show));
  button.setAttribute("aria-label", show ? "Hide password" : "Show password");
  input.focus();
}

/* ---------- Whole form ---------- */
/** Check every field, show all problems, and return the first field to fix (or ""). */
function validateAuthForm() {
  const problems = {};
  if (authMode === "login") {
    if (!$("auth-username").value.trim()) problems.username = "Enter your username.";
    if (!$("auth-password").value) problems.password = "Enter your password.";
  } else {
    ["username", "password", "confirm"].forEach((field) => touched.add(field));
    problems.username = usernameProblem($("auth-username").value);
    if (!problems.username && availability && availability.username === cleanUsername() && !availability.available) {
      problems.username = availability.message;
    }
    problems.password = passwordProblem($("auth-password").value, $("auth-username").value);
    problems.confirm = confirmProblem();
    updatePasswordRules();
    $("password-rules").classList.toggle("show-missing", Boolean(problems.password));
  }
  let first = "";
  for (const field of ["username", "password", "confirm"]) {
    if (problems[field]) {
      setFieldMessage(field, problems[field], "error");
      first ||= field;
    }
  }
  return first;
}

async function submitAuth(event) {
  event.preventDefault();
  showAuthAlert("");
  const firstProblem = validateAuthForm();
  if (firstProblem) { $(FIELD_INPUTS[firstProblem]).focus(); return; }

  const submit = $("auth-submit");
  submit.disabled = true;
  submit.textContent = AUTH_TEXT[authMode].busy;
  try {
    const data = await api(`/api/auth/${authMode}`, {
      method: "POST", body: { username: $("auth-username").value.trim(), password: $("auth-password").value },
    });
    state.token = data.access_token;
    localStorage.setItem("relay_token", state.token);
    resetAuthForm();
    await startApp(data.user);
  } catch (error) {
    showServerErrors(error);
  } finally {
    submit.disabled = false;
    submit.textContent = AUTH_TEXT[authMode].submit;
  }
}

/** Put each server error under the field it belongs to. */
function showServerErrors(error) {
  const fields = Object.entries(error.fields || {}).filter(([field]) => FIELD_INPUTS[field]);
  if (!fields.length) { showAuthAlert(error.message); return; }
  for (const [field, message] of fields) setFieldMessage(field, message, "error");

  const [firstField] = fields[0];
  const input = $(FIELD_INPUTS[firstField]);
  input.focus();
  if (firstField === "password") input.select(); // ready to retype

  // Signing in with a name that doesn't exist: offer to create it
  if (authMode === "login" && error.fields.username?.startsWith("No account found")) {
    const typed = $("auth-username").value;
    $("username-msg").append(el("button", {
      type: "button", className: "link-btn inline-action",
      onclick: () => { setAuthMode("register"); $("auth-username").value = typed; onUsernameInput(); },
    }, "Create an account with this username"));
  }
}

/** Empty every field, message and checklist (used on sign-out and when switching forms). */
function resetAuthForm() {
  $("auth-form").reset();
  clearTimeout(availabilityTimer);
  availabilityCheck += 1;
  availability = null;
  touched.clear();
  for (const field of Object.keys(FIELD_INPUTS)) setFieldMessage(field, "");
  for (const item of $("password-rules").children) item.classList.remove("met");
  $("password-rules").classList.remove("show-missing");
  $("caps-msg").hidden = true;
  document.querySelectorAll(".reveal").forEach((button) => {
    $(button.dataset.target).type = "password";
    button.textContent = "Show";
    button.setAttribute("aria-pressed", "false");
    button.setAttribute("aria-label", "Show password");
  });
  showAuthAlert("");
}

function setAuthMode(mode) {
  authMode = mode;
  resetAuthForm();
  const register = mode === "register";
  const text = AUTH_TEXT[mode];
  $("auth-title").textContent = text.title;
  $("auth-subtitle").textContent = text.subtitle;
  $("auth-submit").textContent = text.submit;
  $("switch-text").textContent = text.switchText;
  $("auth-switch").textContent = text.switchButton;
  $("auth-password").autocomplete = register ? "new-password" : "current-password";
  $("auth-password").maxLength = register ? 32 : 72;
  $("password-rules").hidden = !register;
  $("confirm-field").hidden = !register;
  if (register) setFieldMessage("username", USERNAME_HINT);
  $("auth-username").focus();
}

function showAuth(message = "") {
  $("app-view").hidden = true;
  $("auth-view").hidden = false;
  document.title = "Relay";
  setAuthMode("login");  // always starts empty
  showAuthAlert(message);
}

function signOut(message = "") {
  stopRecording(true);
  clearStaged();
  clearComposeContext();
  closeMessageMenu();
  state.token = null;
  localStorage.removeItem("relay_token");
  const socket = state.socket;
  state.socket = null;
  if (socket) socket.close();
  clearTimeout(state.reconnectTimer);
  clearInterval(state.pingTimer);
  state.rooms.clear();
  state.messages.clear();
  state.activeRoomId = null;
  state.everConnected = false;
  $("connection-banner").hidden = true;
  showAuth(message);
}

async function startApp(me) {
  state.me = me || (await api("/api/auth/me"));
  fillAvatar($("me-avatar"), state.me.username);
  $("me-name").textContent = state.me.username;
  $("auth-view").hidden = true;
  $("app-view").hidden = false;
  showEmptyState();
  api("/api/config").then((config) => (state.config = config)).catch(() => {});
  renderSoundToggle();
  renderNotifyBanner();
  await loadRooms();
  connectSocket();
}

/* ---------------------------------------------------------------------------
   5. Rendering
   --------------------------------------------------------------------------- */
async function loadRooms() {
  const rooms = await api("/api/rooms");
  state.rooms = new Map(rooms.map((room) => [room.id, room]));
  renderRoomList();
}

function sortedRooms() {
  const activity = (room) => room.last_message?.created_at || room.created_at;
  return [...state.rooms.values()].sort((a, b) => activity(b).localeCompare(activity(a)));
}

function typingNames(roomId) {
  const people = state.typing.get(roomId);
  return people ? [...people.values()].map((p) => p.username) : [];
}

function typingText(names, isDirect) {
  if (!names.length) return "";
  if (isDirect) return `${names[0]} is typing…`;
  if (names.length === 1) return `${names[0]} is typing…`;
  if (names.length === 2) return `${names[0]} and ${names[1]} are typing…`;
  return "Several people are typing…";
}

function renderRoomList() {
  const list = $("room-list");
  list.replaceChildren();
  let totalUnread = 0;

  for (const room of sortedRooms()) {
    totalUnread += room.unread;
    const avatar = el("span", { className: "avatar" });
    const other = room.is_direct ? otherMember(room) : null;
    fillAvatar(avatar, roomTitle(room), { group: !room.is_direct, userId: other ? other.id : null });

    const typers = typingNames(room.id);
    let preview = "No messages yet";
    if (room.last_message) {
      const mine = room.last_message.sender.id === state.me.id;
      const who = room.last_message.deleted || mine ? "" : room.is_direct ? "" : `${room.last_message.sender.username}: `;
      preview = who + previewText(room.last_message);
    }
    const lastMine = room.last_message && room.last_message.sender.id === state.me.id && !room.last_message.deleted;
    const previewNode = typers.length
      ? el("span", { className: "room-preview is-typing" }, typingText(typers, room.is_direct))
      : el("span", { className: "room-preview" }, lastMine ? ticksIcon(tickStatus(room.last_message, room)) : null, preview);

    const button = el(
      "button",
      {
        type: "button",
        className: `room${room.unread ? " has-unread" : ""}`,
        "aria-current": String(room.id === state.activeRoomId),
        onclick: () => openRoom(room.id),
      },
      avatar,
      el("span", { className: "room-name" }, roomTitle(room)),
      el("span", { className: "room-time" }, room.last_message ? shortWhen(room.last_message.created_at) : ""),
      previewNode,
      room.unread ? el("span", { className: "badge", "aria-label": `${room.unread} unread` }, room.unread) : null,
    );
    list.append(button);
  }

  if (!state.rooms.size) list.append(el("p", { className: "pick-empty" }, "No conversations yet."));
  document.title = totalUnread ? `(${totalUnread}) Relay` : "Relay";
}

function showEmptyState() {
  $("chat-view").hidden = true;
  $("empty-state").hidden = false;
  $("app-view").classList.remove("show-chat");
}

function renderChatHeader() {
  const room = state.rooms.get(state.activeRoomId);
  if (!room) return;
  const other = room.is_direct ? otherMember(room) : null;
  fillAvatar($("chat-avatar"), roomTitle(room), { group: !room.is_direct, userId: other ? other.id : null });
  $("chat-name").textContent = roomTitle(room);
  const subtitle = $("chat-subtitle");
  subtitle.title = "";
  const typers = typingNames(room.id);
  subtitle.classList.toggle("is-typing", typers.length > 0);
  if (typers.length) {
    subtitle.textContent = room.is_direct ? "typing…" : typingText(typers, false);
  } else if (room.is_direct) {
    subtitle.textContent = state.online.has(other.id) ? "Online" : lastSeenText(other.last_seen);
  } else {
    // Like WhatsApp: member names, with "You" last
    const names = room.members
      .filter((m) => m.id !== state.me.id)
      .map((m) => m.username)
      .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
    names.push("You");
    subtitle.textContent = names.join(", ");
    subtitle.title = names.join(", "); // full list on hover when it's cut off
  }
  $("leave-group").hidden = room.is_direct;
}

async function openRoom(roomId, { keepScroll = false } = {}) {
  if (roomId !== state.activeRoomId) {
    clearStaged();
    stopRecording(true);
    if (state.replyTo || state.editing) clearComposeContext();
  }
  state.activeRoomId = roomId;
  $("empty-state").hidden = true;
  $("chat-view").hidden = false;
  $("app-view").classList.add("show-chat");
  renderChatHeader();
  renderRoomList();
  renderTyping();

  if (!state.messages.get(roomId)?.loaded) {
    $("message-list").replaceChildren(el("p", { className: "day-divider" }, "Loading messages…"));
    try {
      const page = await api(`/api/rooms/${roomId}/messages`);
      state.messages.set(roomId, { list: page.messages, hasMore: page.has_more, loaded: true });
    } catch (error) {
      toast(error.message);
      return;
    }
  }
  if (state.activeRoomId !== roomId) return; // user switched rooms while loading
  renderMessages({ scrollToEnd: !keepScroll });
  markActiveRoomRead();
  $("message-input").focus();
}

async function loadOlder() {
  const roomId = state.activeRoomId;
  const cache = state.messages.get(roomId);
  if (!cache || !cache.list.length) return;
  const box = $("messages");
  const previousHeight = box.scrollHeight;
  try {
    const page = await api(`/api/rooms/${roomId}/messages?before_id=${cache.list[0].id}`);
    cache.list = [...page.messages, ...cache.list];
    cache.hasMore = page.has_more;
  } catch (error) {
    toast(error.message);
    return;
  }
  renderMessages({ scrollToEnd: false });
  box.scrollTop = box.scrollHeight - previousHeight; // stay where you were
}

/* Messages are rendered incrementally: new messages are appended and confirmed
   messages are swapped in place. Redrawing everything would restart any video
   or voice note that is playing. */

function messageKey(message) {
  return message.id ? `m${message.id}` : message.client_id;
}

function isCompact(message, previous) {
  return previous && previous.sender.id === message.sender.id &&
    formatDay(previous.created_at) === formatDay(message.created_at) &&
    new Date(message.created_at) - new Date(previous.created_at) < 5 * 60 * 1000;
}

function attachmentNode(message) {
  const a = message.attachment;
  if (message.localFile) { // still uploading: show a local preview
    const { file, previewUrl } = message.localFile;
    if (previewUrl) return el("span", { className: "att-image" }, el("img", { src: previewUrl, alt: "" }));
    return el("span", { className: "att-file" }, el("span", { className: "att-file-icon" }, fileIcon(file.name, file.type)),
      el("span", {}, el("span", { className: "att-file-name" }, file.name),
        el("span", { className: "att-file-meta" }, formatSize(file.size))));
  }
  if (!a) return null;
  const url = fileUrl(a);
  if (a.kind === "image") {
    return el("button", { type: "button", className: "att-image", "aria-label": `Open photo ${a.name}`,
      onclick: () => openLightbox(url, a.name) },
      el("img", { src: url, alt: a.name, loading: "lazy" }));
  }
  if (a.kind === "video") return el("video", { className: "att-video", src: url, controls: true, preload: "metadata" });
  if (a.kind === "audio") return el("audio", { className: "att-audio", src: url, controls: true, preload: "metadata" });
  return el("a", { className: "att-file", href: url, download: a.name },
    el("span", { className: "att-file-icon" }, fileIcon(a.name, a.content_type)),
    el("span", {}, el("span", { className: "att-file-name" }, a.name),
      el("span", { className: "att-file-meta" }, `${formatSize(a.size)}, tap to download`)));
}

function messageNode(message, previous, room) {
  const mine = message.sender.id === state.me.id;
  const compact = isCompact(message, previous);
  const classes = ["msg"];
  if (mine) classes.push("mine");
  if (compact) classes.push("compact");
  if (message.pending) classes.push("pending");
  if (message.failed) classes.push("failed");

  // Sender name above the bubble (group chats, other people only)
  const name = compact || mine || room.is_direct ? null
    : el("span", { className: "msg-meta" }, el("strong", {}, message.sender.username));

  // Bottom-right of the bubble: "edited", time and ticks
  const meta = el("span", { className: "bubble-meta" },
    message.edited && !message.deleted ? "edited " : null,
    formatTime(message.created_at),
    mine && !message.deleted && !message.failed ? ticksIcon(tickStatus(message, room)) : null);

  let body;
  if (message.deleted) {
    body = [el("span", { className: "deleted-text" }, mine ? "You deleted this message" : "This message was deleted")];
  } else {
    const media = attachmentNode(message);
    body = [
      message.reply_to ? quoteNode(message.reply_to) : null,
      media,
      message.content ? el("span", { className: media ? "text caption" : "text" }, message.content) : null,
    ];
  }
  const hasMedia = !message.deleted && (message.attachment || message.localFile);
  const bubble = el("div", { className: `bubble${hasMedia ? " has-media" : ""}` }, ...body, meta);
  if (message.localFile && message.pending && !message.failed) {
    bubble.append(el("div", { className: "upload-progress", "aria-hidden": "true" },
      el("span", { style: `width:${message.progress || 0}%` })));
  }

  let status = null;
  if (message.failed) status = el("span", { className: "msg-status" }, `Not sent: ${message.failed}`);
  else if (message.localFile && message.pending) status = el("span", { className: "msg-status" }, `Uploading… ${message.progress || 0}%`);

  const menuButton = message.id ? el("button", {
    type: "button", className: "msg-menu-btn", "aria-label": "Message options", "aria-haspopup": "menu",
    "aria-expanded": "false", onclick: (event) => openMessageMenu(message, event.currentTarget),
  }) : null;
  if (menuButton) menuButton.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  const node = el("div", { className: classes.join(" "), dataset: { key: messageKey(message) } }, name, bubble, status, menuButton);
  if (message.id) {
    node.addEventListener("contextmenu", (event) => { // right-click / long-press
      event.preventDefault();
      openMessageMenu(message, menuButton);
    });
  }
  return node;
}

/** Full redraw: used when opening a conversation or loading older messages. */
function renderMessages({ scrollToEnd }) {
  const room = state.rooms.get(state.activeRoomId);
  const cache = state.messages.get(state.activeRoomId);
  if (!room || !cache) return;

  const box = $("messages");
  const list = $("message-list");
  list.replaceChildren();
  $("load-older").hidden = !cache.hasMore;

  if (!cache.list.length) {
    list.append(el("p", { className: "day-divider empty-note" },
      room.is_direct ? `This is the start of your chat with ${roomTitle(room)}.` : "No messages yet. Say hello!"));
  }
  let previous = null;
  for (const message of cache.list) {
    if (!previous || formatDay(previous.created_at) !== formatDay(message.created_at)) {
      list.append(el("p", { className: "day-divider" }, formatDay(message.created_at)));
    }
    list.append(messageNode(message, previous, room));
    previous = message;
  }
  if (scrollToEnd) box.scrollTop = box.scrollHeight;
}

/** Add one new message at the bottom of the open conversation. */
function appendMessage(message) {
  const room = state.rooms.get(message.room_id);
  const cache = state.messages.get(message.room_id);
  if (!room || !cache || message.room_id !== state.activeRoomId) return;

  const box = $("messages");
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 150;
  const list = $("message-list");
  list.querySelector(".empty-note")?.remove();

  const index = cache.list.indexOf(message);
  const previous = index > 0 ? cache.list[index - 1] : null;
  if (!previous || formatDay(previous.created_at) !== formatDay(message.created_at)) {
    list.append(el("p", { className: "day-divider" }, formatDay(message.created_at)));
  }
  list.append(messageNode(message, previous, room));
  if (nearBottom || message.sender.id === state.me.id) box.scrollTop = box.scrollHeight;
}

/** Redraw one message in place (e.g. "Sending…" -> sent, or failed). */
function replaceMessageNode(oldKey, message) {
  if (message.room_id !== state.activeRoomId) return;
  const node = $("message-list").querySelector(`[data-key="${CSS.escape(oldKey)}"]`);
  const cache = state.messages.get(message.room_id);
  if (!node || !cache) return;
  const index = cache.list.indexOf(message);
  node.replaceWith(messageNode(message, index > 0 ? cache.list[index - 1] : null, state.rooms.get(message.room_id)));
}

function updateUploadProgress(message) {
  const node = $("message-list").querySelector(`[data-key="${CSS.escape(message.client_id)}"]`);
  if (!node) return;
  const bar = node.querySelector(".upload-progress span");
  if (bar) bar.style.width = `${message.progress}%`;
  const status = node.querySelector(".msg-status");
  if (status) status.textContent = `Uploading… ${message.progress}%`;
}

function renderTyping() {
  const room = state.rooms.get(state.activeRoomId);
  $("typing").textContent = room ? typingText(typingNames(room.id), room.is_direct) : "";
}

/* ---------------------------------------------------------------------------
   6. Sending, receiving, typing and read events
   --------------------------------------------------------------------------- */
function sendMessage(event) {
  event.preventDefault();
  const input = $("message-input");
  const content = input.value.trim();
  const roomId = state.activeRoomId;
  if (!roomId) return;
  if (state.editing) { saveEdit(content); return; }
  if (!content && !state.staged.length) return;

  const replyToId = state.replyTo ? state.replyTo.id : null;
  const replyTo = state.replyTo ? quoteFrom(state.replyTo) : null;
  clearComposeContext();
  input.value = "";
  autosize();
  stopTyping();

  if (state.staged.length) { // files: the text becomes the caption of the first one
    const files = state.staged;
    clearStaged({ revoke: false });
    files.forEach((item, i) => uploadFile(roomId, item, i === 0 ? content : "", i === 0 ? replyToId : null, i === 0 ? replyTo : null));
    return;
  }

  const clientId = newClientId();
  const pending = {
    id: null, client_id: clientId, room_id: roomId, content, pending: true, reply_to: replyTo,
    sender: state.me, created_at: new Date().toISOString(),
  };
  state.messages.get(roomId)?.list.push(pending); // show it immediately ("optimistic UI")
  if (!send({ type: "message", room_id: roomId, content, client_id: clientId, reply_to_id: replyToId })) {
    pending.failed = "you’re offline";
  }
  appendMessage(pending);
}

function receiveMessage(message, clientId) {
  const room = state.rooms.get(message.room_id);
  if (!room) { loadRooms(); return; } // a room we don't know yet

  const cache = state.messages.get(message.room_id);
  if (cache && !cache.list.some((m) => m.id === message.id)) {
    const pending = clientId ? cache.list.find((m) => m.client_id === clientId && !m.id) : null;
    if (pending) {                       // our own message confirmed: swap it in place
      if (pending.localFile?.previewUrl) URL.revokeObjectURL(pending.localFile.previewUrl);
      cache.list[cache.list.indexOf(pending)] = message;
      replaceMessageNode(clientId, message);
      playSound("sent");
    } else {
      cache.list.push(message);
      appendMessage(message);
    }
  }

  const alreadyKnown = room.last_message && room.last_message.id >= message.id;
  if (!alreadyKnown) room.last_message = message;
  clearTypingUser(message.room_id, message.sender.id);
  const isActive = message.room_id === state.activeRoomId;
  const fromOthers = message.sender.id !== state.me.id;
  const looking = isActive && isLooking();
  if (fromOthers && !alreadyKnown) {
    send({ type: "delivered", room_id: message.room_id, message_id: message.id }); // grey ✓✓ for the sender
    if (!looking) room.unread += 1;
    playSound(looking ? "in-chat" : "incoming");
    if (!looking) showDesktopNotification(room, message);
  }

  renderRoomList();
  if (isActive) {
    renderTyping();
    renderChatHeader();
    if (isLooking()) markActiveRoomRead();
  }
}

function markFailed(clientId, reason) {
  for (const cache of state.messages.values()) {
    const message = cache.list.find((m) => m.client_id === clientId);
    if (message) {
      message.failed = reason;
      replaceMessageNode(clientId, message);
      return;
    }
  }
  toast(reason);
}

/** True when the person can actually see the open chat (tab visible and focused). */
function isLooking() {
  return document.visibilityState === "visible" && document.hasFocus();
}

function markActiveRoomRead() {
  if (!isLooking()) return;
  const room = state.rooms.get(state.activeRoomId);
  const cache = state.messages.get(state.activeRoomId);
  if (!room || !cache) return;
  const last = [...cache.list].reverse().find((m) => m.id);
  if (last) send({ type: "read", room_id: room.id, message_id: last.id });
  if (room.unread) {
    room.unread = 0;
    renderRoomList();
  }
}

function receiveTyping(roomId, user, isTyping) {
  if (!state.typing.has(roomId)) state.typing.set(roomId, new Map());
  const people = state.typing.get(roomId);
  clearTimeout(people.get(user.id)?.timer);
  if (isTyping) {
    // Hide it automatically if we never hear "stopped typing"
    people.set(user.id, { username: user.username, timer: setTimeout(() => clearTypingUser(roomId, user.id), 6000) });
  } else {
    people.delete(user.id);
  }
  renderRoomList();
  if (roomId === state.activeRoomId) { renderTyping(); renderChatHeader(); }
}

function clearTypingUser(roomId, userId) {
  const people = state.typing.get(roomId);
  if (!people || !people.has(userId)) return;
  clearTimeout(people.get(userId).timer);
  people.delete(userId);
  renderRoomList();
  if (roomId === state.activeRoomId) { renderTyping(); renderChatHeader(); }
}

function onComposerInput() {
  autosize();
  const roomId = state.activeRoomId;
  if (!roomId) return;
  if (!$("message-input").value.trim()) { stopTyping(); return; }
  const now = Date.now();
  if (!state.isTyping || now - state.lastTypingSent > 3000) { // don't flood the server
    send({ type: "typing", room_id: roomId, is_typing: true });
    state.isTyping = true;
    state.lastTypingSent = now;
  }
  clearTimeout(state.typingStopTimer);
  state.typingStopTimer = setTimeout(stopTyping, 3000);
}

function stopTyping() {
  clearTimeout(state.typingStopTimer);
  if (state.isTyping && state.activeRoomId) {
    send({ type: "typing", room_id: state.activeRoomId, is_typing: false });
  }
  state.isTyping = false;
}

function autosize() {
  const input = $("message-input");
  input.style.height = "auto";
  input.style.height = `${input.scrollHeight + 2}px`;
}

/* ---------------------------------------------------------------------------
   7. Photos, videos, voice notes and files
   --------------------------------------------------------------------------- */
function stageFiles(fileList) {
  for (const file of fileList) {
    const previewUrl = /^image\/(jpeg|png|gif|webp)$/.test(file.type) ? URL.createObjectURL(file) : null;
    state.staged.push({ file, previewUrl });
  }
  renderStaged();
  $("message-input").focus();
}

function renderStaged() {
  const box = $("staged");
  box.replaceChildren();
  box.hidden = !state.staged.length;
  state.staged.forEach((item, index) => {
    const thumb = item.previewUrl
      ? el("img", { src: item.previewUrl, alt: "" })
      : el("span", { className: "chip-icon" }, fileIcon(item.file.name, item.file.type));
    box.append(el("div", { className: "chip" }, thumb,
      el("span", { className: "chip-name" }, item.file.name, el("span", { className: "chip-size" }, formatSize(item.file.size))),
      el("button", { type: "button", "aria-label": `Remove ${item.file.name}`, onclick: () => {
        if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
        state.staged.splice(index, 1);
        renderStaged();
      } }, "×")));
  });
  $("message-input").placeholder = state.staged.length ? "Add a caption (optional)" : "Write a message";
}

function clearStaged({ revoke = true } = {}) {
  if (revoke) state.staged.forEach((item) => item.previewUrl && URL.revokeObjectURL(item.previewUrl));
  state.staged = [];
  renderStaged();
}

/** Upload one file with a progress bar. The server then broadcasts it like a message. */
function uploadFile(roomId, item, caption, replyToId = null, replyTo = null) {
  const clientId = newClientId();
  const pending = {
    id: null, client_id: clientId, room_id: roomId, content: caption, pending: true, progress: 0, reply_to: replyTo,
    sender: state.me, created_at: new Date().toISOString(), localFile: item,
  };
  state.messages.get(roomId)?.list.push(pending);
  appendMessage(pending);

  const form = new FormData();
  form.append("file", item.file, item.file.name);
  form.append("caption", caption);
  form.append("client_id", clientId);
  if (replyToId) form.append("reply_to_id", String(replyToId));

  // XMLHttpRequest (instead of fetch) because it reports upload progress
  const request = new XMLHttpRequest();
  request.open("POST", `/api/rooms/${roomId}/attachments`);
  request.setRequestHeader("Authorization", `Bearer ${state.token}`);
  request.upload.onprogress = (event) => {
    if (!event.lengthComputable) return;
    pending.progress = Math.min(99, Math.round((event.loaded / event.total) * 100));
    updateUploadProgress(pending);
  };
  request.onload = () => {
    if (request.status === 201) {
      receiveMessage(JSON.parse(request.responseText), clientId); // in case the WebSocket was slower
      return;
    }
    let data = null;
    try { data = JSON.parse(request.responseText); } catch { /* not JSON */ }
    if (request.status === 401) signOut("Your session has expired. Sign in again.");
    else markFailed(clientId, errorMessage(data, request.status));
  };
  request.onerror = () => markFailed(clientId, "the upload was interrupted");
  request.send(form);
}

function openLightbox(url, name) {
  $("lightbox-img").src = url;
  $("lightbox-img").alt = name;
  $("lightbox-download").href = url;
  $("lightbox-download").download = name;
  $("lightbox").showModal();
}

/* ---------- Voice messages (MediaRecorder) ---------- */
function canRecord() {
  return Boolean(window.isSecureContext && navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
}

function recordingMimeType() {
  const options = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
  return options.find((type) => MediaRecorder.isTypeSupported(type)) || "";
}

async function startRecording() {
  if (!state.activeRoomId) return;
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    toast("Allow microphone access in your browser to record voice messages.");
    return;
  }
  const mimeType = recordingMimeType();
  const mediaRecorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
  const rec = { mediaRecorder, stream, chunks: [], started: Date.now(), cancelled: false, roomId: state.activeRoomId };
  state.recorder = rec;

  mediaRecorder.ondataavailable = (event) => event.data.size && rec.chunks.push(event.data);
  mediaRecorder.onstop = () => {
    stream.getTracks().forEach((track) => track.stop()); // turn the microphone off
    clearInterval(rec.timer);
    showRecorder(false);
    if (rec.cancelled || !rec.chunks.length) return;
    const type = (mediaRecorder.mimeType || "audio/webm").split(";")[0];
    const extension = type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm";
    const file = new File(rec.chunks, `voice-message.${extension}`, { type });
    uploadFile(rec.roomId, { file, previewUrl: null }, "");
  };

  mediaRecorder.start();
  rec.timer = setInterval(() => {
    const seconds = Math.floor((Date.now() - rec.started) / 1000);
    $("rec-time").textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
    if (seconds >= 300) stopRecording(false); // 5 minute limit
  }, 250);
  $("rec-time").textContent = "0:00";
  showRecorder(true);
}

function stopRecording(cancel) {
  const rec = state.recorder;
  if (!rec) return;
  rec.cancelled = cancel;
  state.recorder = null;
  if (rec.mediaRecorder.state !== "inactive") rec.mediaRecorder.stop();
}

function showRecorder(on) {
  $("recorder").hidden = !on;
  $("composer").hidden = on;
  if (on) $("rec-send").focus();
}

/* ---------- Drag & drop and paste ---------- */
function wireUpFileDrops() {
  const view = $("chat-view");
  let depth = 0;
  view.addEventListener("dragenter", (event) => {
    if (!event.dataTransfer?.types.includes("Files")) return;
    depth += 1;
    $("drop-hint").hidden = false;
  });
  view.addEventListener("dragleave", () => {
    depth = Math.max(0, depth - 1);
    if (!depth) $("drop-hint").hidden = true;
  });
  view.addEventListener("dragover", (event) => event.preventDefault());
  view.addEventListener("drop", (event) => {
    event.preventDefault();
    depth = 0;
    $("drop-hint").hidden = true;
    if (event.dataTransfer?.files.length) stageFiles(event.dataTransfer.files);
  });
  $("message-input").addEventListener("paste", (event) => {
    const files = [...(event.clipboardData?.files || [])];
    if (files.length) {
      event.preventDefault();
      stageFiles(files);
    }
  });
}

/* ---------------------------------------------------------------------------
   8. Ticks, replies, editing and deleting
   --------------------------------------------------------------------------- */

/** ✓ sent, ✓✓ delivered, blue ✓✓ read. In groups, everyone else must have received/read it. */
function tickStatus(message, room) {
  if (message.pending) return "pending";
  const others = (room.receipts || []).filter((r) => r.user_id !== state.me.id);
  if (!others.length) return "sent";
  if (others.every((r) => r.read_id >= message.id)) return "read";
  if (others.every((r) => r.delivered_id >= message.id)) return "delivered";
  return "sent";
}

const TICK_PATHS = {
  pending: '<circle cx="8" cy="8" r="6"/><path d="M8 4.5V8l2.2 1.5"/>',
  sent: '<path d="M4 8.5l3 3 6-7"/>',
  delivered: '<path d="M1.5 8.5l3 3 6-7M8 11.5l1 0.9 6-7"/>',
  read: '<path d="M1.5 8.5l3 3 6-7M8 11.5l1 0.9 6-7"/>',
};
const TICK_LABELS = { pending: "Sending", sent: "Sent", delivered: "Delivered", read: "Read" };

function ticksIcon(status) {
  const span = el("span", { className: "tick-wrap", title: TICK_LABELS[status] });
  span.innerHTML = `<svg class="ticks ${status}" viewBox="0 0 ${status === "pending" ? 16 : 17} 16" role="img" aria-label="${TICK_LABELS[status]}">${TICK_PATHS[status]}</svg>`;
  return span;
}

function applyReceipt(event) {
  const room = state.rooms.get(event.room_id);
  if (!room) return;
  room.receipts = (room.receipts || []).filter((r) => r.user_id !== event.user_id);
  room.receipts.push({ user_id: event.user_id, delivered_id: event.delivered_id, read_id: event.read_id });
  renderRoomList();
  if (room.id === state.activeRoomId) refreshTicks();
}

/** Update ticks on your own messages in place (no redraw). */
function refreshTicks() {
  const room = state.rooms.get(state.activeRoomId);
  const cache = state.messages.get(state.activeRoomId);
  if (!room || !cache) return;
  for (const message of cache.list) {
    if (!message.id || message.deleted || message.sender.id !== state.me.id) continue;
    const old = $("message-list").querySelector(`[data-key="m${message.id}"] .tick-wrap`);
    if (old) old.replaceWith(ticksIcon(tickStatus(message, room)));
  }
}

/** After connecting, tell senders we've received what's in our conversation list. */
function acknowledgeAllRooms() {
  for (const room of state.rooms.values()) {
    const last = room.last_message;
    if (last && last.sender.id !== state.me.id) send({ type: "delivered", room_id: room.id, message_id: last.id });
  }
  markActiveRoomRead();
}

function quoteFrom(message) {
  return {
    id: message.id, sender: message.sender, content: message.content, deleted: !!message.deleted,
    attachment_kind: message.attachment?.kind || null, attachment_name: message.attachment?.name || null,
  };
}

function quoteText(quote) {
  if (quote.deleted) return "This message was deleted";
  return previewText({ ...quote, attachment: quote.attachment_kind ? { kind: quote.attachment_kind, name: quote.attachment_name || "" } : null });
}

function quoteNode(quote) {
  const who = quote.sender.id === state.me.id ? "You" : quote.sender.username;
  return el("button", { type: "button", className: "quote", dataset: { replyId: String(quote.id) },
    onclick: () => jumpToMessage(quote.id) },
  el("strong", {}, who), el("span", {}, quoteText(quote)));
}

function jumpToMessage(messageId) {
  const node = $("message-list").querySelector(`[data-key="m${messageId}"]`);
  if (!node) { toast("That message is further up. Load older messages to see it."); return; }
  node.scrollIntoView({ behavior: "smooth", block: "center" });
  node.classList.remove("flash");
  void node.offsetWidth; // restart the animation
  node.classList.add("flash");
}

/* ---------- Message menu ---------- */
function canEdit(message) {
  return message.sender.id === state.me.id && !message.deleted &&
    Date.now() - new Date(message.created_at) < state.config.edit_window_minutes * 60 * 1000;
}

function canDeleteForEveryone(message) {
  return message.sender.id === state.me.id && !message.deleted &&
    Date.now() - new Date(message.created_at) < state.config.delete_for_everyone_hours * 3600 * 1000;
}

function openMessageMenu(message, button) {
  closeMessageMenu();
  const menu = $("msg-menu");
  state.menuMessage = message;
  menu.querySelector('[data-action="reply"]').hidden = !!message.deleted;
  menu.querySelector('[data-action="copy"]').hidden = message.deleted || !message.content;
  menu.querySelector('[data-action="edit"]').hidden = !canEdit(message);
  menu.hidden = false;

  const box = (button || $("message-list")).getBoundingClientRect();
  const width = menu.offsetWidth;
  const height = menu.offsetHeight;
  const left = Math.min(Math.max(8, box.left), window.innerWidth - width - 8);
  const top = box.bottom + height + 8 > window.innerHeight ? box.top - height - 4 : box.bottom + 4;
  menu.style.left = `${left}px`;
  menu.style.top = `${Math.max(8, top)}px`;
  button?.setAttribute("aria-expanded", "true");
  menu.querySelector("button:not([hidden])")?.focus();
}

function closeMessageMenu() {
  $("msg-menu").hidden = true;
  document.querySelectorAll('.msg-menu-btn[aria-expanded="true"]').forEach((b) => b.setAttribute("aria-expanded", "false"));
}

function onMenuAction(action) {
  const message = state.menuMessage;
  closeMessageMenu();
  if (!message) return;
  if (action === "reply") startReply(message);
  else if (action === "copy") navigator.clipboard?.writeText(message.content).then(() => toast("Copied"));
  else if (action === "edit") startEdit(message);
  else if (action === "delete") askDelete(message);
}

/* ---------- Replying & editing ---------- */
function startReply(message) {
  state.editing = null;
  state.replyTo = message;
  const who = message.sender.id === state.me.id ? "yourself" : message.sender.username;
  showComposeContext(`Replying to ${who}`, previewText(message));
  $("message-input").focus();
}

function startEdit(message) {
  state.replyTo = null;
  state.editing = message;
  clearStaged();
  showComposeContext("Editing message", previewText(message));
  const input = $("message-input");
  input.value = message.content;
  autosize();
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

function showComposeContext(title, preview) {
  $("context-title").textContent = title;
  $("context-preview").textContent = preview;
  $("compose-context").hidden = false;
  $("attach").hidden = !!state.editing;
  $("record").hidden = !!state.editing || !canRecord();
  $("send").textContent = state.editing ? "Save" : "Send";
}

function clearComposeContext() {
  const wasEditing = !!state.editing;
  state.replyTo = null;
  state.editing = null;
  $("compose-context").hidden = true;
  $("attach").hidden = false;
  $("record").hidden = !canRecord();
  $("send").textContent = "Send";
  if (wasEditing) { $("message-input").value = ""; autosize(); }
}

async function saveEdit(content) {
  const message = state.editing;
  if (content === message.content) { clearComposeContext(); return; }
  try {
    const updated = await api(`/api/messages/${message.id}`, { method: "PATCH", body: { content } });
    clearComposeContext();
    applyMessageUpdate(updated);
  } catch (error) {
    toast(error.message);
  }
}

/* ---------- Deleting ---------- */
function askDelete(message) {
  state.menuMessage = message;
  const everyone = canDeleteForEveryone(message);
  $("delete-everyone").hidden = !everyone;
  $("delete-note").textContent = everyone
    ? "Delete for everyone removes it from this chat for all members."
    : message.sender.id === state.me.id && !message.deleted
      ? `It’s been more than ${state.config.delete_for_everyone_hours} hours, so you can only delete it for yourself.`
      : "This removes the message from your chat only.";
  $("dialog-delete").showModal();
}

async function deleteMessage(scope) {
  const message = state.menuMessage;
  $("dialog-delete").close();
  if (!message) return;
  try {
    const result = await api(`/api/messages/${message.id}?scope=${scope}`, { method: "DELETE" });
    if (scope === "everyone") applyMessageUpdate(result);
    else removeHiddenMessage(message.room_id, message.id);
  } catch (error) {
    toast(error.message);
  }
}

/** A message was edited or deleted for everyone: update it (and any replies quoting it). */
function applyMessageUpdate(updated) {
  const cache = state.messages.get(updated.room_id);
  if (cache) {
    const index = cache.list.findIndex((m) => m.id === updated.id);
    if (index >= 0) {
      cache.list[index] = updated;
      replaceMessageNode(`m${updated.id}`, updated);
    }
    for (const message of cache.list) { // refresh quotes of this message
      if (message.reply_to && message.reply_to.id === updated.id) {
        message.reply_to = quoteFrom(updated);
        replaceMessageNode(messageKey(message), message);
      }
    }
  }
  const room = state.rooms.get(updated.room_id);
  if (room && room.last_message && room.last_message.id === updated.id) {
    room.last_message = updated;
    renderRoomList();
  }
  if (state.replyTo && state.replyTo.id === updated.id && updated.deleted) clearComposeContext();
}

/** "Delete for me": remove it from this browser only. */
function removeHiddenMessage(roomId, messageId) {
  const cache = state.messages.get(roomId);
  if (cache) {
    cache.list = cache.list.filter((m) => m.id !== messageId);
    if (roomId === state.activeRoomId) renderMessages({ scrollToEnd: false });
  }
  const room = state.rooms.get(roomId);
  if (room && room.last_message && room.last_message.id === messageId) loadRooms(); // new preview
}

/* ---------------------------------------------------------------------------
   9. Sounds and notifications
   Sounds are generated with the Web Audio API, so no sound files are needed.
   --------------------------------------------------------------------------- */
let audioContext = null;

function getAudio() {
  if (!audioContext) audioContext = new (window.AudioContext || window.webkitAudioContext)();
  if (audioContext.state === "suspended") audioContext.resume();
  return audioContext;
}

function tone(frequency, start, duration, { volume = 0.12, endFrequency = null, type = "sine" } = {}) {
  const ctx = getAudio();
  const t0 = ctx.currentTime + start;
  const oscillator = ctx.createOscillator();
  const gain = ctx.createGain();
  oscillator.type = type;
  oscillator.frequency.setValueAtTime(frequency, t0);
  if (endFrequency) oscillator.frequency.exponentialRampToValueAtTime(endFrequency, t0 + duration);
  gain.gain.setValueAtTime(0.0001, t0);
  gain.gain.exponentialRampToValueAtTime(volume, t0 + 0.012);
  gain.gain.exponentialRampToValueAtTime(0.0001, t0 + duration);
  oscillator.connect(gain).connect(ctx.destination);
  oscillator.start(t0);
  oscillator.stop(t0 + duration + 0.03);
}

function playSound(kind) {
  if (!state.soundOn) return;
  try {
    if (kind === "sent") {                 // quick upward "pop"
      tone(480, 0, 0.11, { volume: 0.07, endFrequency: 900 });
    } else if (kind === "incoming") {      // two-note chime
      tone(784, 0, 0.16, { volume: 0.13 });
      tone(1175, 0.12, 0.26, { volume: 0.11 });
    } else if (kind === "in-chat") {       // soft blip for the chat you're looking at
      tone(660, 0, 0.08, { volume: 0.05 });
    }
  } catch { /* audio not available */ }
}

function renderSoundToggle() {
  const button = $("sound-toggle");
  button.setAttribute("aria-pressed", String(state.soundOn));
  button.title = state.soundOn ? "Sounds on" : "Sounds off";
}

function toggleSound() {
  state.soundOn = !state.soundOn;
  localStorage.setItem("relay_sound", state.soundOn ? "on" : "off");
  renderSoundToggle();
  if (state.soundOn) playSound("incoming"); // preview
}

/* ---------- Desktop notifications ---------- */
function notificationsSupported() {
  return "Notification" in window && window.isSecureContext;
}

function renderNotifyBanner() {
  $("notify-banner").hidden = !(notificationsSupported() && Notification.permission === "default" &&
    localStorage.getItem("relay_notify_dismissed") !== "yes");
}

async function enableNotifications() {
  try {
    const result = await Notification.requestPermission();
    if (result === "granted") toast("Notifications are on");
    else if (result === "denied") toast("Notifications are blocked. You can allow them in your browser’s site settings.");
  } catch { /* old browsers */ }
  renderNotifyBanner();
}

function showDesktopNotification(room, message) {
  if (!notificationsSupported() || Notification.permission !== "granted" || document.visibilityState === "visible") return;
  const title = room.is_direct ? message.sender.username : `${message.sender.username} in ${room.name}`;
  try {
    const notification = new Notification(title, { body: previewText(message), tag: `room-${room.id}` });
    notification.onclick = () => {
      window.focus();
      openRoom(room.id);
      notification.close();
    };
  } catch { /* some mobile browsers only allow notifications from a service worker */ }
}

/* ---------- Theme: System / Light / Dark ---------- */
function currentTheme() {
  try {
    const saved = localStorage.getItem("relay_theme");
    return saved === "light" || saved === "dark" ? saved : "system";
  } catch {
    return "system";
  }
}

function applyTheme(theme) {
  if (theme === "system") delete document.documentElement.dataset.theme; // follow the device
  else document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("relay_theme", theme); } catch { /* storage blocked */ }
  document.querySelectorAll("[data-theme-choice]").forEach((button) => {
    button.setAttribute("aria-checked", String(button.dataset.themeChoice === theme));
  });
}

/* ---------------------------------------------------------------------------
   10. Dialogs
   --------------------------------------------------------------------------- */
let searchTimer = null;

function openNewMessage() {
  $("people-search").value = "";
  $("dialog-new-message").showModal();
  searchPeople();
}

function searchPeople() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(async () => {
    const results = $("people-results");
    try {
      const people = await api(`/api/users/search?q=${encodeURIComponent($("people-search").value.trim())}`);
      results.replaceChildren();
      if (!people.length) results.append(el("li", { className: "pick-empty" }, "No one found with that name."));
      for (const person of people) {
        const avatar = el("span", { className: "avatar" });
        fillAvatar(avatar, person.username, { userId: person.id });
        avatar.dataset.online = String(person.online || state.online.has(person.id));
        results.append(el("li", {}, avatar,
          el("span", { className: "pick-name" }, person.username),
          el("button", { type: "button", className: "btn-secondary", onclick: () => startDirect(person.username) }, "Message")));
      }
    } catch (error) {
      results.replaceChildren(el("li", { className: "pick-empty" }, error.message));
    }
  }, 200);
}

async function startDirect(username) {
  try {
    const room = await api(`/api/rooms/direct/${encodeURIComponent(username)}`, { method: "POST" });
    state.rooms.set(room.id, room);
    $("dialog-new-message").close();
    await openRoom(room.id);
  } catch (error) {
    toast(error.message);
  }
}

async function createGroup(event) {
  event.preventDefault();
  $("group-error").textContent = "";
  try {
    const room = await api("/api/rooms", { method: "POST", body: { name: $("group-name").value } });
    state.rooms.set(room.id, room);
    $("dialog-new-group").close();
    toast(`Created ${room.name}`);
    await openRoom(room.id);
  } catch (error) {
    $("group-error").textContent = error.message;
  }
}

async function openBrowse() {
  const results = $("browse-results");
  results.replaceChildren(el("li", { className: "pick-empty" }, "Loading groups…"));
  $("dialog-browse").showModal();
  try {
    const groups = await api("/api/rooms/discover");
    results.replaceChildren();
    if (!groups.length) results.append(el("li", { className: "pick-empty" }, "You’re in every group. Create a new one!"));
    for (const group of groups) {
      const avatar = el("span", { className: "avatar" });
      fillAvatar(avatar, group.name, { group: true });
      results.append(el("li", {}, avatar,
        el("span", { className: "pick-name" }, group.name, el("br"),
          el("span", { className: "pick-sub" }, `${group.member_count} member${group.member_count === 1 ? "" : "s"}`)),
        el("button", { type: "button", className: "btn-secondary", onclick: () => joinGroup(group.id) }, "Join")));
    }
  } catch (error) {
    results.replaceChildren(el("li", { className: "pick-empty" }, error.message));
  }
}

async function joinGroup(roomId) {
  try {
    const room = await api(`/api/rooms/${roomId}/join`, { method: "POST" });
    state.rooms.set(room.id, room);
    $("dialog-browse").close();
    toast(`Joined ${room.name}`);
    await openRoom(room.id);
  } catch (error) {
    toast(error.message);
  }
}

async function leaveGroup() {
  const room = state.rooms.get(state.activeRoomId);
  if (!room || !confirm(`Leave ${room.name}? You can join again from Browse groups.`)) return;
  try {
    await api(`/api/rooms/${room.id}/leave`, { method: "POST" });
    state.rooms.delete(room.id);
    state.messages.delete(room.id);
    state.activeRoomId = null;
    renderRoomList();
    showEmptyState();
    toast(`Left ${room.name}`);
  } catch (error) {
    toast(error.message);
  }
}

/* ---------------------------------------------------------------------------
   11. Start-up
   --------------------------------------------------------------------------- */
function wireUpEvents() {
  document.querySelectorAll("[data-theme-choice]").forEach((button) => {
    button.onclick = () => applyTheme(button.dataset.themeChoice);
  });
  applyTheme(currentTheme());

  $("auth-form").onsubmit = submitAuth;
  $("auth-switch").onclick = () => setAuthMode(authMode === "login" ? "register" : "login");
  $("auth-username").addEventListener("input", onUsernameInput);
  $("auth-username").addEventListener("blur", onUsernameBlur);
  $("auth-password").addEventListener("input", onPasswordInput);
  $("auth-password").addEventListener("blur", onPasswordBlur);
  $("auth-confirm").addEventListener("input", onConfirmInput);
  $("auth-confirm").addEventListener("blur", onConfirmBlur);
  for (const id of ["auth-password", "auth-confirm"]) {
    $(id).addEventListener("keydown", updateCapsLock);
    $(id).addEventListener("keyup", updateCapsLock);
  }
  document.querySelectorAll(".reveal").forEach((button) => (button.onclick = () => toggleReveal(button)));
  $("sign-out").onclick = () => signOut();

  $("composer").onsubmit = sendMessage;
  $("message-input").addEventListener("input", onComposerInput);
  $("message-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { // Shift+Enter = new line
      event.preventDefault();
      $("composer").requestSubmit();
    }
  });
  $("load-older").onclick = loadOlder;
  $("attach").onclick = () => $("file-input").click();
  $("file-input").onchange = (event) => {
    stageFiles(event.target.files);
    event.target.value = ""; // allow picking the same file again
  };
  $("record").hidden = !canRecord(); // needs https or localhost
  $("record").onclick = startRecording;
  $("rec-cancel").onclick = () => stopRecording(true);
  $("rec-send").onclick = () => stopRecording(false);
  wireUpFileDrops();

  $("context-cancel").onclick = clearComposeContext;
  $("msg-menu").addEventListener("click", (event) => {
    const action = event.target.closest("button")?.dataset.action;
    if (action) onMenuAction(action);
  });
  document.addEventListener("pointerdown", (event) => {
    if (!$("msg-menu").hidden && !event.target.closest("#msg-menu, .msg-menu-btn")) closeMessageMenu();
  });
  $("messages").addEventListener("scroll", closeMessageMenu, { passive: true });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (!$("msg-menu").hidden) closeMessageMenu();
    else if (state.replyTo || state.editing) clearComposeContext();
  });
  $("delete-everyone").onclick = () => deleteMessage("everyone");
  $("delete-me").onclick = () => deleteMessage("me");

  $("sound-toggle").onclick = toggleSound;
  $("notify-enable").onclick = enableNotifications;
  $("notify-dismiss").onclick = () => {
    localStorage.setItem("relay_notify_dismissed", "yes");
    renderNotifyBanner();
  };
  // Browsers only allow sound after the person has interacted with the page
  document.addEventListener("pointerdown", () => state.soundOn && getAudio(), { once: true });
  document.addEventListener("visibilitychange", () => { if (isLooking()) markActiveRoomRead(); });
  $("back").onclick = () => { state.activeRoomId = null; showEmptyState(); renderRoomList(); };
  $("leave-group").onclick = leaveGroup;

  $("open-new-message").onclick = openNewMessage;
  $("people-search").addEventListener("input", searchPeople);
  $("open-new-group").onclick = () => {
    $("group-name").value = "";
    $("group-error").textContent = "";
    $("dialog-new-group").showModal();
  };
  $("new-group-form").onsubmit = createGroup;
  document.querySelectorAll("[data-close]").forEach((b) => (b.onclick = () => b.closest("dialog").close()));
  $("open-browse").onclick = openBrowse;

  // Coming back to the tab counts as reading the open conversation
  window.addEventListener("focus", markActiveRoomRead);
}

async function boot() {
  wireUpEvents();
  if (!state.token) { showAuth(); return; }
  try {
    await startApp();
  } catch {
    if (state.token) signOut(); // token invalid or server unreachable
  }
}

boot();
