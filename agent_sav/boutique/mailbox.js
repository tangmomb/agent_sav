// Read and starred messages are remembered in this browser only.
const mails = [...document.querySelectorAll(".mail")];
const list = document.querySelector("#mails");
const search = document.querySelector("#search");
const folders = [...document.querySelectorAll("[data-folder]")];
const listTools = document.querySelector("#list-tools");
const messageTools = document.querySelector("#message-tools");
const noMail = document.querySelector("#no-mail");
const account = document.querySelector("#mailbox-customer");
let folder = "inbox";

function remembered(name) {
  try {
    return new Set(JSON.parse(localStorage.getItem(name)) ?? []);
  } catch {
    return new Set();
  }
}

const read = remembered("mail-read");
const starred = remembered("mail-starred");

function remember(name, set, key, on) {
  if (on) set.add(key);
  else set.delete(key);
  try {
    localStorage.setItem(name, JSON.stringify([...set]));
  } catch { /* private window: kept for this page only */ }
}

const opened = () => mails.find(({dataset}) => location.hash === "#message-" + dataset.index);

function show() {
  const current = opened();
  if (current) remember("mail-read", read, current.dataset.key, true);
  for (const message of document.querySelectorAll(".message")) {
    message.hidden = !current || message.id !== "message-" + current.dataset.index;
  }
  list.hidden = listTools.hidden = Boolean(current);
  messageTools.hidden = !current;

  const words = search.value.trim().toLocaleLowerCase("fr");
  let count = 0;
  let unread = 0;
  for (const mail of mails) {
    const isUnread = !read.has(mail.dataset.key);
    const isStarred = starred.has(mail.dataset.key);
    mail.classList.toggle("unread", isUnread);
    mail.querySelector(".star").setAttribute("aria-pressed", String(isStarred));
    const inFolder = folder === "inbox" || (folder === "unread" ? isUnread : isStarred);
    mail.hidden = !inFolder || !mail.textContent.toLocaleLowerCase("fr").includes(words);
    count += !mail.hidden;
    unread += isUnread;
  }
  document.querySelector("#unread-count").textContent = unread || "";
  document.querySelector("#list-count").textContent = count + (count > 1 ? " messages" : " message");
  noMail.hidden = Boolean(current) || count > 0;
  for (const button of folders) button.setAttribute("aria-pressed", String(button.dataset.folder === folder));
}

function backToList() {
  history.replaceState(null, "", location.pathname + location.search);
  show();
}

for (const mail of mails) {
  mail.querySelector(".star").addEventListener("click", () => {
    remember("mail-starred", starred, mail.dataset.key, !starred.has(mail.dataset.key));
    show();
  });
}
for (const button of folders) {
  button.addEventListener("click", () => {
    folder = button.dataset.folder;
    backToList();
  });
}
document.querySelector("#mark-unread").addEventListener("click", () => {
  const {key} = opened().dataset;
  backToList();
  remember("mail-read", read, key, false);
  show();
});
search.addEventListener("input", backToList);
account.addEventListener("change", () => account.form.submit());
addEventListener("hashchange", show);
show();
