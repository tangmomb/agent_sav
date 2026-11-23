"""HTML of the two demo tools: the helpdesk and the customers' mailbox."""

from datetime import datetime
from html import escape

from .cases import KINDS
from .policy import euros

MONTHS = ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc.")
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
    """What every ticket shows, whatever its kind."""
    question = entry["kind"] == "question"
    reference = escape(entry["customer_id"] if question else entry["order_id"])
    return {
        "id": entry["id"],
        "customer": escape(f'{entry["first_name"]} {entry["last_name"]}'),
        "initials": escape(entry["first_name"][0] + entry["last_name"][0]),
        "subject": f"{KINDS[entry['kind']]} · {reference}",
        "reference": reference,
        "excerpt": escape(" ".join((entry["details"] if question else entry["name"] or entry["order_id"]).split())),
        "waiting": waiting_time(entry["updated_at"]),
        "status": "Colis attendu" if entry["status"] == "awaiting_parcel" else "À traiter",
    }


def ticket_row(entry: dict) -> str:
    shown = ticket(entry)
    return f"""<li data-action="{entry["kind"]}"><a class="row" href="#{shown["id"]}">
        <span class="avatar" aria-hidden="true">{shown["initials"]}</span>
        <span class="row-text"><strong>{shown["customer"]}</strong><span>{shown["excerpt"]}</span><small>{shown["reference"]}</small></span>
        <span class="row-side"><time>{shown["waiting"]}</time><span class="tag {entry["kind"]}">{KINDS[entry["kind"]]}</span></span>
        </a></li>"""


RULES = {
    "in_time": "Dans les délais", "late": "Hors délai, à l’appréciation de la boutique",
    "fault": "Article abîmé ou erreur de préparation", "high_value": "Montant à valider",
    "many_requests": "Plusieurs demandes récentes", "lost_parcel": "Colis perdu", "goodwill": "Geste commercial",
}
REASONS = {
    "wrong_size": "Taille ou coupe", "changed_mind": "Ne convient pas", "damaged": "Abîmé ou défectueux",
    "wrong_item": "Mauvais article reçu", "lost_parcel": "Colis jamais reçu", "goodwill": "Geste commercial demandé",
}


def action_form(case_id: str, action: str, label: str) -> str:
    return f"""<form method="post" action="/shop/support/{case_id}">
              <button class="button primary" name="action" value="{action}">{label}</button>
            </form>"""


def welcome(notice: str = "") -> str:
    banner = f'<p class="notice" role="status">{escape(notice)}</p>' if notice else ""
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Agent SAV · Démo</title><link rel="stylesheet" href="/theme.css">
        <style>main {{ width: min(100% - 2rem, 40rem); margin: auto; padding-block: 3rem; display: grid; align-content: center; gap: 1.25rem; }} ul {{ margin: 0; padding-left: 1.2rem; }}
        p, li {{ color: var(--muted); }} .notice {{ color: var(--ink); }}
        button {{ justify-self: start; min-height: 2.75rem; padding: .5rem 1.25rem; border: 1px solid var(--accent-border);
        border-radius: var(--radius); background: var(--accent-surface); color: var(--accent); cursor: pointer; }}</style></head>
        <body><main>
          <h1>Agent SAV</h1>
          {banner}
          <p>Vous jouez les deux rôles : le client qui écrit au service client, puis la boutique qui décide.</p>
          <ul>
            <li>Une boutique rien que pour vous, avec des clients et des commandes fictifs.</li>
            <li>Elle est supprimée après 24 heures sans visite.</li>
            <li>N’écrivez pas d’informations personnelles : vos messages sont traités par un modèle de langage.</li>
          </ul>
          <form method="post" action="/demo"><button>Ouvrir ma boutique</button></form>
          <p>Un cookie garde votre boutique. Il ne sert à rien d’autre.</p>
        </main></body></html>"""


STEPS = {
    "customer": "Client", "agent": "Agent", "recap": "Récapitulatif", "tool": "Outil",
    "taken_back": "Réponse retenue", "case_opened": "Dossier ouvert", "case_command": "Action", "case_mail": "Mail",
}
COMMANDS = {
    "auto": "accepté par les règles", "shop": "soumis à la boutique", "approve": "validé par la boutique",
    "reject": "refusé par la boutique", "answer": "réponse de la boutique", "received": "colis reçu", "remind": "rappel",
    "expire": "délai dépassé", "cancel": "annulé par le client",
}


def timeline(steps: list[dict]) -> str:
    """Everything that led to a case: what was said, the tools called, the rules, the mails."""
    rows = ""
    for step in steps:
        title = STEPS[step["kind"]]
        if step["kind"] == "tool":
            title = f"<code>{escape(step['title'])}</code>"
        elif step["kind"] == "taken_back":
            title += f" : {escape(step['title'])}"
        args = ", ".join(f"{name} = {value}" for name, value in step.get("args", {}).items() if value not in (None, ""))
        text = COMMANDS.get(step["text"], step["text"])
        when = f"<time>{long_date(step['at'])}</time>" if "at" in step else ""
        rows += f"""<li class="{step["kind"]}"><p><strong>{title}</strong>{when}</p>
            {f'<p class="args">{escape(args)}</p>' if args else ""}<p class="text">{lines(text)}</p></li>"""
    return f"""<details class="timeline"><summary>Chronologie de l’agent <span>{len(steps)} étapes</span></summary><ol>{rows}</ol></details>"""


def request_body(case: dict) -> str:
    fields = []
    if case["item_id"]:
        fields += [("Article", escape(case["name"])),
                   ("Variante achetée", variant(case["bought_color"], case["bought_size"]))]
    if case["kind"] == "exchange":
        fields.append(("Variante souhaitée", f'<strong>{variant(case["color"], case["size"])}</strong>'))
    if case["amount_cents"]:
        fields.append(("Remboursement", f'<strong>{euros(case["amount_cents"])}</strong>'))
    fields += [("Motif", escape(REASONS.get(case["reason"], ""))), ("Règle appliquée", RULES.get(case["rule"], ""))]
    if case["details"]:
        fields.append(("Précisions du client", escape(case["details"])))
    rows = "".join(f"<div><dt>{label}</dt><dd>{value}</dd></div>" for label, value in fields)
    if case["status"] == "awaiting_parcel":
        title = "Retour du colis"
        choices = action_form(case["id"], "received", "Colis reçu")
    else:
        title = "Votre décision"
        choices = action_form(case["id"], "approve", "Valider") + f"""
            <details>
              <summary class="button">Refuser</summary>
              <form method="post" action="/shop/support/{case["id"]}">
                <label for="motif-{case["id"]}">Motif envoyé au client</label>
                <textarea id="motif-{case["id"]}" name="reason" maxlength="1000" rows="3" required></textarea>
                <button class="button danger" name="action" value="reject">Confirmer le refus</button>
              </form>
            </details>"""
    return f"""<section class="event">
          <p class="event-head"><span class="avatar bot" aria-hidden="true">IA</span><span><strong>Assistant IA</strong> a qualifié la demande</span><time>{long_date(case["created_at"])}</time></p>
          <dl>{rows}</dl>
        </section>
        <section class="composer">
          <h3>{title}</h3>
          <div class="choices">{choices}</div>
        </section>"""


def inquiry_body(case: dict) -> str:
    return f"""<section class="event">
          <p class="event-head"><span class="avatar bot" aria-hidden="true">IA</span><span><strong>Assistant IA</strong> transmet une demande</span><time>{long_date(case["created_at"])}</time></p>
          <blockquote>{lines(case["details"])}</blockquote>
        </section>
        <section class="composer">
          <h3><label for="answer-{case["id"]}">Répondre par mail</label></h3>
          <form method="post" action="/shop/support/{case["id"]}">
            <textarea id="answer-{case["id"]}" name="answer" maxlength="1000" rows="5" required></textarea>
            <button class="button primary" name="action" value="answer">Envoyer la réponse</button>
          </form>
        </section>"""


def ticket_view(entry: dict) -> str:
    shown = ticket(entry)
    body = inquiry_body(entry) if entry["kind"] == "question" else request_body(entry)
    order = "" if entry["kind"] == "question" else f'<div><dt>Commande</dt><dd>{escape(entry["order_id"])}</dd></div>'
    return f"""<article class="ticket" id="{shown["id"]}">
        <div class="thread">
          <header><p>Ticket #{shown["id"]}</p><h2>{shown["subject"]}</h2><span class="status">{shown["status"]}</span></header>
          {body}
          {timeline(entry.get("timeline", []))}
        </div>
        <aside aria-label="Informations du ticket">
          <h3>Client</h3>
          <p class="person"><span class="avatar" aria-hidden="true">{shown["initials"]}</span><span><strong>{shown["customer"]}</strong><br>{escape(entry["customer_id"])}</span></p>
          <h3>Ticket</h3>
          <dl>
            <div><dt>Type</dt><dd>{KINDS[entry["kind"]]}</dd></div>{order}
            <div><dt>Canal</dt><dd>Chat</dd></div>
            <div><dt>En attente depuis</dt><dd>{shown["waiting"]}</dd></div>
          </dl>
        </aside></article>"""


def helpdesk(entries: list[dict], notice: str = "", error: bool = False) -> str:
    views = {"": "Tous les tickets", "exchange": "Échanges", "return": "Retours", "refund": "Remboursements", "question": "Renseignements"}
    filters = ""
    for action, label in views.items():
        count = sum(action in ("", entry["kind"]) for entry in entries)
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
    key = escape(f'{message["case_id"]}@{message["created_at"].isoformat()}')
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
