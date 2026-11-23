import {CATEGORIES, categoryOf, colorOf, garment} from "/garments.js";

const ALL = "Tout";
const grid = document.querySelector("#products");
const filters = document.querySelector("#filters");
const status = document.querySelector("#collection-status");
const retry = document.querySelector("#retry-collection");
const template = document.querySelector("#product-card");
const money = new Intl.NumberFormat("fr-FR", {style: "currency", currency: "EUR"});
const sizeOrder = ["S", "M", "L", "XL"];
let cards = [];

function showVariant(card, variant, swatches) {
  card.querySelector(".product-visual").innerHTML = garment(variant.name, variant.color);
  card.querySelector(".product-color").textContent = variant.color;
  card.querySelector(".price").textContent = money.format(variant.price_cents / 100);
  for (const swatch of swatches) {
    swatch.setAttribute("aria-pressed", String(swatch.dataset.id === variant.id));
  }
  const sizes = Object.keys(variant.stock).filter(Boolean)
    .sort((a, b) => sizeOrder.indexOf(a) - sizeOrder.indexOf(b));
  const available = Object.values(variant.stock).some((quantity) => quantity > 0);
  const list = card.querySelector(".product-sizes");
  list.replaceChildren(...sizes.map((size) => {
    const item = document.createElement("li");
    item.textContent = size;
    if (!variant.stock[size]) {
      item.className = "sold-out";
      item.title = "Épuisé";
      item.setAttribute("aria-label", `${size}, épuisé`);
    }
    return item;
  }));
  if (!sizes.length) {
    const item = document.createElement("li");
    item.className = "one-size";
    item.textContent = available ? "Taille unique" : "Épuisé";
    list.append(item);
  }
}

function productCard(variants) {
  const card = template.content.firstElementChild.cloneNode(true);
  const [first] = variants;
  card.dataset.category = categoryOf(first.name);
  card.querySelector(".product-name").textContent = first.name;
  const swatches = variants.map((variant) => {
    const swatch = document.createElement("button");
    swatch.type = "button";
    swatch.className = "swatch";
    swatch.dataset.id = variant.id;
    swatch.style.setProperty("--swatch", colorOf(variant.color));
    swatch.title = variant.color;
    swatch.setAttribute("aria-label", `${first.name}, ${variant.color}`);
    swatch.addEventListener("click", () => showVariant(card, variant, swatches));
    return swatch;
  });
  card.querySelector(".swatches").append(...swatches);
  showVariant(card, first, swatches);
  return card;
}

function filterBy(category) {
  for (const button of filters.children) {
    button.setAttribute("aria-pressed", String(button.textContent === category));
  }
  const shown = cards.filter((card) => category === ALL || card.dataset.category === category);
  grid.replaceChildren(...shown);
  status.textContent = `${shown.length} ${shown.length === 1 ? "article" : "articles"}`;
}

async function loadCollection() {
  retry.hidden = true;
  status.textContent = "Chargement de la collection…";
  try {
    const response = await fetch("/api/products");
    if (!response.ok) throw new Error(String(response.status));
    const {products} = await response.json();
    const byName = Map.groupBy
      ? Map.groupBy(products, ({name}) => name)
      : products.reduce((map, product) => map.set(product.name, [...(map.get(product.name) ?? []), product]), new Map());
    cards = [...byName.values()].map(productCard);
    filters.replaceChildren(...[ALL, ...CATEGORIES].map((category) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "filter";
      button.textContent = category;
      button.addEventListener("click", () => filterBy(category));
      return button;
    }));
    filterBy(ALL);
  } catch {
    status.textContent = "Impossible de charger la collection.";
    retry.hidden = false;
  }
}

// The collection and the purchases share one page, so the chat stays open between the two.
function showView(view) {
  document.documentElement.dataset.view = view;
  document.title = (view === "orders" ? "Mes achats" : "Collection") + " · Maison Orme";
  for (const link of document.querySelectorAll(".site-nav [data-view]")) {
    if (link.dataset.view === view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

const viewOfPath = () => location.pathname === "/orders" ? "orders" : "collection";

document.addEventListener("click", (event) => {
  const link = event.target.closest("a[data-view]");
  if (!link || event.button || event.ctrlKey || event.metaKey || event.shiftKey) return;
  event.preventDefault();
  if (link.pathname !== location.pathname) history.pushState(null, "", link.pathname);
  showView(link.dataset.view);
  scrollTo(0, 0);
});
addEventListener("popstate", () => showView(viewOfPath()));
retry.addEventListener("click", loadCollection);
showView(viewOfPath());
loadCollection();
