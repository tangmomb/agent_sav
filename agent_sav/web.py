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
from .model import interpret_demo, interpret_mistral
from .shop import OrderContext, Shop
from .thread_id import order_thread
from .workflow import build_graph

PACKAGE = Path(__file__).resolve().parent
INTERFACE = PACKAGE.parent / "interface"
ASSETS = PACKAGE / "boutique"
ROUTES = {
    "/": (INTERFACE / "index.html", "text/html; charset=utf-8"),
    "/mes-achats": (INTERFACE / "index.html", "text/html; charset=utf-8"),
    "/theme.css": (INTERFACE / "theme.css", "text/css; charset=utf-8"),
    "/boutique.css": (ASSETS / "boutique.css", "text/css; charset=utf-8"),
    "/chat.js": (ASSETS / "chat.js", "text/javascript; charset=utf-8"),
    "/achats.js": (ASSETS / "achats.js", "text/javascript; charset=utf-8"),
    "/pull.webp": (ASSETS / "pull.webp", "image/webp"),
}
CASE_ID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
SIGNED_REPLY = rf"/sav/reponse/({CASE_ID})/(confirmer|refuser)/([0-9a-f]{{64}})"
STATUS_LABELS = {
    "attente_client": "Demande validée. Le mail de confirmation est disponible.",
    "refuse_boutique": "Demande refusée. Le motif a été communiqué au client.",
    "termine": "Votre demande est confirmée. Le récapitulatif est dans votre boîte mail.",
    "annule": "Votre demande est annulée.",
}


def chat_greeting(context: OrderContext, count: int) -> str:
    order = context["order"]
    labels = {"validee": "validée", "en_livraison": "en livraison", "livree": "livrée"}
    lines = [
        f"Bonjour {context['first_name']} !",
        f"Votre commande #{order['id']} est {labels[order['status']]}.",
        "",
    ]
    for item in context["items"]:
        variant = f", taille {item['size']}" if item["size"] else ""
        lines.append(f"• {item['name']} ({item['color']}{variant})")
    lines.append("")
    if count == 1:
        lines.append("Vous avez une autre commande en préparation ou en livraison.")
    elif count:
        lines.append(f"Vous avez {count} autres commandes en préparation ou en livraison.")
    else:
        lines.append("Vous n’avez aucune autre commande en préparation ou en livraison.")
    return "\n".join(lines)


def serve(dsn: str, port: int, *, use_mistral: bool) -> None:
    shop = Shop(dsn)
    initialize_database(dsn)
    interpret = interpret_mistral if use_mistral else interpret_demo

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
            html = f"""<!doctype html><html lang="fr"><meta charset="utf-8">
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <title>{title} · Maison Orme</title>
                <link rel="stylesheet" href="/theme.css"><link rel="stylesheet" href="/boutique.css">
                <main class="shell sav-page"><p><a href="/mes-achats">Mes achats</a> · <a href="/boutique/sav">Espace boutique</a></p>
                <h1>{title}</h1>{body}</main></html>"""
            self.reply(status, html.encode(), "text/html; charset=utf-8")

        def shop_page(self) -> None:
            cards = []
            for request in shop.requests_for_shop():
                case_id = request["case_id"]
                variant = " · ".join(escape(value) for value in (request["name"], request["color"], request["size"]) if value)
                cards.append(f"""<article class="order-card sav-card">
                    <h2>{escape(request["action"].capitalize())} · {escape(request["order_id"])}</h2>
                    <p>{escape(request["customer_id"])} · {variant}</p>
                    <form method="post" action="/boutique/sav/{case_id}">
                      <button class="contact" name="decision" value="oui">Valider</button>
                      <label for="motif-{case_id}">Motif du refus</label>
                      <input id="motif-{case_id}" name="reason" maxlength="1000">
                      <button class="contact" name="decision" value="non">Refuser</button>
                    </form></article>""")
            self.page("Demandes SAV", "".join(cards) or "<p>Aucune demande en attente.</p>")

        def mailbox_page(self, customer_id: str | None) -> None:
            customers = shop.customers()
            if not customers:
                self.page("Boîte mail de démonstration", "<p>Aucun compte de démonstration disponible.</p>")
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
            picker = f"""<form class="account mailbox-account" method="get" action="/sav/mail">
                <label for="mail-client">Compte de démo</label>
                <select id="mail-client" name="client">{"".join(options)}</select>
                <button class="contact" type="submit">Ouvrir la boîte mail</button>
            </form>"""

            cards = []
            for message in shop.mailbox(customer_id):
                links = message.get("links")
                actions = ""
                if links:
                    actions = f'<a href="{links["confirmer"]}">Confirmer</a> · <a href="{links["refuser"]}">Refuser</a>'
                cards.append(
                    f'<article class="order-card sav-card"><h2>{escape(message["subject"])}</h2>'
                    f'<p>{escape(message["body"]).replace(chr(10), "<br>")}</p>{actions}</article>'
                )
            self.page("Boîte mail de démonstration", picker + ("".join(cards) or "<p>Aucun message.</p>"))

        def do_GET(self) -> None:
            route, _, query = self.path.partition("?")
            customer = re.fullmatch(r"/api/clients/(CLI-[0-9]{3})/commandes", route)
            try:
                if route == "/boutique/sav":
                    self.shop_page()
                    return
                if route == "/sav/mail":
                    customer_id = parse_qs(query).get("client", [None])[0]
                    self.mailbox_page(customer_id)
                    return
                mailbox = re.fullmatch(r"/sav/mail/(CLI-[0-9]{3})", route)
                if mailbox:
                    self.mailbox_page(mailbox[1])
                    return
                signed_reply = re.fullmatch(SIGNED_REPLY, route)
                if signed_reply:
                    if not shop.request_thread(*signed_reply.groups()):
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    action = signed_reply[2].capitalize()
                    self.page("Votre demande SAV", f'<form method="post" action="{route}">'
                              f'<button class="contact">{action} la demande</button></form>')
                    return
                if route == "/api/clients":
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
            signed_reply = re.fullmatch(SIGNED_REPLY, self.path)
            shop_reply = re.fullmatch(rf"/boutique/sav/({CASE_ID})", self.path)
            if signed_reply or shop_reply:
                try:
                    if signed_reply:
                        thread_id = shop.request_thread(*signed_reply.groups())
                        node = "attendre_confirmation_client"
                        decision = signed_reply[2] == "confirmer"
                    else:
                        thread_id = shop.request_thread(shop_reply[1])
                        node = "attendre_validation_boutique"
                        length = int(self.headers.get("Content-Length", "0"))
                        if not 0 < length <= 16000:
                            raise ValueError("Formulaire invalide.")
                        form = parse_qs(self.rfile.read(length).decode())
                        reply = form.get("decision", [""])[0]
                        reason = form.get("reason", [""])[0].strip()
                        if reply not in {"oui", "non"} or (reply == "non" and not 0 < len(reason) <= 1000):
                            raise ValueError("Indiquez un motif pour refuser la demande.")
                        decision = {"approved": reply == "oui", "reason": reason}
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
            if self.path != "/api/sav":
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
                customer_id, order_id = order_thread(thread_id)
                line_number = payload.get("line_number")
                if "line_number" in payload and (type(line_number) is not int or not 0 < line_number <= 2**31 - 1):
                    raise ValueError
                if sum(key in payload for key in ("open", "message", "article")) != 1 or "approved" in payload:
                    raise ValueError
                opening = payload.get("open") is True
                message = payload.get("message")
                message_id = None
                article = payload.get("article")
                if "message" in payload:
                    if not isinstance(message, str) or not 0 < len(message.strip()) <= 1000:
                        raise ValueError
                    if "message_id" in payload:
                        if not isinstance(payload["message_id"], str):
                            raise ValueError
                        message_id = str(UUID(payload["message_id"]))
                elif "article" in payload:
                    if type(article) is not int or not 0 < article <= 2**31 - 1:
                        raise ValueError
                elif not opening:
                    raise ValueError
            except (ValueError, UnicodeError):
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "La demande n’a pas pu être lue."})
                return

            try:
                if opening:
                    state = self.open_chat(customer_id, order_id, line_number)
                else:
                    state = self.run_case(thread_id, customer_id, order_id, line_number, message, message_id, article)
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
            return {"greeting": chat_greeting(context, count)}

        def resume_decision(self, thread_id: str, node: str, decision) -> str:
            config = {"configurable": {"thread_id": thread_id}, "run_name": "SAV boutique", "tags": ["boutique"]}
            with open_checkpointer(dsn) as checkpointer, psycopg.connect(dsn) as lock:
                lock.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (thread_id,))
                graph = build_graph(checkpointer, shop, interpret)
                snapshot = graph.get_state(config)
                if snapshot.next and not snapshot.interrupts:
                    graph.invoke(None, config, durability="sync")
                    snapshot = graph.get_state(config)
                if snapshot.next == (node,) and snapshot.interrupts:
                    graph.invoke(Command(resume=decision), config, durability="sync")
                    snapshot = graph.get_state(config)
                status = snapshot.values.get("status")
                if node == "attendre_validation_boutique":
                    accepted = {"attente_client", "termine", "annule"} if decision["approved"] else {"refuse_boutique"}
                else:
                    accepted = {"termine"} if decision else {"annule"}
                if status not in accepted:
                    raise ValueError("Le dossier n’attend pas cette décision.")
                return status

        def run_case(
            self, thread_id: str, customer_id: str, order_id: str, line_number: int | None,
            message: str | None,
            message_id: str | None, article: int | None,
        ) -> dict:
            config = {
                "configurable": {"thread_id": thread_id},
                "run_name": "SAV boutique",
                "tags": ["boutique"],
                "metadata": {
                    "customer_id": customer_id, "order_id": order_id, "line_number": line_number,
                },
            }
            with (
                open_checkpointer(dsn) as checkpointer,
                psycopg.connect(dsn) as lock,
            ):
                lock.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (thread_id,))
                graph = build_graph(checkpointer, shop, interpret)

                def invoke_entry(data: dict | Command | None) -> dict | None:
                    prepared = shop.order_context(customer_id, order_id)
                    if prepared is None:
                        return {}
                    context, _ = prepared
                    if line_number is not None and not any(item["line_number"] == line_number for item in context["items"]):
                        return {}
                    status = context["order"]["status"]
                    if status != "livree":
                        return {"status": "commande_en_transit" if status == "en_livraison" else "commande_validee"}
                    if isinstance(data, dict) and line_number is not None:
                        data = {**data, "line_number": line_number}
                    graph.invoke(data, config, context=context, durability="sync")
                    return None

                snapshot = graph.get_state(config)
                if line_number is not None and snapshot.values and snapshot.values.get("line_number", 1) != line_number:
                    return {}
                if snapshot.next and not snapshot.interrupts:
                    if snapshot.next == ("choisir_action",):
                        early_result = invoke_entry(None)
                        if early_result is not None:
                            return early_result
                    else:
                        graph.invoke(None, config, durability="sync")
                    snapshot = graph.get_state(config)
                already_received = message_id is not None and any(
                    item.id == message_id for item in snapshot.values.get("messages", [])
                )
                if message_id is not None and snapshot.interrupts:
                    already_received |= message_id in snapshot.interrupts[0].value.get("_message_ids", [])
                if article is not None and snapshot.next == ("choisir_article",) and snapshot.interrupts:
                    graph.invoke(Command(resume={"line_number": article}), config, durability="sync")
                elif message is not None and not already_received and snapshot.next in {
                    ("choisir_action",), ("demander_taille",),
                } and snapshot.interrupts:
                    reply = {"message": message.strip()}
                    if message_id is not None:
                        reply["message_id"] = message_id
                    command = Command(resume=reply)
                    if snapshot.next == ("choisir_action",):
                        early_result = invoke_entry(command)
                        if early_result is not None:
                            return early_result
                    else:
                        graph.invoke(command, config, durability="sync")
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
                    "status", "action", "size", "color", "return_id", "message_client", "line_number",
                ) if key in state}
                if snapshot.next == ("choisir_article",) and snapshot.interrupts:
                    result.update(status="article_a_preciser", **snapshot.interrupts[0].value)
                if snapshot.next in {("choisir_action",), ("demander_taille",)} and snapshot.interrupts:
                    result.update({key: val for key, val in snapshot.interrupts[0].value.items() if key != "_message_ids"})
                return result

    with ThreadingHTTPServer(("127.0.0.1", port), BoutiqueHandler) as server:
        print(f"Boutique de démonstration : http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
