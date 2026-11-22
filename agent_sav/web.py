import json
import re
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from uuid import UUID

import psycopg
from langchain_core.messages import AIMessage
from langgraph.types import Command

from .database import initialize_database, open_checkpointer
from .shop import Shop
from .thread_id import CHAT_THREAD
from .workflow import build_graph

PACKAGE = Path(__file__).resolve().parent
INTERFACE = PACKAGE.parent / "interface"
ASSETS = PACKAGE / "boutique"
ROUTES = {
    "/": (INTERFACE / "index.html", "text/html; charset=utf-8"),
    "/orders": (INTERFACE / "index.html", "text/html; charset=utf-8"),
    "/theme.css": (INTERFACE / "theme.css", "text/css; charset=utf-8"),
    "/shop.css": (ASSETS / "boutique.css", "text/css; charset=utf-8"),
    "/chat.js": (ASSETS / "chat.js", "text/javascript; charset=utf-8"),
    "/orders.js": (ASSETS / "achats.js", "text/javascript; charset=utf-8"),
    "/pull.webp": (ASSETS / "pull.webp", "image/webp"),
}
THREAD_ID = CHAT_THREAD
INQUIRY_REPLY = rf"/shop/support/inquiry/({THREAD_ID})"
STATUS_LABELS = {
    "rejected_by_shop": "Demande refusée. Le motif a été communiqué au client.",
    "completed": "Demande acceptée. Le client a reçu la confirmation par mail.",
}


def serve(dsn: str, port: int) -> None:
    shop = Shop(dsn)
    initialize_database(dsn)

    class BoutiqueHandler(BaseHTTPRequestHandler):
        def reply(self, status: HTTPStatus, data: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def send_json(self, status: HTTPStatus, data: dict) -> None:
            self.reply(status, json.dumps(data, ensure_ascii=False).encode(), "application/json")

        def page(self, title: str, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            title = escape(title)
            mail_current = ' aria-current="page"' if title == "Boîte mail de démonstration" else ""
            shop_current = ' aria-current="page"' if title == "Demandes SAV" else ""
            html = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <title>{title} · Maison Orme</title>
                <link rel="stylesheet" href="/theme.css"><link rel="stylesheet" href="/shop.css">
                </head><body>
                <header class="site-header"><div class="shell header-inner">
                  <a class="wordmark" href="/orders">Maison Orme<span>.</span></a>
                  <nav class="site-nav" aria-label="Navigation">
                    <a href="/orders">Mes achats</a>
                    <a href="/support/mailbox"{mail_current}>Boîte mail</a>
                    <a href="/shop/support"{shop_current}>Espace boutique</a>
                  </nav>
                </div></header>
                <main class="shell"><h1 class="page-heading">{title}</h1>{body}</main>
                </body></html>"""
            self.reply(status, html.encode(), "text/html; charset=utf-8")

        def shop_page(self) -> None:
            cards = []
            for request in shop.requests_for_shop():
                thread_id = request["thread_id"]
                action_label = "Retour" if request["action"] == "return" else "Échange"
                variant = " · ".join(escape(value) for value in (request["name"], request["color"], request["size"]) if value)
                cards.append(f"""<article class="order-card support-card">
                    <h2>{action_label} · {escape(request["order_id"])}</h2>
                    <p>{escape(request["customer_id"])} · {variant}</p>
                    <form class="refusal-form" method="post" action="/shop/support/{thread_id}">
                      <button class="contact" name="decision" value="yes">Valider</button>
                      <label for="motif-{thread_id}">Motif du refus
                        <input class="control" id="motif-{thread_id}" name="reason" maxlength="1000">
                      </label>
                      <button class="contact secondary" name="decision" value="no">Refuser</button>
                    </form></article>""")
            for inquiry in shop.unanswered_inquiries():
                thread_id = inquiry["thread_id"]
                question = escape(inquiry["question"]).replace("\n", "<br>")
                cards.append(f"""<article class="order-card support-card">
                    <h2>Renseignement · {escape(inquiry["customer_id"])}</h2>
                    <p>{question}</p>
                    <form class="refusal-form" method="post" action="/shop/support/inquiry/{thread_id}">
                      <label for="answer-{thread_id}">Réponse
                        <textarea class="control" id="answer-{thread_id}" name="answer" maxlength="1000" required></textarea>
                      </label>
                      <button class="contact" type="submit">Envoyer la réponse</button>
                    </form></article>""")
            self.page("Demandes SAV", "".join(cards) or '<p class="empty-state">Aucune demande en attente.</p>')

        def mailbox_page(self, customer_id: str | None) -> None:
            customers = shop.customers()
            if not customers:
                self.page("Boîte mail de démonstration", '<p class="empty-state">Aucun compte de démonstration disponible.</p>')
                return
            if customer_id is None:
                customer_id = customers[0]["id"]
            if not any(customer["id"] == customer_id for customer in customers):
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            options = []
            for customer in customers:
                selected = " selected" if customer["id"] == customer_id else ""
                options.append(f'<option value="{escape(customer["id"])}"{selected}>{escape(customer["first_name"])} {escape(customer["name"])}</option>')
            picker = f"""<form class="account mailbox-account" method="get" action="/support/mailbox">
                <label for="mailbox-customer">Compte de démo</label>
                <select class="control" id="mailbox-customer" name="customer">{"".join(options)}</select>
                <button class="contact" type="submit">Ouvrir la boîte mail</button>
            </form>"""

            cards = []
            for message in shop.mailbox(customer_id):
                cards.append(
                    f'<article class="order-card support-card"><h2>{"Agent SAV" if message["sender"] == "sav" else "Boutique"}</h2>'
                    f'<p>{escape(message["body"]).replace(chr(10), "<br>")}</p></article>'
                )
            self.page("Boîte mail de démonstration", picker + ("".join(cards) or '<p class="empty-state">Aucun message.</p>'))

        def do_GET(self) -> None:
            route, _, query = self.path.partition("?")
            customer = re.fullmatch(r"/api/customers/(CLI-[0-9]{3})/orders", route)
            try:
                if route == "/shop/support":
                    self.shop_page()
                    return
                if route == "/support/mailbox":
                    customer_id = parse_qs(query).get("customer", [None])[0]
                    self.mailbox_page(customer_id)
                    return
                mailbox = re.fullmatch(r"/support/mailbox/(CLI-[0-9]{3})", route)
                if mailbox:
                    self.mailbox_page(mailbox[1])
                    return
                if route == "/api/customers":
                    self.send_json(HTTPStatus.OK, {"customers": shop.customers()})
                    return
                if customer:
                    orders = shop.orders(customer[1])
                    if orders is None:
                        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Client introuvable."})
                    else:
                        self.send_json(HTTPStatus.OK, {"orders": orders})
                    return
            except psycopg.Error:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "La boutique est momentanément indisponible."})
                return

            asset = ROUTES.get(route)
            if asset is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            path, content_type = asset
            self.reply(HTTPStatus.OK, path.read_bytes(), content_type)

        def do_POST(self) -> None:
            inquiry_reply = re.fullmatch(INQUIRY_REPLY, self.path)
            if inquiry_reply:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 16000:
                        raise ValueError("Formulaire invalide.")
                    form = parse_qs(self.rfile.read(length).decode())
                    answer = form.get("answer", [""])[0].strip()
                    if not 0 < len(answer) <= 1000:
                        raise ValueError("Indiquez une réponse de 1 à 1 000 caractères.")
                    if not shop.answer_inquiry(inquiry_reply[1], answer):
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    self.page("Demande SAV", "<p>Réponse disponible dans la boîte mail de démonstration du client.</p>")
                except ValueError as error:
                    self.page("Demande SAV", f"<p>{escape(str(error))}</p>", HTTPStatus.BAD_REQUEST)
                except psycopg.Error:
                    self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)
                return
            shop_reply = re.fullmatch(rf"/shop/support/({THREAD_ID})", self.path)
            if shop_reply:
                try:
                    thread_id = shop.request_thread(shop_reply[1])
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 16000:
                        raise ValueError("Formulaire invalide.")
                    form = parse_qs(self.rfile.read(length).decode())
                    reply = form.get("decision", [""])[0]
                    reason = form.get("reason", [""])[0].strip()
                    if reply not in {"yes", "no"} or (reply == "no" and not 0 < len(reason) <= 1000):
                        raise ValueError("Indiquez un motif pour refuser la demande.")
                    if not thread_id:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    status = self.shop_decision(thread_id, {"approved": reply == "yes", "reason": reason})
                    self.page("Demande SAV", f"<p>{STATUS_LABELS[status]}</p>")
                except ValueError as error:
                    self.page("Demande SAV", f"<p>{escape(str(error))}</p>", HTTPStatus.BAD_REQUEST)
                except psycopg.Error:
                    self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)
                return
            if self.path != "/api/support":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192 or self.headers.get_content_type() != "application/json":
                    raise ValueError
                payload = json.loads(self.rfile.read(length))
                thread_id, message = payload["thread_id"], payload["message"].strip()
                message_id = str(UUID(payload["message_id"])) if "message_id" in payload else None
                if not re.fullmatch(THREAD_ID, thread_id) or not 0 < len(message) <= 1000:
                    raise ValueError
            except (ValueError, KeyError, TypeError, AttributeError, UnicodeError):
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "La demande n’a pas pu être lue."})
                return
            try:
                self.send_json(HTTPStatus.OK, self.chat(thread_id, message, message_id))
            except Exception as error:
                self.log_error("Échec du dossier SAV (%s)", type(error).__name__)
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {
                    "error": "Le SAV est momentanément indisponible. Vous pouvez réessayer.",
                })

        def shop_decision(self, thread_id: str, decision: dict) -> str:
            config = {"configurable": {"thread_id": thread_id}, "run_name": "SAV boutique", "tags": ["shop"]}
            with open_checkpointer(dsn) as checkpointer, psycopg.connect(dsn) as lock:
                lock.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (thread_id,))
                graph = build_graph(checkpointer, shop)
                snapshot = graph.get_state(config)
                if snapshot.next != ("shop_review",):
                    raise ValueError("Le dossier n’attend pas cette décision.")
                graph.invoke(Command(resume=decision), config, durability="sync")
                return graph.get_state(config).values["status"]

        def chat(self, thread_id: str, message: str, message_id: str | None) -> dict:
            """Passes a customer message to the graph and returns its answer."""
            config = {"configurable": {"thread_id": thread_id}, "run_name": "SAV boutique", "tags": ["shop"]}
            with open_checkpointer(dsn) as checkpointer, psycopg.connect(dsn) as lock:
                lock.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (thread_id,))
                graph = build_graph(checkpointer, shop)
                snapshot = graph.get_state(config)
                received = any(m.id == message_id for m in snapshot.values.get("messages", [])) if message_id else False
                if not received:
                    if not snapshot.values:
                        graph.invoke({"messages": [{"type": "human", "content": message, "id": message_id}]},
                                     config, durability="sync")
                    elif snapshot.next == ("ask",):
                        graph.invoke(Command(resume={"message": message, "id": message_id}), config, durability="sync")
                snapshot = graph.get_state(config)
            answer = next(str(m.text) for m in reversed(snapshot.values["messages"]) if isinstance(m, AIMessage))
            if snapshot.next == ("ask",):
                return {"question": answer, "awaiting_message": True}
            return {"status": snapshot.values.get("status"), "messages": [{"role": "assistant", "content": answer}]}

    with ThreadingHTTPServer(("127.0.0.1", port), BoutiqueHandler) as server:
        print(f"Boutique de démonstration : http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
