import json
import re
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID

import psycopg

from . import pages
from .alerts import alert_operator
from .database import initialize_database
from .graph_map import graph_map
from .sandboxes import BudgetSpent, Full, Limit, Sandboxes
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
    "/graphs": (INTERFACE / "graphes.html", "text/html; charset=utf-8"),
    "/graphs.css": (INTERFACE / "graphes.css", "text/css; charset=utf-8"),
    "/graphs.js": (INTERFACE / "graphes.js", "text/javascript; charset=utf-8"),
    "/pull.webp": (ASSETS / "pull.webp", "image/webp"),
}
TICK_SECONDS = 60
COOKIE = "sav_demo"
MESSAGES_PER_HOUR = 60  # per visitor address, public demo only
SHOPS_PER_DAY = 5
BUDGET_SPENT = "La démo a atteint son budget du jour. Revenez demain."
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


def serve(dsn: str, port: int, support: Support | None = None, *, host: str = "127.0.0.1",
          sandboxes: Sandboxes | None = None) -> None:
    """The demo website. With sandboxes it is public: each visitor gets a shop of their own.

    Without them everyone shares one shop and its back office, so only this machine is served.
    """
    if not sandboxes and host not in ("127.0.0.1", "localhost"):
        raise ValueError("Une boutique partagée ne s’ouvre qu’à cette machine : utilisez --public.")
    shared = support or Support(dsn)
    initialize_database(dsn)
    stopped = Event()
    messages, shops = Limit(MESSAGES_PER_HOUR, 3600), Limit(SHOPS_PER_DAY, 86400)

    def keep_time() -> None:
        while not stopped.wait(TICK_SECONDS):
            try:
                if sandboxes:
                    sandboxes.tick()
                else:
                    shared.tick()
                    alert_operator(shared.cases)
            except Exception as error:  # tried again at the next tick
                print(f"Échéances ou alerte non traitées ({type(error).__name__})", flush=True)

    class BoutiqueHandler(BaseHTTPRequestHandler):
        support: Support | None = None  # the shop this request is about

        def visitor(self) -> str:
            """The address of the visitor, as the reverse proxy saw it."""
            forwarded = self.headers.get("X-Forwarded-For", "") if sandboxes else ""
            return forwarded.split(",")[-1].strip() or self.client_address[0]

        def token(self) -> str | None:
            try:
                cookie = SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE)
            except CookieError:
                return None
            return cookie.value if cookie else None

        def refused(self) -> bool:
            """Requests no page of this site sends: another host, or a form posted from another site."""
            name = urlsplit(f"//{self.headers.get('Host', '')}").hostname
            origin = self.headers.get("Origin")
            if (not sandboxes and name not in ("127.0.0.1", "localhost")) or (
                    origin and urlsplit(origin).netloc != self.headers.get("Host")):
                self.send_error(HTTPStatus.FORBIDDEN)
                return True
            return False

        def enter(self) -> bool:
            """Finds the shop of the visitor. Without one, offers to open it."""
            self.support = sandboxes.open(self.token()) if sandboxes else shared
            if self.support:
                return True
            if self.command == "GET" and not self.path.startswith("/api/"):
                self.html(pages.welcome())
            else:
                self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Votre session de démo a expiré. Rechargez la page."})
            return False

        def open_shop(self) -> None:
            if not shops.allow(self.visitor()):
                self.html(pages.welcome("Vous avez déjà ouvert plusieurs démos aujourd’hui. Revenez demain."), HTTPStatus.TOO_MANY_REQUESTS)
                return
            try:
                token = sandboxes.create()
            except Full:
                self.html(pages.welcome("Toutes les démos sont occupées. Réessayez plus tard."), HTTPStatus.SERVICE_UNAVAILABLE)
                return
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Set-Cookie", f"{COOKIE}={token}; Path=/; Max-Age=86400; HttpOnly; Secure; SameSite=Lax")
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def reply(self, status: HTTPStatus, data: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.end_headers()
            self.wfile.write(data)

        def send_json(self, status: HTTPStatus, data: dict) -> None:
            self.reply(status, json.dumps(data, ensure_ascii=False).encode(), "application/json")

        def html(self, page: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            self.reply(status, page.encode(), "text/html; charset=utf-8")

        def shop_page(self, notice: str = "") -> None:
            self.html(self.helpdesk(notice))

        def shop_error(self, error: ValueError) -> None:
            self.html(self.helpdesk(str(error), error=True), HTTPStatus.BAD_REQUEST)

        def helpdesk(self, notice: str, error: bool = False) -> str:
            waiting = self.support.cases.for_shop()
            return pages.helpdesk([{**case, "timeline": self.support.timeline(case["id"])} for case in waiting], notice, error)

        def shop_done(self, notice: str) -> None:
            """Back to the list, so that reloading it does not send the form again."""
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/shop/support?" + urlencode({"done": notice}))
            self.send_header("Content-Length", "0")
            self.end_headers()

        def mailbox_page(self, customer_id: str | None) -> None:
            customers = self.support.shop.customers()
            customer = next((each for each in customers if customer_id in (None, each["id"])), None)
            if customer is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.html(pages.mailbox(customers, customer, self.support.cases.mailbox(customer["id"])))

        def do_GET(self) -> None:
            route, _, query = self.path.partition("?")
            if self.refused():
                return
            if (asset := ROUTES.get(route)) and not (sandboxes and route in ("/", "/orders")):
                path, content_type = asset
                self.reply(HTTPStatus.OK, path.read_bytes(), content_type)
                return
            if route == "/api/graphs":  # about the code, not about anyone's shop
                self.send_json(HTTPStatus.OK, graph_map())
                return
            customer = re.fullmatch(r"/api/customers/(CLI-[0-9]{3})/orders", route)
            try:
                if not self.enter():
                    return
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
                    self.send_json(HTTPStatus.OK, {"customers": self.support.shop.customers()})
                    return
                if route == "/api/products":
                    self.send_json(HTTPStatus.OK, {"products": self.support.shop.products()})
                    return
                if customer:
                    orders = self.support.shop.orders(customer[1])
                    if orders is None:
                        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Client introuvable."})
                    else:
                        self.send_json(HTTPStatus.OK, {"orders": orders})
                    return
            except psycopg.Error:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "La boutique est momentanément indisponible."})
                return

            if route not in ROUTES:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            path, content_type = ROUTES[route]
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
            if self.refused():
                return
            if sandboxes and self.path == "/demo":
                self.open_shop()
                return
            try:
                if not self.enter():
                    return
            except psycopg.Error:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)
                return
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
                self.support.act(case_id, action, payload)
                self.shop_done(action)
            except ValueError as error:  # including an action the case does not expect
                self.shop_error(error)
            except psycopg.Error:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)

        def open_chat(self) -> None:
            """Signs a customer in. The demo has no password: it only offers fictional accounts."""
            try:
                chat_id = self.support.shop.open_chat(str(self.read_json()["customer_id"]))
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
            if sandboxes and not messages.allow(self.visitor()):
                self.send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Trop de messages en une heure. Faites une pause."})
                return
            try:
                self.send_json(HTTPStatus.OK, self.support.chat(chat_id, message, message_id))
            except BudgetSpent:
                self.send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": BUDGET_SPENT})
            except UnknownChat:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "La demande n’a pas pu être lue."})
            except Exception as error:
                self.log_error("Échec du chat SAV (%s)", type(error).__name__)
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {
                    "error": "Le SAV est momentanément indisponible. Vous pouvez réessayer.",
                })

    with ThreadingHTTPServer((host, port), BoutiqueHandler) as server:
        print(f"Boutique de démonstration : http://{host}:{server.server_port}", flush=True)
        Thread(target=keep_time, daemon=True).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stopped.set()
