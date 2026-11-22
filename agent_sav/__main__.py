import argparse
import json
import os
import sys
from uuid import uuid4

import psycopg
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from .database import initialize_database, open_checkpointer
from .demo_data import DEMO_CUSTOMER
from .shop import DEFAULT_DSN, Shop
from .workflow import LostResponse, build_graph


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent SAV e-commerce (sans MISTRAL_API_KEY, des règles simples lisent les messages)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Initialise la base de démonstration")
    commands.add_parser("web", help="Lance la boutique de démonstration").add_argument("--port", type=int, default=8000)
    start = commands.add_parser("open", help="Ouvre un dossier avec un premier message")
    start.add_argument("message")
    start.add_argument("--customer", default=DEMO_CUSTOMER)
    say = commands.add_parser("say", help="Répond à la question du SAV")
    say.add_argument("thread_id")
    say.add_argument("message")
    decide = commands.add_parser("decide", help="Enregistre la décision de la boutique")
    decide.add_argument("thread_id")
    decide.add_argument("decision", choices=["yes", "no"])
    decide.add_argument("--reason", default="")
    decide.add_argument("--simulate-timeout", action="store_true", help="Perd la réponse après l'enregistrement")
    for name, help_text in (("resume", "Rejoue une exécution interrompue"), ("status", "Affiche l'état sauvegardé")):
        commands.add_parser(name, help=help_text).add_argument("thread_id")

    args = parser.parse_args()
    load_dotenv()
    dsn = os.getenv("DATABASE_URL", DEFAULT_DSN)
    if args.command == "web":
        from .web import serve

        serve(dsn, args.port)
        return 0
    if args.command == "init":
        initialize_database(dsn)
        print("Base SAV initialisée.")
        return 0

    shop = Shop(dsn)
    thread_id = getattr(args, "thread_id", None)
    try:
        if args.command == "open":
            thread_id = f"{args.customer}:{uuid4()}"
        if args.command == "decide" and args.decision == "no" and not args.reason.strip():
            raise ValueError("Indiquez un motif avec --reason.")

        config = {"configurable": {"thread_id": thread_id}}
        with open_checkpointer(dsn) as checkpointer, psycopg.connect(dsn) as lock:
            lock.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (thread_id,))
            graph = build_graph(checkpointer, shop, simulate_timeout=getattr(args, "simulate_timeout", False))
            snapshot = graph.get_state(config)
            if args.command != "open" and not snapshot.values:
                raise ValueError("Dossier SAV introuvable.")
            expected = {"say": "ask", "decide": "shop_review"}.get(args.command)
            if expected and snapshot.next != (expected,):
                raise ValueError(f"Ce dossier n’attend pas cette étape (étape en cours : {', '.join(snapshot.next) or 'aucune'}).")
            if args.command == "resume" and not any(task.error for task in snapshot.tasks):
                raise ValueError("Ce dossier n'a pas d'exécution à reprendre.")

            entry = {
                "open": lambda: {"messages": [{"type": "human", "content": args.message}]},
                "say": lambda: Command(resume={"message": args.message}),
                "decide": lambda: Command(resume={"approved": args.decision == "yes", "reason": args.reason.strip()}),
                "resume": lambda: None,
            }.get(args.command)
            if entry:
                graph.invoke(entry(), config, durability="sync")
            snapshot = graph.get_state(config)

        output = {key: value for key, value in snapshot.values.items() if key != "messages"}
        output = {"thread_id": thread_id, **output, "next": list(snapshot.next), "messages": [
            {"role": "client" if isinstance(m, HumanMessage) else "sav", "content": str(m.text)}
            for m in snapshot.values["messages"]
        ]}
        if output.get("status") == "completed":
            output["reference"] = shop.reference(thread_id)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except LostResponse as error:
        print(f"{error} Relance avec : resume {thread_id}", file=sys.stderr)
        return 2
    except (ValueError, psycopg.Error) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
