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
    welcome(chat, customerName.split(" ")[0]);
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

function welcome(chat, firstName) {
  say(chat, `Bonjour ${firstName}, comment pouvons-nous vous aider ?`);
  offer(chat, ["Échanger un article", "Retourner un article", "Suivre ma commande"]);
}

function offer(chat, labels) {
  chat.suggestions?.remove();
  chat.suggestions = document.createElement("div");
  chat.suggestions.className = "suggestions";
  chat.suggestions.append(...labels.map((label) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.addEventListener("click", () => submitMessage(chat, label));
    return button;
  }));
  chat.log.append(chat.suggestions);
  chat.log.scrollTop = chat.log.scrollHeight;
}

// What the agent did before answering, for whoever looks at the demo.
function showSteps(chat, steps) {
  if (!steps.length) return;
  const details = document.createElement("details");
  details.className = "steps";
  const summary = document.createElement("summary");
  summary.textContent = `Voir ce qu’a fait l’agent (${steps.length})`;
  const list = document.createElement("ol");
  for (const step of steps) {
    const entry = document.createElement("li");
    const name = document.createElement("code");
    name.textContent = step.kind === "tool" ? step.title : "réponse retenue";
    const text = step.kind === "tool" ? step.text : `${step.title} : « ${step.text} »`;
    entry.append(name, " ", text.length > 160 ? text.slice(0, 160) + "…" : text);
    list.append(entry);
  }
  details.append(summary, list);
  chat.log.append(details);
}

function showResult(chat, result) {
  showSteps(chat, result.steps);
  for (const text of result.messages) say(chat, text);
  if (result.awaiting_confirmation) offer(chat, ["Oui, je confirme", "Non, merci"]);
}

async function post(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok) {
    throw Object.assign(new Error(result.error), [401, 429].includes(response.status) ? {shown: result.error} : {});
  }
  return result;
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
    chat.id ??= (await post("/api/chats", {customer_id: chat.customerId})).chat_id;
    const result = await post("/api/support", {chat_id: chat.id, ...payload});
    pending.remove();
    chat.failedPayload = undefined;
    showResult(chat, result);
  } catch (error) {
    pending.textContent = error.shown ?? "Le message n’a pas abouti. Renvoyez-le pour réessayer.";
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
  chat.suggestions.remove();
  if (!retry) say(chat, message, "customer");
  chat.draft = "";
  if (chat === active) input.value = "";
  send(chat, retry ?? {message_id: crypto.randomUUID(), message});
}

input.addEventListener("input", () => { active.draft = input.value; });
form.addEventListener("submit", (event) => {
  event.preventDefault();
  const message = input.value.trim();
  if (active && message) submitMessage(active, message);
});
