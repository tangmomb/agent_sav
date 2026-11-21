const panel = document.querySelector("#chat-panel");
const closeButton = document.querySelector("#close-chat");
const heading = document.querySelector("#chat-order");
const form = document.querySelector("#chat-form");
const input = document.querySelector("#chat-input");
const sendButton = document.querySelector("#send-chat");
let log = document.querySelector("#chat-log");
let actions = document.querySelector("#chat-actions");

const conversations = new Map();
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
  const key = order.customer_id + "/" + order.id;
  const firstVisit = !conversations.has(key);
  if (firstVisit) {
    const chat = {
      order,
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
  else focusChat(active);
}

function newThreadId(order) {
  return order.customer_id + ":" + order.id + ":" + crypto.randomUUID();
}

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
  if (chat.order.status !== "livree") {
    say(chat, "Les retours et échanges sont disponibles après la livraison.");
    return;
  }
  showActions(chat, [
    ["Échanger un article", () => submitMessage(chat, "Je voudrais échanger un article.")],
    ["Retourner un article", () => submitMessage(chat, "Je voudrais retourner un article.")],
  ]);
}

function showResult(chat, threadId, result) {
  switch (result.status) {
    case "article_a_preciser":
      say(chat, result.question);
      showActions(chat, result.articles.map((article) => {
        const label = [article.name, article.color, article.size].filter(Boolean).join(" · ");
        return [label, () => {
          say(chat, label, "client");
          send(chat, {thread_id: threadId, article: article.line_number});
        }];
      }));
      return true;
    case "taille_a_preciser":
    case "taille_identique":
    case "article_sans_taille":
    case "coloris_inconnu": {
      say(chat, result.question);
      const sizeOptions = result.ordered_size ? sizes.filter((size) => size !== result.ordered_size).map((size) => [
        "Taille " + size, () => submitMessage(chat, "Du " + size),
      ]) : [];
      showActions(chat, [...sizeOptions, ...result.coloris.map((color) => [
        color, () => submitMessage(chat, "En coloris " + color),
      ])]);
      return false;
    }
    case "attente_boutique":
    case "attente_client":
    case "refuse_boutique":
    case "termine":
    case "annule": {
      const message = say(chat, result.status === "attente_boutique"
        ? result.message_client
        : "Vous pouvez consulter le suivi dans la boîte mail de démonstration.");
      const mailbox = document.createElement("a");
      mailbox.href = "/sav/mail/" + chat.order.customer_id;
      mailbox.textContent = "Ouvrir ma boîte mail";
      message.append(document.createElement("br"), mailbox);
      break;
    }
    case "stock_indisponible":
      say(chat, result.message_client);
      showActions(chat, [["Retourner cet article", () => submitMessage(chat, "Je voudrais retourner cet article.", result.line_number)]]);
      return false;
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
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error);
    pending.remove();
    chat.failed = false;
    if (payload.open) {
      say(chat, result.greeting);
      if (chat.order.status === "livree") say(chat, "Souhaitez-vous échanger ou retourner un article ?");
      offerRequests(chat);
      chat.log.scrollTop = 0;
    } else {
      chat.confirming = showResult(chat, payload.thread_id, result);
      const awaitingReply = ["demande_a_clarifier", "taille_a_preciser", "taille_identique",
        "coloris_inconnu", "article_a_preciser", "article_sans_taille"].includes(result.status);
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
    if (panel.contains(document.activeElement) || document.activeElement === document.body) {
      focusChat(chat);
    }
  }
}

function submitMessage(chat, message, lineNumber) {
  if (chat.waiting) return;
  const threadId = chat.threadId ?? newThreadId(chat.order);
  say(chat, message, "client");
  chat.draft = "";
  if (chat === active) input.value = "";
  send(chat, {thread_id: threadId, message_id: crypto.randomUUID(), message, line_number: lineNumber});
}

input.addEventListener("input", () => { active.draft = input.value; });
form.addEventListener("submit", (event) => {
  event.preventDefault();
  const message = input.value.trim();
  if (active && message) submitMessage(active, message);
});
