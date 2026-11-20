const panel = document.querySelector("#chat-panel");
const launcher = document.querySelector("#chat-launcher");
const closeButton = document.querySelector("#close-chat");
const log = document.querySelector("#chat-log");
const actions = document.querySelector("#chat-actions");
const form = document.querySelector("#chat-form");
const input = document.querySelector("#chat-input");
const sendButton = document.querySelector("#send-chat");

let opener = launcher;
let waiting = false;

function setPanel(open) {
  panel.hidden = !open;
  launcher.setAttribute("aria-expanded", String(open));
  if (open) {
    log.scrollTop = log.scrollHeight;
    if (input.disabled) closeButton.focus();
    else input.focus({preventScroll: true});
  } else {
    opener.focus({preventScroll: true});
  }
}

launcher.addEventListener("click", () => {
  if (panel.hidden) opener = launcher;
  setPanel(panel.hidden);
});
document.querySelector("[data-open-chat]").addEventListener("click", (event) => {
  opener = event.currentTarget;
  setPanel(true);
});
closeButton.addEventListener("click", () => setPanel(false));
panel.addEventListener("keydown", (event) => {
  if (event.key === "Escape") setPanel(false);
});

function say(text, speaker = "agent") {
  const message = document.createElement("p");
  message.classList.add("message", speaker);
  message.textContent = text;
  log.append(message);
  log.scrollTop = log.scrollHeight;
  return message;
}

function showActions(options) {
  actions.replaceChildren();
  for (const [text, run] of options) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = text;
    button.addEventListener("click", run);
    actions.append(button);
  }
  log.scrollTop = log.scrollHeight;
}

function offerRequests() {
  showActions([
    ["Échanger mon pull", () => submitMessage("Je voudrais échanger mon pull.")],
    ["Retourner mon pull", () => submitMessage("Je voudrais retourner mon pull.")],
  ]);
}

function showResult(caseId, result) {
  switch (result.status) {
    case "accord_requis": {
      const exchange = result.action === "echange";
      say(exchange
        ? `Je peux échanger votre pull taille S contre un pull taille ${result.size}. Vous confirmez ?`
        : "Je peux enregistrer le retour de votre pull. Vous confirmez ?");
      showActions([
        ["Oui, je confirme", () => submitDecision(caseId, true)],
        ["Non, annuler", () => submitDecision(caseId, false)],
      ]);
      return true;
    }
    case "taille_a_preciser":
    case "taille_identique":
      say(result.status === "taille_identique"
        ? "Votre pull est déjà en taille S. Quelle autre taille souhaitez-vous ?"
        : "Quelle taille souhaitez-vous pour l’échange ?");
      showActions(["M", "L", "XL"].map((size) => [
        `Taille ${size}`, () => submitMessage(`Je voudrais échanger mon pull contre un ${size}.`),
      ]));
      break;
    case "termine":
      say(result.action === "echange"
        ? `Votre échange est enregistré et la taille ${result.size} est réservée. Référence ${result.return_id}.`
        : `Votre retour est enregistré. Référence ${result.return_id}.`);
      offerRequests();
      break;
    case "annule":
      say("D’accord, j’ai annulé votre demande.");
      offerRequests();
      break;
    case "stock_indisponible":
      say(`Le pull n’est plus disponible en taille ${result.size}. Vous pouvez demander une autre taille ou un retour.`);
      offerRequests();
      break;
    default:
      say("Pouvez-vous préciser si vous souhaitez retourner votre pull ou l’échanger ?");
      offerRequests();
  }
  return false;
}

function setComposer(enabled, placeholder) {
  input.disabled = !enabled;
  sendButton.disabled = !enabled;
  input.placeholder = placeholder;
}

async function send(payload) {
  if (waiting) return;
  waiting = true;
  showActions([]);
  setComposer(false, "Un instant…");
  const pending = say("Un instant…");
  try {
    const response = await fetch("/api/sav", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error);
    pending.remove();
    const confirming = showResult(payload.case_id, result);
    setComposer(!confirming, confirming ? "Choisissez une réponse ci-dessus" : "Écrivez votre message…");
    if (!panel.hidden) (confirming ? actions.firstElementChild : input).focus({preventScroll: true});
  } catch (error) {
    pending.textContent = "Le message n’a pas abouti. Vous pouvez réessayer.";
    showActions([["Réessayer", () => send(payload)]]);
    setComposer(false, "En attente de connexion…");
    if (!panel.hidden) actions.firstElementChild.focus({preventScroll: true});
  } finally {
    waiting = false;
  }
}

function submitMessage(message) {
  if (waiting) return;
  const caseId = crypto.randomUUID();
  say(message, "client");
  input.value = "";
  send({case_id: caseId, message});
}

function submitDecision(caseId, approved) {
  if (waiting) return;
  say(approved ? "Oui, je confirme." : "Non, j’annule.", "client");
  send({case_id: caseId, approved});
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const message = input.value.trim();
  if (message) submitMessage(message);
});

offerRequests();
