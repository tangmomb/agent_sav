"""HTML of the two demo tools: the helpdesk and the customers' mailbox."""

from datetime import datetime
from html import escape

MONTHS = ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc.")
ACTIONS = {"exchange": "Échange", "return": "Retour", "information": "Renseignement"}
SENDERS = {"sav": "Agent SAV", "shop": "Maison Orme"}


def short_date(moment: datetime) -> str:
    """The time for today, the day otherwise."""
    moment = moment.astimezone()
    if moment.date() == datetime.now().astimezone().date():
        return f"{moment:%H:%M}"
    return f"{moment.day} {MONTHS[moment.month - 1]}"


def long_date(moment: datetime) -> str:
    moment = moment.astimezone()
    return f"{moment.day} {MONTHS[moment.month - 1]} {moment:%Y à %H:%M}"


def lines(text: str) -> str:
    return escape(text).replace("\n", "<br>")


def document(title: str, fonts: str, name: str, body: str) -> str:
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
        <title>{title}</title>
        <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family={fonts}&display=swap">
        <link rel="stylesheet" href="/{name}.css">
        <script type="module" src="/{name}.js"></script>
        </head><body>{body}</body></html>"""


def variant(color: str | None, size: str | None) -> str:
    return escape(" · ".join(value for value in (color, size) if value))


def waiting_time(since: datetime) -> str:
    minutes = int((datetime.now().astimezone() - since).total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 1)} min"
    return f"{minutes // 60} h" if minutes < 48 * 60 else f"{minutes // (24 * 60)} j"


def ticket(entry: dict) -> dict:
    """What every ticket shows, whether it is a return, an exchange or a question."""
    question = entry["action"] == "information"
    reference = escape(entry["customer_id"] if question else entry["order_id"])
    return {
        "id": entry["thread_id"][-36:].split("-")[0].upper(),
        "customer": escape(f'{entry["first_name"]} {entry["last_name"]}'),
        "initials": escape(entry["first_name"][0] + entry["last_name"][0]),
        "subject": f"{ACTIONS[entry['action']]} · {reference}",
        "reference": reference,
        "excerpt": escape(" ".join(entry["question"].split())) if question else escape(entry["name"]),
        "waiting": waiting_time(entry["created_at"]),
    }


def ticket_row(entry: dict) -> str:
    shown = ticket(entry)
    return f"""<li data-action="{entry["action"]}"><a class="row" href="#{shown["id"]}">
        <span class="avatar" aria-hidden="true">{shown["initials"]}</span>
        <span class="row-text"><strong>{shown["customer"]}</strong><span>{shown["excerpt"]}</span><small>{shown["reference"]}</small></span>
        <span class="row-side"><time>{shown["waiting"]}</time><span class="tag {entry["action"]}">{ACTIONS[entry["action"]]}</span></span>
        </a></li>"""


def request_body(request: dict) -> str:
    thread_id = request["thread_id"]
    fields = [("Article", escape(request["name"])),
              ("Variante achetée", variant(request["bought_color"], request["bought_size"]))]
    if request["action"] == "exchange":
        fields.append(("Variante souhaitée", f'<strong>{variant(request["color"], request["size"])}</strong>'))
    rows = "".join(f"<div><dt>{label}</dt><dd>{value}</dd></div>" for label, value in fields)
    return f"""<section class="event">
          <p class="event-head"><span class="avatar bot" aria-hidden="true">IA</span><span><strong>Assistant IA</strong> a qualifié la demande</span><time>{long_date(request["created_at"])}</time></p>
          <dl>{rows}</dl>
        </section>
        <section class="composer">
          <h3>Votre décision</h3>
          <div class="choices">
            <form method="post" action="/shop/support/{thread_id}">
              <button class="button primary" name="decision" value="yes">Valider</button>
            </form>
            <details>
              <summary class="button">Refuser</summary>
              <form method="post" action="/shop/support/{thread_id}">
                <label for="motif-{thread_id}">Motif envoyé au client</label>
                <textarea id="motif-{thread_id}" name="reason" maxlength="1000" rows="3" required></textarea>
                <button class="button danger" name="decision" value="no">Confirmer le refus</button>
              </form>
            </details>
          </div>
        </section>"""


def inquiry_body(inquiry: dict) -> str:
    thread_id = inquiry["thread_id"]
    shown = ticket(inquiry)
    return f"""<section class="event">
          <p class="event-head"><span class="avatar" aria-hidden="true">{shown["initials"]}</span><span><strong>{shown["customer"]}</strong> a écrit</span><time>{long_date(inquiry["created_at"])}</time></p>
          <blockquote>{lines(inquiry["question"])}</blockquote>
        </section>
        <section class="composer">
          <h3><label for="answer-{thread_id}">Répondre par mail</label></h3>
          <form method="post" action="/shop/support/inquiry/{thread_id}">
            <textarea id="answer-{thread_id}" name="answer" maxlength="1000" rows="5" required></textarea>
            <button class="button primary">Envoyer la réponse</button>
          </form>
        </section>"""


def ticket_view(entry: dict) -> str:
    shown = ticket(entry)
    body = inquiry_body(entry) if entry["action"] == "information" else request_body(entry)
    order = "" if entry["action"] == "information" else f'<div><dt>Commande</dt><dd>{escape(entry["order_id"])}</dd></div>'
    return f"""<article class="ticket" id="{shown["id"]}">
        <div class="thread">
          <header><p>Ticket #{shown["id"]}</p><h2>{shown["subject"]}</h2><span class="status">À traiter</span></header>
          {body}
        </div>
        <aside aria-label="Informations du ticket">
          <h3>Client</h3>
          <p class="person"><span class="avatar" aria-hidden="true">{shown["initials"]}</span><span><strong>{shown["customer"]}</strong><br>{escape(entry["customer_id"])}</span></p>
          <h3>Ticket</h3>
          <dl>
            <div><dt>Type</dt><dd>{ACTIONS[entry["action"]]}</dd></div>{order}
            <div><dt>Canal</dt><dd>Chat</dd></div>
            <div><dt>En attente depuis</dt><dd>{shown["waiting"]}</dd></div>
          </dl>
        </aside></article>"""


def helpdesk(requests: list[dict], inquiries: list[dict], notice: str = "", error: bool = False) -> str:
    entries = sorted(requests + [{**inquiry, "action": "information"} for inquiry in inquiries], key=lambda entry: entry["created_at"])
    views = {"": "Tous les tickets", "exchange": "Échanges", "return": "Retours", "information": "Renseignements"}
    filters = ""
    for action, label in views.items():
        count = sum(action in ("", entry["action"]) for entry in entries)
        disabled = " disabled" if action and not count else ""
        filters += (f'<button type="button" data-filter="{action}" aria-pressed="{"false" if action else "true"}"{disabled}>'
                    f'<i class="dot {action}"></i>{label} <span>{count}</span></button>')
    banner = f'<p class="notice{" error" if error else ""}" role="status">{escape(notice)}</p>' if notice else ""
    content = f"""<section class="queue" aria-label="Tickets">
          <header><h1 id="view-title">Tous les tickets</h1><input id="search" type="search" placeholder="Rechercher un ticket" aria-label="Rechercher un ticket"></header>
          <ul>{"".join(map(ticket_row, entries))}</ul>
          <p class="no-result" hidden>Aucun ticket trouvé.</p>
        </section>
        <main>{banner}{"".join(map(ticket_view, entries))}</main>""" if entries else f"""<main class="done">{banner}
          <div class="empty"><span aria-hidden="true">✓</span><h1>Aucune demande en attente</h1>
          <p>Rechargez la page pour voir les nouveaux tickets.</p></div></main>"""
    return document("Deskalo · Tickets", "IBM+Plex+Sans:wght@400;500;600", "helpdesk", f"""
        <nav class="rail" aria-label="Vues">
          <p class="brand"><svg viewBox="0 0 32 32" aria-hidden="true"><path d="M4 8a5 5 0 0 1 5-5h9a11 11 0 0 1 0 22h-4l-7 5v-5.4A5 5 0 0 1 4 20z"/><path d="M11 14.5l3.5 3.5 6.5-7"/></svg>Deskalo</p>
          <p class="rail-title">Vues</p>{filters}
          <p class="workspace"><span class="avatar" aria-hidden="true">S</span><span>Équipe SAV<br><small>Agent de démonstration</small></span></p>
        </nav>{content}""")


ICONS = {
    "mail": '<path d="M3 6.5h18v12H3zM3.5 7l8.5 6.5L20.5 7"/>',
    "inbox": '<path d="M4 13 6.5 5h11L20 13v6H4zM4 13h4.5l1.5 2.5h4l1.5-2.5H20"/>',
    "star": '<path d="m12 3.8 2.5 5.3 5.7.8-4.1 4.1 1 5.8-5.1-2.8-5.1 2.8 1-5.8-4.1-4.1 5.7-.8z"/>',
    "unread": '<circle cx="12" cy="12" r="4.5"/>',
    "search": '<circle cx="10.5" cy="10.5" r="6"/><path d="m15 15 5 5"/>',
    "pen": '<path d="m5 19 1-4L16 5l3 3L9 18z"/>',
    "back": '<path d="M19 12H5m6-6-6 6 6 6"/>',
}


def icon(name: str) -> str:
    return f'<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">{ICONS[name]}</svg>'


def mail_row(index: int, message: dict) -> str:
    subject, _, content = message["body"].partition("\n")
    sender = SENDERS[message["sender"]]
    key = escape(f'{message["thread_id"]}@{message["created_at"].isoformat()}')
    return f"""<li class="mail" data-key="{key}" data-index="{index}">
        <button type="button" class="star" aria-label="Suivre" aria-pressed="false">{icon("star")}</button>
        <a class="row" href="#message-{index}">
          <span class="sender">{sender}</span>
          <span class="text"><span class="subject">{escape(subject)}</span><span class="snippet"> - {escape(" ".join(content.split()))}</span></span>
          <time>{short_date(message["created_at"])}</time>
        </a></li>"""


def mail_view(index: int, message: dict, customer: dict) -> str:
    subject, _, content = message["body"].partition("\n")
    sender = SENDERS[message["sender"]]
    return f"""<article class="message" id="message-{index}" hidden>
        <h1>{escape(subject)} <span class="label">Boîte de réception</span></h1>
        <header>
          <span class="avatar {message["sender"]}" aria-hidden="true">{sender[0]}</span>
          <p><strong>{sender}</strong> <span>&lt;sav@maison-orme.example&gt;</span><br><span>À {escape(customer["first_name"])} {escape(customer["name"])}</span></p>
          <time>{long_date(message["created_at"])}</time>
        </header>
        <p class="body">{lines(content)}</p></article>"""


def mailbox(customers: list[dict], customer: dict, messages: list[dict]) -> str:
    options = "".join(
        f'<option value="{escape(each["id"])}"{" selected" if each is customer else ""}>{escape(each["first_name"])} {escape(each["name"])}</option>'
        for each in customers
    )
    rows = "".join(mail_row(index, message) for index, message in enumerate(messages, 1))
    views = "".join(mail_view(index, message, customer) for index, message in enumerate(messages, 1))
    return document("Boîte mail de démonstration", "Roboto:wght@400;500;700", "mailbox", f"""
        <header class="topbar">
          <a class="logo" href="/support/mailbox/{customer["id"]}">{icon("mail")} Boîte mail</a>
          <label class="search">{icon("search")}<input id="search" type="search" placeholder="Rechercher dans les messages" aria-label="Rechercher dans les messages"></label>
          <form class="account" method="get" action="/support/mailbox">
            <label for="mailbox-customer">Compte de démo</label>
            <select id="mailbox-customer" name="customer">{options}</select>
            <span class="avatar" aria-hidden="true">{escape(customer["first_name"][0])}</span>
          </form>
        </header>
        <nav class="folders" aria-label="Dossiers">
          <a class="compose" href="/?customer={customer["id"]}#chat">{icon("pen")} Écrire au SAV</a>
          <button type="button" data-folder="inbox" aria-pressed="true">{icon("inbox")} Boîte de réception <span id="unread-count"></span></button>
          <button type="button" data-folder="unread" aria-pressed="false">{icon("unread")} Non lus</button>
          <button type="button" data-folder="starred" aria-pressed="false">{icon("star")} Suivis</button>
        </nav>
        <main>
          <div class="toolbar" id="message-tools" hidden>
            <a class="tool" href="#" aria-label="Retour à la liste">{icon("back")}</a>
            <button type="button" class="tool text-tool" id="mark-unread">Marquer comme non lu</button>
          </div>
          <div class="toolbar" id="list-tools"><span id="list-count"></span></div>
          <ul id="mails">{rows}</ul>
          <p class="empty" id="no-mail"{" hidden" if messages else ""}>Aucun message.</p>
          {views}
        </main>""")
