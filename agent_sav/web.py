import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode
from uuid import UUID

import psycopg
from langchain_core.messages import AIMessage
from langgraph.types import Command

from . import pages
from .database import initialize_database, open_checkpointer
from .shop import Shop
from .thread_id import CHAT_THREAD
from .workflow import CUSTOMER_PAUSES, build_graph

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
    "/catalogue.js": (ASSETS / "catalogue.js", "text/javascript; charset=utf-8"),
    "/helpdesk.css": (ASSETS / "helpdesk.css", "text/css; charset=utf-8"),
    "/helpdesk.js": (ASSETS / "helpdesk.js", "text/javascript; charset=utf-8"),
    "/mailbox.css": (ASSETS / "mailbox.css", "text/css; charset=utf-8"),
    "/mailbox.js": (ASSETS / "mailbox.js", "text/javascript; charset=utf-8"),
    "/garments.js": (ASSETS / "vetements.js", "text/javascript; charset=utf-8"),
    "/pull.webp": (ASSETS / "pull.webp", "image/webp"),
}
THREAD_ID = CHAT_THREAD
INQUIRY_REPLY = rf"/shop/support/inquiry/({THREAD_ID})"
NOTICES = {
    "answered": "Réponse envoyée au client par mail.",
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

        def html(self, page: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            self.reply(status, page.encode(), "text/html; charset=utf-8")

        def shop_page(self, notice: str = "") -> None:
            self.html(pages.helpdesk(shop.requests_for_shop(), shop.unanswered_inquiries(), notice))

        def shop_error(self, error: ValueError) -> None:
            page = pages.helpdesk(shop.requests_for_shop(), shop.unanswered_inquiries(), str(error), error=True)
            self.html(page, HTTPStatus.BAD_REQUEST)

        def shop_done(self, notice: str) -> None:
            """Back to the list, so that reloading it does not send the form again."""
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/shop/support?" + urlencode({"done": notice}))
            self.send_header("Content-Length", "0")
            self.end_headers()

        def mailbox_page(self, customer_id: str | None) -> None:
            customers = shop.customers()
            customer = next((each for each in customers if customer_id in (None, each["id"])), None)
            if customer is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.html(pages.mailbox(customers, customer, shop.mailbox(customer["id"])))

        def do_GET(self) -> None:
            route, _, query = self.path.partition("?")
            customer = re.fullmatch(r"/api/customers/(CLI-[0-9]{3})/orders", route)
            try:
                if route == "/shop/support":
                    self.shop_page(NOTICES.get(parse_qs(query).get("done", [""])[0], ""))
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
                if route == "/api/products":
                    self.send_json(HTTPStatus.OK, {"products": shop.products()})
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
                    self.shop_done("answered")
                except ValueError as error:
                    self.shop_error(error)
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
                    self.shop_done(status)
                except ValueError as error:
                    self.shop_error(error)
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
                    elif snapshot.next in CUSTOMER_PAUSES:
                        reply = {"message": message} | ({"id": message_id} if message_id else {})
                        graph.invoke(Command(resume=reply), config, durability="sync")
                snapshot = graph.get_state(config)
            if snapshot.next in CUSTOMER_PAUSES:
                return {"question": snapshot.interrupts[0].value["question"], "awaiting_message": True}
            answer = next(str(m.text) for m in reversed(snapshot.values["messages"]) if isinstance(m, AIMessage))
            return {"status": snapshot.values.get("status"), "messages": [{"role": "assistant", "content": answer}]}

    with ThreadingHTTPServer(("127.0.0.1", port), BoutiqueHandler) as server:
        print(f"Boutique de démonstration : http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
