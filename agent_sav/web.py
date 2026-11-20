import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID

import psycopg
from langgraph.types import Command

from .database import initialize_database, open_checkpointer
from .model import interpret_demo, interpret_mistral
from .shop import OrderContext, Shop
from .thread_id import order_thread
from .workflow import awaits_size, build_graph

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

        def do_GET(self) -> None:
            route = self.path.partition("?")[0]
            customer = re.fullmatch(r"/api/clients/(CLI-[0-9]{3})/commandes", route)
            try:
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
                line_number = payload.get("line_number", 1)
                if type(line_number) is not int or not 0 < line_number <= 2**31 - 1:
                    raise ValueError
                if sum(key in payload for key in ("open", "message", "approved")) != 1:
                    raise ValueError
                opening = payload.get("open") is True
                message = payload.get("message")
                message_id = None
                approved = payload.get("approved")
                if "message" in payload:
                    if not isinstance(message, str) or not 0 < len(message.strip()) <= 1000:
                        raise ValueError
                    if "message_id" in payload:
                        if not isinstance(payload["message_id"], str):
                            raise ValueError
                        message_id = str(UUID(payload["message_id"]))
                elif not opening and not isinstance(approved, bool):
                    raise ValueError
            except (ValueError, UnicodeError):
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "La demande n’a pas pu être lue."})
                return

            try:
                if opening:
                    state = self.open_chat(customer_id, order_id, line_number)
                else:
                    state = self.run_case(thread_id, customer_id, order_id, line_number, message, message_id, approved)
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

        def open_chat(self, customer_id: str, order_id: str, line_number: int) -> dict:
            prepared = shop.order_context(customer_id, order_id)
            if prepared is None:
                return {}
            context, count = prepared
            if not any(item["line_number"] == line_number for item in context["items"]):
                return {}
            return {"greeting": chat_greeting(context, count)}

        def run_case(
            self, thread_id: str, customer_id: str, order_id: str, line_number: int,
            message: str | None,
            message_id: str | None, approved: bool | None,
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

                def invoke_entry(data: dict) -> bool:
                    prepared = shop.order_context(customer_id, order_id)
                    if prepared is None:
                        return False
                    context, count = prepared
                    graph.invoke(
                        {**data, "line_number": line_number, "active_order_count": count},
                        config, context=context, durability="sync",
                    )
                    return True

                snapshot = graph.get_state(config)
                if snapshot.values and snapshot.values.get("line_number", 1) != line_number:
                    return {}
                if snapshot.next and not snapshot.interrupts:
                    if snapshot.next in {("verifier_statut_commande",), ("presenter_commande",)}:
                        if not invoke_entry({}):
                            return {}
                    else:
                        graph.invoke(None, config, durability="sync")
                    snapshot = graph.get_state(config)
                already_received = message_id is not None and any(
                    item.id == message_id for item in snapshot.values.get("messages", [])
                )
                need_entry = not snapshot.next and snapshot.values.get("status") in {None, "accueil", "demande_recue"}
                if message is None and snapshot.next == ("attendre_accord",) and snapshot.interrupts:
                    graph.invoke(Command(resume=approved), config, durability="sync")
                elif message is not None and not already_received and snapshot.next == ("demander_taille",):
                    reply = {"message": message.strip()}
                    if message_id is not None:
                        reply["message_id"] = message_id
                    graph.invoke(Command(resume=reply), config, durability="sync")
                elif message is not None and (
                    need_entry or (not already_received and not snapshot.next and awaits_size(snapshot.values))
                ):
                    data = {} if already_received else {
                        "messages": [{"type": "human", "content": message.strip(), "id": message_id}],
                    }
                    if not invoke_entry(data):
                        return {}

                snapshot = graph.get_state(config)
                state = snapshot.values
                if state.get("status") in {"accueil", "demande_recue", "commande_inconnue"}:
                    return {}
                result = {key: state[key] for key in ("status", "action", "size", "return_id") if key in state}
                if snapshot.next == ("demander_taille",) and snapshot.interrupts:
                    result["question"] = snapshot.interrupts[0].value["question"]
                return result

    with ThreadingHTTPServer(("127.0.0.1", port), BoutiqueHandler) as server:
        print(f"Boutique de démonstration : http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
