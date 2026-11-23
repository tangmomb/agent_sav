const filters = [...document.querySelectorAll("[data-filter]")];
const rows = [...document.querySelectorAll(".row")];
const tickets = document.querySelectorAll(".ticket");
const search = document.querySelector("#search");
let view = "";

const idOf = (row) => row.hash.slice(1);

function show() {
  const words = search.value.trim().toLocaleLowerCase("fr");
  const visible = rows.filter((row) => {
    const item = row.parentElement;
    const text = `${idOf(row)} ${row.textContent}`.toLocaleLowerCase("fr");
    item.hidden = Boolean(view && item.dataset.action !== view) || !text.includes(words);
    return !item.hidden;
  });
  const current = visible.find((row) => row.hash === location.hash) ?? visible[0];
  for (const row of rows) {
    if (row === current) row.setAttribute("aria-current", "true");
    else row.removeAttribute("aria-current");
  }
  for (const ticket of tickets) ticket.hidden = !current || ticket.id !== idOf(current);
  for (const filter of filters) filter.setAttribute("aria-pressed", String(filter.dataset.filter === view));
  document.querySelector(".no-result").hidden = visible.length > 0;
}

if (search) {
  for (const filter of filters) {
    filter.addEventListener("click", () => {
      view = filter.dataset.filter;
      document.querySelector("#view-title").textContent = filter.childNodes[1].textContent.trim();
      show();
    });
  }
  search.addEventListener("input", show);
  addEventListener("hashchange", show);
  show();
}

for (const details of document.querySelectorAll(".choices details")) {
  details.addEventListener("toggle", () => details.open && details.querySelector("textarea").focus());
}

// The notice shows once: reloading the page does not repeat it.
if (location.search) history.replaceState(null, "", location.pathname + location.hash);
