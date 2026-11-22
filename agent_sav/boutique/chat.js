const panel = document.querySelector("#chat-panel");
const launcher = document.querySelector("#open-chat");
const closeButton = document.querySelector("#close-chat");
const heading = document.querySelector("#chat-customer");
const form = document.querySelector("#chat-form");
const input = document.querySelector("#chat-input");
const sendButton = document.querySelector("#send-chat");
let log = document.querySelector("#chat-log");

const conversations = new Map();
let active;

export function openChat(customerId, customerName) {
  heading.textContent = customerName;
  launcher.setAttribute("aria-expanded", "true");
  launcher.hidden = true;
  panel.hidden = false;
  if (!conversations.has(customerId)) {
    const chat = {
      customerId,
      log: log.cloneNode(false),
      draft: "",
      placeholder: "Écrivez votre message…",
    };
    conversations.set(customerId, chat);
  }
  active = conversations.get(customerId);
  log.replaceWith(active.log);
  log = active.log;
  input.value = active.draft;
  updateComposer(active);
  log.scrollTop = log.scrollHeight;
  focusChat(active);
}

export function closeChat(restoreFocus = true) {
  panel.hidden = true;
  launcher.hidden = false;
  launcher.setAttribute("aria-expanded", "false");
  if (active && restoreFocus) launcher.focus({preventScroll: true});
  active = undefined;
}

closeButton.addEventListener("click", () => closeChat());
panel.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeChat();
});

function say(chat, text, speaker = "agent") {
  const message = document.createElement("p");
  message.classList.add("message", speaker);
  message.textContent = text;
  chat.log.append(message);
  chat.log.scrollTop = chat.log.scrollHeight;
  return message;
}

function showResult(chat, result) {
  const text = result.question || result.messages?.at(-1)?.content;
  if (text) say(chat, text);
}

function updateComposer(chat) {
  if (chat !== active) return;
  input.disabled = sendButton.disabled = Boolean(chat.waiting);
  input.placeholder = chat.placeholder;
}

function focusChat(chat) {
  if (chat !== active) return;
  const target = input.disabled ? closeButton : input;
  target.focus({preventScroll: true});
}

async function send(chat, payload) {
  if (chat.waiting) return;
  chat.waiting = true;
  chat.placeholder = "Un instant…";
  updateComposer(chat);
  const pending = say(chat, "Un instant…");
  try {
    const response = await fetch("/api/support", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error);
    pending.remove();
    chat.failedPayload = undefined;
    showResult(chat, result);
    chat.threadId = result.awaiting_message ? payload.thread_id : undefined;
  } catch {
    pending.textContent = "Le message n’a pas abouti. Renvoyez-le pour réessayer.";
    chat.failedPayload = payload;
    chat.draft = payload.message;
    if (chat === active) input.value = chat.draft;
  } finally {
    chat.waiting = false;
    chat.placeholder = "Écrivez votre message…";
    updateComposer(chat);
    if (panel.contains(document.activeElement) || document.activeElement === document.body) {
      focusChat(chat);
    }
  }
}

function submitMessage(chat, message) {
  if (chat.waiting) return;
  const retry = chat.failedPayload?.message === message ? chat.failedPayload : undefined;
  const threadId = chat.threadId ?? chat.customerId + ":" + crypto.randomUUID();
  if (!retry) say(chat, message, "customer");
  chat.draft = "";
  if (chat === active) input.value = "";
  send(chat, retry ?? {thread_id: threadId, message_id: crypto.randomUUID(), message});
}

input.addEventListener("input", () => { active.draft = input.value; });
form.addEventListener("submit", (event) => {
  event.preventDefault();
  const message = input.value.trim();
  if (active && message) submitMessage(active, message);
});
