import argparse
import json
import os
import sys

import psycopg
from dotenv import load_dotenv

from .alerts import alert_operator
from .database import initialize_database
from .demo_data import DEMO_CUSTOMER
from .service import Support
from .shop import DEFAULT_DSN


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent SAV e-commerce")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Initialise la base de démonstration")
    web = commands.add_parser("web", help="Lance la boutique de démonstration")
    web.add_argument("--port", type=int, default=8000)
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--public", action="store_true", help="Démo publique : une boutique isolée par visiteur")
    commands.add_parser("mcp", help="Lance le serveur MCP").add_argument("--port", type=int, default=8765)
    commands.add_parser("mcp-token", help="Crée un jeton MCP pour un client").add_argument("--customer", default=DEMO_CUSTOMER)
    start = commands.add_parser("open", help="Ouvre un chat avec un premier message")
    start.add_argument("message")
    start.add_argument("--customer", default=DEMO_CUSTOMER)
    say = commands.add_parser("say", help="Continue un chat")
    say.add_argument("chat_id")
    say.add_argument("message")
    decide = commands.add_parser("decide", help="Enregistre ce que la boutique fait d’un dossier")
    decide.add_argument("case_id")
    decide.add_argument("action", choices=["approve", "reject", "answer", "received"])
    decide.add_argument("--text", default="", help="Motif du refus, ou réponse à la question")
    commands.add_parser("case", help="Affiche un dossier").add_argument("case_id")
    commands.add_parser("tick", help="Applique les échéances passées et reprend les dossiers interrompus")

    args = parser.parse_args()
    load_dotenv()
    dsn = os.getenv("DATABASE_URL", DEFAULT_DSN)
    support = Support(dsn)
    try:
        if args.command in ("web", "open", "say") and not os.getenv("MISTRAL_API_KEY"):
            raise ValueError("MISTRAL_API_KEY est absente : l’agent a besoin du modèle.")
        if args.command == "web":
            from .sandboxes import Sandboxes
            from .web import serve

            if args.public:
                initialize_database(dsn)
                serve(dsn, args.port, support, host=args.host, sandboxes=Sandboxes(dsn))
            else:
                serve(dsn, args.port, support, host=args.host)
            return 0
        if args.command == "mcp":
            import uvicorn

            from .mcp_server import build_app

            initialize_database(dsn)
            uvicorn.run(build_app(support), host="127.0.0.1", port=args.port)
            return 0
        if args.command == "mcp-token":
            token = support.shop.create_token(args.customer)
            if token is None:
                raise ValueError("Client introuvable.")
            print(token)
            return 0
        if args.command == "init":
            initialize_database(dsn)
            print("Base SAV initialisée.")
            return 0

        if args.command in ("open", "say"):
            chat_id = support.shop.open_chat(args.customer) if args.command == "open" else args.chat_id
            if chat_id is None:
                raise ValueError("Client introuvable.")
            output = {"chat_id": chat_id, **support.chat(chat_id, args.message)}
        elif args.command == "decide":
            field = {"reject": "reason", "answer": "answer"}.get(args.action)
            if field and not args.text.strip():
                raise ValueError("Indiquez un texte avec --text.")
            output = support.act(args.case_id, args.action, {field: args.text.strip()} if field else {})
        elif args.command == "tick":
            support.tick()
            output = {"reste_à_traiter": support.cases.to_run(), "alerte_envoyée": alert_operator(support.cases)}
        else:
            output = support.cases.get(args.case_id)
            if output is None:
                raise ValueError("Dossier introuvable.")
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
        return 0
    except (ValueError, psycopg.Error) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
