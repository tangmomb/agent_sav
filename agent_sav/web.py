import json
import re
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from uuid import UUID

import psycopg
from langgraph.types import Command

from .database import initialize_database, open_checkpointer
from .model import (
    ORDER_REFERENCE, collect_demo, collect_mistral, reply_demo, reply_mistral, select_demo, select_mistral,
)
from .shop import Shop
from .thread_id import customer_thread
from .workflow import MESSAGE_NODES, build_graph

PACKAGE = Path(__file__).resolve().parent
INTERFACE = PACKAGE.parent / "interface"
ASSETS = PACKAGE / "shop_notification"
ROUTES = {
    "/": (INTERFACE / "index.html", "text/html; charset=utf-8"),
    "/orders": (INTERFACE / "index.html", "text/html; charset=utf-8"),
    "/theme.css": (INTERFACE / "theme.css", "text/css; charset=utf-8"),
    "/shop.css": (ASSETS / "boutique.css", "text/css; charset=utf-8"),
    "/chat.js": (ASSETS / "chat.js", "text/javascript; charset=utf-8"),
    "/orders.js": (ASSETS / "achats.js", "text/javascript; charset=utf-8"),
    "/pull.webp": (ASSETS / "pull.webp", "image/webp"),
}
CASE_ID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
SIGNED_REPLY = rf"(?:/support/reply|/sav/reponse)/({CASE_ID})/(confirm|decline|confirmer|refuser)/([0-9a-f]{{64}})"
INQUIRY_REPLY = rf"/shop/support/inquiry/({CASE_ID})"
STATUS_LABELS = {
    "awaiting_customer": "Demande validée. Le mail de confirmation est disponible.",
    "rejected_by_shop": "Demande refusée. Le motif a été communiqué au client.",
    "completed": "Votre demande est confirmée. Le récapitulatif est dans votre boîte mail.",
    "cancelled": "Votre demande est annulée.",
}


def serve(dsn: str, port: int, *, use_mistral: bool) -> None:
    shop = Shop(dsn)
    initialize_database(dsn)
    collect = collect_mistral if use_mistral else collect_demo
    respond = reply_mistral if use_mistral else reply_demo
    select = select_mistral if use_mistral else select_demo

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
                case_id = request["case_id"]
                action_label = "Retour" if request["action"] == "return" else "Échange"
                variant = " · ".join(escape(value) for value in (request["name"], request["color"], request["size"]) if value)
                cards.append(f"""<article class="order-card support-card">
                    <h2>{action_label} · {escape(request["order_id"])}</h2>
                    <p>{escape(request["customer_id"])} · {variant}</p>
                    <form class="refusal-form" method="post" action="/shop/support/{case_id}">
                      <button class="contact" name="decision" value="yes">Valider</button>
                      <label for="motif-{case_id}">Motif du refus
                        <input class="control" id="motif-{case_id}" name="reason" maxlength="1000">
                      </label>
                      <button class="contact secondary" name="decision" value="no">Refuser</button>
                    </form></article>""")
            for inquiry in shop.unanswered_inquiries():
                case_id = inquiry["case_id"]
                question = escape(inquiry["question"]).replace("\n", "<br>")
                cards.append(f"""<article class="order-card support-card">
                    <h2>Renseignement · {escape(inquiry["customer_id"])}</h2>
                    <p>{question}</p>
                    <form class="refusal-form" method="post" action="/shop/support/inquiry/{case_id}">
                      <label for="answer-{case_id}">Réponse
                        <textarea class="control" id="answer-{case_id}" name="answer" maxlength="1000" required></textarea>
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
                options.append(f'<option value="{escape(customer["id"])}"{selected}>{escape(customer["name"])}</option>')
            picker = f"""<form class="account mailbox-account" method="get" action="/support/mailbox">
                <label for="mailbox-customer">Compte de démo</label>
                <select class="control" id="mailbox-customer" name="customer">{"".join(options)}</select>
                <button class="contact" type="submit">Ouvrir la boîte mail</button>
            </form>"""

            cards = []
            for message in shop.mailbox(customer_id):
                links = message.get("links")
                actions = ""
                if links:
                    actions = (
                        f'<div class="support-actions"><a class="contact" href="{links["confirm"]}">Confirmer</a>'
                        f'<a class="contact secondary" href="{links["decline"]}">Refuser</a></div>'
                    )
                cards.append(
                    f'<article class="order-card support-card"><h2>{escape(message["subject"])}</h2>'
                    f'<p>{escape(message["body"]).replace(chr(10), "<br>")}</p>{actions}</article>'
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
                signed_reply = re.fullmatch(SIGNED_REPLY, route)
                if signed_reply:
                    if not shop.request_thread(*signed_reply.groups()):
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    action = "Confirmer" if signed_reply[2] in {"confirm", "confirmer"} else "Refuser"
                    self.page("Votre demande SAV", f'<form method="post" action="{route}">'
                              f'<button class="contact">{action} la demande</button></form>')
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
            signed_reply = re.fullmatch(SIGNED_REPLY, self.path)
            shop_reply = re.fullmatch(rf"/shop/support/({CASE_ID})", self.path)
            if signed_reply or shop_reply:
                try:
                    if signed_reply:
                        thread_id = shop.request_thread(*signed_reply.groups())
                        node = "wait_for_customer_confirmation"
                        decision = signed_reply[2] in {"confirm", "confirmer"}
                    else:
                        thread_id = shop.request_thread(shop_reply[1])
                        node = "wait_for_shop_approval"
                        length = int(self.headers.get("Content-Length", "0"))
                        if not 0 < length <= 16000:
                            raise ValueError("Formulaire invalide.")
                        form = parse_qs(self.rfile.read(length).decode())
                        reply = form.get("decision", [""])[0]
                        reason = form.get("reason", [""])[0].strip()
                        if reply not in {"yes", "no"} or (reply == "no" and not 0 < len(reason) <= 1000):
                            raise ValueError("Indiquez un motif pour refuser la demande.")
                        decision = {"approved": reply == "yes", "reason": reason}
                    if not thread_id:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    status = self.resume_decision(thread_id, node, decision)
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
                if not isinstance(payload, dict) or not isinstance(payload.get("thread_id"), str):
                    raise ValueError
                thread_id = payload["thread_id"]
                customer_id, order_id = customer_thread(thread_id)
                line_number = payload.get("line_number")
                if "line_number" in payload and (
                    order_id is None or type(line_number) is not int or not 0 < line_number <= 2**31 - 1
                ):
                    raise ValueError
                if sum(key in payload for key in ("open", "message", "select_order_id", "select_line_number")) != 1 or "approved" in payload:
                    raise ValueError
                opening = payload.get("open") is True
                message = payload.get("message")
                message_id = None
                selected_order_id = payload.get("select_order_id")
                selected_line = payload.get("select_line_number")
                if "message" in payload:
                    if not isinstance(message, str) or not 0 < len(message.strip()) <= 1000:
                        raise ValueError
                    if "message_id" in payload:
                        if not isinstance(payload["message_id"], str):
                            raise ValueError
                        message_id = str(UUID(payload["message_id"]))
                elif "select_line_number" in payload:
                    if type(selected_line) is not int or not 0 < selected_line <= 2**31 - 1:
                        raise ValueError
                elif "select_order_id" in payload:
                    if order_id is not None or not isinstance(selected_order_id, str) or not ORDER_REFERENCE.fullmatch(selected_order_id):
                        raise ValueError
                    selected_order_id = selected_order_id.upper()
                elif not opening or order_id is None:
                    raise ValueError
            except (ValueError, UnicodeError):
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "La demande n’a pas pu être lue."})
                return

            try:
                if opening:
                    state = self.open_chat(customer_id, order_id, line_number)
                else:
                    state = self.run_case(
                        thread_id, customer_id, order_id, line_number, message, message_id, selected_order_id, selected_line,
                    )
            except Exception as error:
                self.log_error("Échec du dossier SAV (%s)", type(error).__name__)
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {
                    "error": "Le SAV est momentanément indisponible. Vous pouvez réessayer.",
                })
                return
            if not state:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Ce dossier est introuvable."})
                return
            self.send_json(HTTPStatus.OK, state)

        def open_chat(self, customer_id: str, order_id: str, line_number: int | None) -> dict:
            prepared = shop.order_context(customer_id, order_id)
            if prepared is None:
                return {}
            context, count = prepared
            if line_number is not None and not any(item["line_number"] == line_number for item in context["items"]):
                return {}
            order = context["order"]
            return {"greeting": respond({
                "status": "order_greeting", "first_name": context["first_name"], "items": context["items"],
                "order": {"id": order["id"], "status": order["status"]},
                "other_pending_orders": count,
            })}

        def resume_decision(self, thread_id: str, node: str, decision) -> str:
            config = {"configurable": {"thread_id": thread_id}, "run_name": "SAV boutique", "tags": ["shop"]}
            with open_checkpointer(dsn) as checkpointer, psycopg.connect(dsn) as lock:
                lock.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (thread_id,))
                graph = build_graph(checkpointer, shop, collect, respond=respond, select=select)
                snapshot = graph.get_state(config)
                if snapshot.next and not snapshot.interrupts:
                    graph.invoke(None, config, durability="sync")
                    snapshot = graph.get_state(config)
                if snapshot.next == (node,) and snapshot.interrupts:
                    graph.invoke(Command(resume=decision), config, durability="sync")
                    snapshot = graph.get_state(config)
                status = snapshot.values.get("status")
                if node == "wait_for_shop_approval":
                    accepted = {"awaiting_customer", "completed", "cancelled"} if decision["approved"] else {"rejected_by_shop"}
                else:
                    accepted = {"completed"} if decision else {"cancelled"}
                if status not in accepted:
                    raise ValueError("Le dossier n’attend pas cette décision.")
                return status

        def run_case(
            self, thread_id: str, customer_id: str, order_id: str | None, line_number: int | None,
            message: str | None, message_id: str | None, selected_order_id: str | None, selected_line: int | None,
        ) -> dict:
            config = {
                "configurable": {"thread_id": thread_id},
                "run_name": "SAV boutique",
                "tags": ["shop"],
                "metadata": {
                    "customer_id": customer_id, "order_id": order_id, "line_number": line_number,
                },
            }
            with (
                open_checkpointer(dsn) as checkpointer,
                psycopg.connect(dsn) as lock,
            ):
                lock.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (thread_id,))
                graph = build_graph(checkpointer, shop, collect, respond=respond, select=select)

                def invoke_entry(data: dict) -> dict | None:
                    if order_id is None:
                        if shop.orders(customer_id) is None:
                            return {}
                        graph.invoke(data, config, durability="sync")
                        return None
                    prepared = shop.order_context(customer_id, order_id)
                    if prepared is None:
                        return {}
                    context, _ = prepared
                    if line_number is not None and not any(item["line_number"] == line_number for item in context["items"]):
                        return {}
                    status = context["order"]["status"]
                    if status != "delivered":
                        event = "order_in_transit" if status == "in_transit" else "order_confirmed"
                        return {"status": event, "customer_message": respond({
                            "status": event, "order": {"id": order_id, "status": status},
                        }, message)}
                    if line_number is not None:
                        data = Command(update={**data, "line_number": line_number})
                    graph.invoke(data, config, context=context, durability="sync")
                    return None

                snapshot = graph.get_state(config)
                initial_line = snapshot.values.get("initial_line_number", snapshot.values.get("line_number"))
                if line_number is not None and initial_line is not None and initial_line != line_number:
                    return {}
                if snapshot.next and not snapshot.interrupts:
                    graph.invoke(None, config, durability="sync")
                    snapshot = graph.get_state(config)
                already_received = message_id is not None and any(
                    item.id == message_id for item in snapshot.values.get("messages", [])
                )
                pending = snapshot.interrupts[0].value if snapshot.next in MESSAGE_NODES and snapshot.interrupts else {}
                if message_id is not None:
                    already_received |= message_id in pending.get("_message_ids", [])
                if selected_order_id is not None and pending.get("status") == "order_needed":
                    graph.invoke(Command(resume={"order_id": selected_order_id}), config, durability="sync")
                elif selected_line is not None and pending.get("status") == "item_needed":
                    graph.invoke(Command(resume={"line_number": selected_line}), config, durability="sync")
                elif message is not None and not already_received and snapshot.next in MESSAGE_NODES and snapshot.interrupts:
                    reply = {"message": message.strip()}
                    if message_id is not None:
                        reply["message_id"] = message_id
                    graph.invoke(Command(resume=reply), config, durability="sync")
                elif message is not None and not snapshot.values:
                    data = {
                        "messages": [{"type": "human", "content": message.strip(), "id": message_id}],
                    }
                    early_result = invoke_entry(data)
                    if early_result is not None:
                        return early_result

                snapshot = graph.get_state(config)
                state = snapshot.values
                result = {key: state[key] for key in (
                    "status", "action", "size", "color", "return_id", "customer_message", "line_number", "order_id",
                ) if key in state}
                if snapshot.next in MESSAGE_NODES and snapshot.interrupts:
                    result.update({key: val for key, val in snapshot.interrupts[0].value.items() if key != "_message_ids"})
                    result["awaiting_message"] = True
                return result

    with ThreadingHTTPServer(("127.0.0.1", port), BoutiqueHandler) as server:
        print(f"Boutique de démonstration : http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
