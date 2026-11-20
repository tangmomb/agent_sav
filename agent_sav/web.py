import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command

from .model import interpret_demo, interpret_mistral
from .shop import DEMO_ORDER, Shop
from .workflow import awaits_size, build_graph

PACKAGE = Path(__file__).resolve().parent
INTERFACE = PACKAGE.parent / "interface"
ASSETS = PACKAGE / "boutique"
ROUTES = {
    "/": (INTERFACE / "index.html", "text/html; charset=utf-8"),
    "/theme.css": (INTERFACE / "theme.css", "text/css; charset=utf-8"),
    "/boutique.css": (ASSETS / "boutique.css", "text/css; charset=utf-8"),
    "/chat.js": (ASSETS / "chat.js", "text/javascript; charset=utf-8"),
    "/pull.webp": (ASSETS / "pull.webp", "image/webp"),
}


def serve(dsn: str, port: int, *, use_mistral: bool) -> None:
    shop = Shop(dsn)
    with PostgresSaver.from_conn_string(dsn) as checkpointer:
        checkpointer.setup()
    shop.setup()
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
            asset = ROUTES.get(self.path)
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
                if not isinstance(payload, dict) or not isinstance(payload.get("case_id"), str):
                    raise ValueError
                case_id = str(UUID(payload["case_id"]))
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
                elif not isinstance(approved, bool):
                    raise ValueError
            except (ValueError, UnicodeError):
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "La demande n’a pas pu être lue."})
                return

            try:
                state = self.run_case(case_id, message, message_id, approved)
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

        def run_case(
            self, case_id: str, message: str | None, message_id: str | None, approved: bool | None,
        ) -> dict:
            config = {
                "configurable": {"thread_id": case_id},
                "run_name": "SAV boutique",
                "tags": ["boutique"],
                "metadata": {"case_id": case_id, "order_id": DEMO_ORDER},
            }
            with (
                PostgresSaver.from_conn_string(dsn) as checkpointer,
                psycopg.connect(dsn) as lock,
            ):
                lock.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (case_id,))
                graph = build_graph(checkpointer, shop, interpret)
                snapshot = graph.get_state(config)
                already_received = message_id is not None and any(
                    item.id == message_id for item in snapshot.values.get("messages", [])
                )
                if snapshot.next and not snapshot.interrupts:
                    graph.invoke(None, config, durability="sync")
                elif message is None and snapshot.next == ("attendre_accord",) and snapshot.interrupts:
                    graph.invoke(Command(resume=approved), config, durability="sync")
                elif message is not None and not already_received and snapshot.next == ("demander_taille",):
                    reply = {"message": message.strip()}
                    if message_id is not None:
                        reply["message_id"] = message_id
                    graph.invoke(Command(resume=reply), config, durability="sync")
                elif message is not None and not already_received and (
                    not snapshot.values or (not snapshot.next and awaits_size(snapshot.values))
                ):
                    graph.invoke(
                        {
                            "case_id": case_id,
                            "messages": [{"type": "human", "content": message.strip(), "id": message_id}],
                        },
                        config, durability="sync",
                    )

                snapshot = graph.get_state(config)
                state = snapshot.values
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
