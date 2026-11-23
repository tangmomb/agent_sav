import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from urllib.parse import parse_qs, urlencode
from uuid import UUID

import psycopg

from . import pages
from .database import initialize_database
from .service import Support, UnknownChat

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
TICK_SECONDS = 60
CASE_ACTION = r"/shop/support/(SAV-[0-9A-F]{8})"
CHAT_ID = r"chat-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# What the shop can do with a case, and the text it has to give, if any.
ACTIONS = {"approve": "", "reject": "reason", "answer": "answer", "received": ""}
NOTICES = {
    "answer": "Réponse envoyée au client par mail.",
    "reject": "Demande refusée. Le motif a été communiqué au client.",
    "approve": "Demande acceptée. Le client a reçu la confirmation par mail.",
    "received": "Colis enregistré. Le client a reçu la confirmation par mail.",
}


def serve(dsn: str, port: int, support: Support | None = None) -> None:
    support = support or Support(dsn)
    shop, cases = support.shop, support.cases
    initialize_database(dsn)
    stopped = Event()

    def keep_time() -> None:
        while not stopped.wait(TICK_SECONDS):
            try:
                support.tick()
            except psycopg.Error as error:
                print(f"Échéances non traitées ({type(error).__name__})", flush=True)

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
            self.html(pages.helpdesk(cases.for_shop(), notice))

        def shop_error(self, error: ValueError) -> None:
            page = pages.helpdesk(cases.for_shop(), str(error), error=True)
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
            self.html(pages.mailbox(customers, customer, cases.mailbox(customer["id"])))

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

        def read_body(self, limit: int) -> bytes:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= limit:
                raise ValueError("Formulaire invalide.")
            return self.rfile.read(length)

        def read_json(self) -> dict:
            if self.headers.get_content_type() != "application/json":
                raise ValueError
            payload = json.loads(self.read_body(8192))
            if not isinstance(payload, dict):
                raise ValueError
            return payload

        def do_POST(self) -> None:
            if action := re.fullmatch(CASE_ACTION, self.path):
                self.shop_action(action[1])
            elif self.path == "/api/chats":
                self.open_chat()
            elif self.path == "/api/support":
                self.support_message()
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def shop_action(self, case_id: str) -> None:
            try:
                form = parse_qs(self.read_body(16000).decode())
                action = form.get("action", [""])[0]
                if action not in ACTIONS:
                    raise ValueError("Action inconnue.")
                payload = {}
                if field := ACTIONS[action]:
                    text = form.get(field, [""])[0].strip()
                    if not 0 < len(text) <= 1000:
                        raise ValueError("Indiquez un texte de 1 à 1 000 caractères.")
                    payload = {field: text}
                support.act(case_id, action, payload)
                self.shop_done(action)
            except ValueError as error:  # including an action the case does not expect
                self.shop_error(error)
            except psycopg.Error:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)

        def open_chat(self) -> None:
            """Signs a customer in. The demo has no password: it only offers fictional accounts."""
            try:
                chat_id = shop.open_chat(str(self.read_json()["customer_id"]))
                if chat_id is None:
                    raise ValueError
            except (ValueError, KeyError):
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Client introuvable."})
            except psycopg.Error:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Le SAV est momentanément indisponible."})
            else:
                self.send_json(HTTPStatus.OK, {"chat_id": chat_id})

        def support_message(self) -> None:
            try:
                payload = self.read_json()
                chat_id, message = payload["chat_id"], payload["message"].strip()
                message_id = str(UUID(payload["message_id"])) if "message_id" in payload else None
                if not re.fullmatch(CHAT_ID, chat_id) or not 0 < len(message) <= 1000:
                    raise ValueError
            except (ValueError, KeyError, TypeError, AttributeError):
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "La demande n’a pas pu être lue."})
                return
            try:
                self.send_json(HTTPStatus.OK, support.chat(chat_id, message, message_id))
            except UnknownChat:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "La demande n’a pas pu être lue."})
            except Exception as error:
                self.log_error("Échec du chat SAV (%s)", type(error).__name__)
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {
                    "error": "Le SAV est momentanément indisponible. Vous pouvez réessayer.",
                })

    with ThreadingHTTPServer(("127.0.0.1", port), BoutiqueHandler) as server:
        print(f"Boutique de démonstration : http://127.0.0.1:{server.server_port}", flush=True)
        Thread(target=keep_time, daemon=True).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stopped.set()
