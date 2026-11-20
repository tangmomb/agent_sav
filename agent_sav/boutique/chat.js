const panel = document.querySelector("#chat-panel");
const closeButton = document.querySelector("#close-chat");
const heading = document.querySelector("#chat-order");
const itemPicker = document.querySelector("#chat-product");
const form = document.querySelector("#chat-form");
const input = document.querySelector("#chat-input");
const sendButton = document.querySelector("#send-chat");
let log = document.querySelector("#chat-log");
let actions = document.querySelector("#chat-actions");

const conversations = new Map();
const selectedItems = new Map();
const sizes = ["S", "M", "L", "XL"];
let active;
let opener;

export function openChat(order, customer, button) {
  if (opener === button && !panel.hidden) {
    closeChat();
    return;
  }
  opener?.setAttribute("aria-expanded", "false");
  heading.textContent = "#" + order.id + " · " + customer;
  opener = button;
  opener.setAttribute("aria-expanded", "true");
  panel.hidden = false;
  itemPicker.parentElement.hidden = order.items.length === 1;
  itemPicker.replaceChildren(...order.items.map((item) => new Option(
    item.line_number + ". " + [item.name, item.color, item.size].filter(Boolean).join(" · "),
    item.line_number,
  )));
  const orderKey = order.customer_id + "/" + order.id;
  itemPicker.value = selectedItems.get(orderKey) ?? order.items[0].line_number;
  selectItem(order);
  if (order.items.length > 1) itemPicker.focus({preventScroll: true});
  else focusChat(active);
}

function selectItem(order) {
  const item = order.items.find(({line_number}) => line_number === Number(itemPicker.value));
  const orderKey = order.customer_id + "/" + order.id;
  selectedItems.set(orderKey, item.line_number);
  const key = orderKey + "/" + item.line_number;
  const firstVisit = !conversations.has(key);
  if (firstVisit) {
    const chat = {
      order,
      item,
      log: log.cloneNode(false),
      actions: actions.cloneNode(false),
      threadId: newThreadId(order),
      draft: "",
      placeholder: "Écrivez votre message…",
    };
    conversations.set(key, chat);
  }
  active = conversations.get(key);
  log.replaceWith(active.log);
  actions.replaceWith(active.actions);
  log = active.log;
  actions = active.actions;
  input.value = active.draft;
  updateComposer(active);
  log.scrollTop = log.scrollHeight;
  if (firstVisit) send(active, {thread_id: active.threadId, open: true});
}

function newThreadId(order) {
  return order.customer_id + ":" + order.id + ":" + crypto.randomUUID();
}

itemPicker.addEventListener("change", () => selectItem(active.order));

export function closeChat(restoreFocus = true) {
  panel.hidden = true;
  opener?.setAttribute("aria-expanded", "false");
  if (restoreFocus) opener?.focus({preventScroll: true});
  opener = undefined;
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

function showActions(chat, options) {
  chat.actions.replaceChildren();
  for (const [text, run] of options) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = text;
    button.addEventListener("click", run);
    chat.actions.append(button);
  }
  chat.log.scrollTop = chat.log.scrollHeight;
}

function offerRequests(chat) {
  const options = [["Retourner mon article", () => submitMessage(chat, "Je voudrais retourner mon article.")]];
  if (chat.item.sized) {
    options.unshift(["Changer de taille", () => submitMessage(chat, "Je voudrais échanger mon article.")]);
  }
  showActions(chat, options);
}

function showResult(chat, threadId, result) {
  const {item} = chat;
  switch (result.status) {
    case "accord_requis":
      say(chat, result.action === "echange"
        ? "Je peux échanger l’article « " + item.name + " » (taille " + item.size + ") contre une taille " + result.size + ". Vous confirmez ?"
        : "Je peux enregistrer le retour de l’article « " + item.name + " ». Vous confirmez ?");
      showActions(chat, [
        ["Oui, je confirme", () => submitDecision(chat, threadId, true)],
        ["Non, annuler", () => submitDecision(chat, threadId, false)],
      ]);
      return true;
    case "taille_a_preciser":
    case "taille_identique":
      say(chat, result.question);
      showActions(chat, sizes.filter((size) => size !== item.size).map((size) => [
        "Taille " + size, () => submitMessage(chat, "Du " + size),
      ]));
      return false;
    case "termine":
      say(chat, result.action === "echange"
        ? "Votre échange est enregistré et la taille " + result.size + " est réservée. Référence " + result.return_id + "."
        : "Votre retour est enregistré. Référence " + result.return_id + ".");
      break;
    case "annule":
      say(chat, "D’accord, j’ai annulé votre demande.");
      break;
    case "stock_indisponible":
      say(chat, "Cet article est épuisé en taille " + result.size + ". Vous pouvez demander une autre taille ou un retour.");
      break;
    case "article_sans_taille":
      say(chat, "Cet article est proposé en taille unique. Je peux vous aider à le retourner.");
      break;
    case "commande_en_transit":
      say(chat, "Votre commande est en cours de livraison. Vous pourrez demander un retour ou un échange une fois reçue.");
      return false;
    case "commande_validee":
      say(chat, "Votre commande est validée et sera préparée pour l’expédition. Vous pourrez demander un retour ou un échange après la livraison.");
      return false;
    default:
      say(chat, "Pouvez-vous préciser si vous souhaitez retourner votre article ou l’échanger ?");
  }
  offerRequests(chat);
  return false;
}

function updateComposer(chat) {
  if (chat !== active) return;
  input.disabled = sendButton.disabled = Boolean(chat.waiting || chat.confirming || chat.failed);
  input.placeholder = chat.placeholder;
}

function focusChat(chat) {
  if (chat !== active) return;
  const target = input.disabled ? actions.firstElementChild ?? closeButton : input;
  target.focus({preventScroll: true});
}

async function send(chat, payload) {
  if (chat.waiting) return;
  chat.waiting = true;
  chat.placeholder = "Un instant…";
  showActions(chat, []);
  updateComposer(chat);
  const pending = say(chat, "Un instant…");
  try {
    const response = await fetch("/api/sav", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        ...payload,
        line_number: chat.item.line_number,
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error);
    pending.remove();
    chat.failed = false;
    if (payload.open) {
      say(chat, result.greeting);
      offerRequests(chat);
      chat.log.scrollTop = 0;
    } else {
      chat.confirming = showResult(chat, payload.thread_id, result);
      const awaitingReply = ["taille_a_preciser", "taille_identique", "accord_requis"].includes(result.status);
      chat.threadId = awaitingReply ? payload.thread_id : undefined;
    }
    chat.placeholder = chat.confirming ? "Choisissez une réponse ci-dessus" : "Écrivez votre message…";
  } catch {
    pending.textContent = "Le message n’a pas abouti. Vous pouvez réessayer.";
    chat.failed = true;
    chat.placeholder = "En attente de connexion…";
    showActions(chat, [["Réessayer", () => send(chat, payload)]]);
  } finally {
    chat.waiting = false;
    updateComposer(chat);
    if (document.activeElement !== itemPicker &&
        (panel.contains(document.activeElement) || document.activeElement === document.body)) {
      focusChat(chat);
    }
  }
}

function submitMessage(chat, message) {
  if (chat.waiting) return;
  const threadId = chat.threadId ?? newThreadId(chat.order);
  say(chat, message, "client");
  chat.draft = "";
  if (chat === active) input.value = "";
  send(chat, {thread_id: threadId, message_id: crypto.randomUUID(), message});
}

function submitDecision(chat, threadId, approved) {
  if (chat.waiting) return;
  say(chat, approved ? "Oui, je confirme." : "Non, j’annule.", "client");
  send(chat, {thread_id: threadId, approved});
}

input.addEventListener("input", () => { active.draft = input.value; });
form.addEventListener("submit", (event) => {
  event.preventDefault();
  const message = input.value.trim();
  if (active && message) submitMessage(active, message);
});
