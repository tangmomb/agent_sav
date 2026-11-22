import {closeChat, openChat} from "/chat.js";

const select = document.querySelector("#customer");
const orders = document.querySelector("#orders");
const status = document.querySelector("#orders-status");
const retry = document.querySelector("#retry-orders");
const mailbox = document.querySelector("#support-mailbox");
const helpButton = document.querySelector("#open-chat");
const template = document.querySelector("#order-card");
const itemTemplate = document.querySelector("#order-item");
const money = new Intl.NumberFormat("fr-FR", {style: "currency", currency: "EUR"});
const date = new Intl.DateTimeFormat("fr-FR", {dateStyle: "long", timeZone: "UTC"});
const statusLabels = {
  confirmed: "Commande validée",
  in_transit: "En livraison",
  delivered: "Livrée",
};
let currentRequest;

async function getJSON(url, signal) {
  const response = await fetch(url, {signal});
  if (!response.ok) throw new Error(String(response.status));
  return response.json();
}

function orderCard(order) {
  const card = template.content.cloneNode(true);
  card.querySelector(".order-id").textContent = "#" + order.id;
  const time = card.querySelector("time");
  time.dateTime = order.ordered_on;
  time.textContent = date.format(new Date(order.ordered_on));
  const state = card.querySelector(".order-state");
  state.dataset.status = order.status;
  state.textContent = statusLabels[order.status];
  for (const item of order.items) {
    const row = itemTemplate.content.cloneNode(true);
    row.querySelector(".product-name").textContent = item.name;
    row.querySelector(".variant").textContent = item.size
      ? item.color + " · Taille " + item.size
      : item.color;
    row.querySelector(".price").textContent = money.format(item.price_cents / 100);
    card.querySelector(".order-items").append(row);
  }
  card.querySelector(".order-total").textContent = money.format(order.total_cents / 100);
  return card;
}

async function loadOrders() {
  currentRequest?.abort();
  currentRequest = new AbortController();
  const {signal} = currentRequest;
  closeChat(false);
  orders.replaceChildren();
  retry.hidden = true;
  status.textContent = "Chargement de vos commandes…";
  const customer = select.selectedOptions[0];
  mailbox.href = "/support/mailbox/" + customer.value;
  try {
    const data = await getJSON("/api/customers/" + customer.value + "/orders", signal);
    if (signal.aborted) return;
    const count = data.orders.length;
    status.textContent = count
      ? count + (count === 1 ? " commande" : " commandes") + " · " + customer.textContent
      : "Aucune commande pour ce compte.";
    orders.replaceChildren(...data.orders.map(orderCard));
  } catch {
    if (signal.aborted) return;
    status.textContent = "Impossible de charger les commandes de ce compte.";
    retry.hidden = false;
  }
}

async function loadCustomers() {
  retry.hidden = true;
  try {
    const {customers} = await getJSON("/api/customers");
    select.replaceChildren(...customers.map(({id, name}) => new Option(name, id)));
    if (!customers.length) {
      status.textContent = "Aucun compte de démonstration disponible.";
      return;
    }
    select.disabled = false;
    helpButton.disabled = false;
    await loadOrders();
  } catch {
    status.textContent = "Impossible de charger les comptes de démonstration.";
    retry.hidden = false;
  }
}

select.addEventListener("change", loadOrders);
helpButton.addEventListener("click", () => openChat(select.value, select.selectedOptions[0].textContent));
retry.addEventListener("click", () => select.disabled ? loadCustomers() : loadOrders());
loadCustomers();
