import argparse
import json
import os
import sys
from uuid import UUID, uuid4

import psycopg
from dotenv import load_dotenv
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command

from .model import Intent, interpret_mistral
from .shop import DEFAULT_DSN, DEMO_ORDER, Shop
from .workflow import LostResponse, build_graph, client_message

def main() -> int:
    parser = argparse.ArgumentParser(description="Agent SAV e-commerce")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Initialise la base de démonstration")
    web = commands.add_parser("web", help="Lance la boutique de démonstration")
    web.add_argument("--port", type=int, default=8000)
    web_mode = web.add_mutually_exclusive_group()
    web_mode.add_argument("--mistral", dest="use_mistral", action="store_true", help="Utilise Mistral (par défaut)")
    web_mode.add_argument("--demo", dest="use_mistral", action="store_false", help="Utilise des règles sans clé API")
    web.set_defaults(use_mistral=True)

    start = commands.add_parser("ouvrir", help="Ouvre un dossier et analyse la demande")
    start.add_argument("message")
    start.add_argument("--commande", default=DEMO_ORDER)
    start.add_argument("--demo", choices=["retour", "echange"], help="Utilise une intention simulée")

    approval = commands.add_parser("decider", help="Répond à la demande d'accord")
    approval.add_argument("dossier", type=UUID)
    approval.add_argument("decision", choices=["oui", "non"])
    approval.add_argument("--simuler-timeout", action="store_true")

    for name, help_text in (
        ("reprendre", "Rejoue une exécution interrompue"),
        ("statut", "Affiche l'état sauvegardé"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("dossier", type=UUID)

    args = parser.parse_args()
    load_dotenv()
    dsn = os.getenv("DATABASE_URL", DEFAULT_DSN)
    if args.command == "web":
        from .web import serve

        serve(dsn, args.port, use_mistral=args.use_mistral)
        return 0

    shop = Shop(dsn)

    try:
        with PostgresSaver.from_conn_string(dsn) as checkpointer:
            if args.command == "init":
                checkpointer.setup()
                shop.setup()
                print("Base SAV initialisée.")
                return 0

            def interpret(message: str) -> Intent:
                demo = getattr(args, "demo", None)
                if demo == "retour":
                    return Intent(action="retour", taille=None)
                if demo == "echange":
                    return Intent(action="echange", taille="M")
                return interpret_mistral(message)

            graph = build_graph(
                checkpointer, shop, interpret,
                simulate_timeout=getattr(args, "simuler_timeout", False),
            )

            if args.command == "ouvrir":
                case_id = str(uuid4())
                config = {"configurable": {"thread_id": case_id}}
                graph.invoke(
                    {
                        "case_id": case_id,
                        "order_id": args.commande,
                        "messages": [{"type": "human", "content": args.message}],
                    },
                    config,
                    durability="sync",
                )
                snapshot = graph.get_state(config)
            else:
                case_id = str(args.dossier)
                config = {"configurable": {"thread_id": case_id}}
                with psycopg.connect(dsn) as lock:
                    lock.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (case_id,))
                    snapshot = graph.get_state(config)
                    if not snapshot.values:
                        raise ValueError("Dossier SAV introuvable.")

                    if args.command == "decider":
                        if snapshot.next != ("attendre_accord",) or not snapshot.interrupts:
                            raise ValueError("Ce dossier n'attend pas d'accord.")
                        graph.invoke(Command(resume=args.decision == "oui"), config, durability="sync")
                    elif args.command == "reprendre":
                        if snapshot.next != ("executer",):
                            raise ValueError("Ce dossier n'a pas d'exécution à reprendre.")
                        graph.invoke(None, config, durability="sync")

                    snapshot = graph.get_state(config)

            output = dict(snapshot.values)
            if "messages" in output:
                output["message"] = client_message(snapshot.values)
                del output["messages"]
            if snapshot.interrupts:
                output["confirmation"] = snapshot.interrupts[0].value
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0
    except LostResponse as error:
        print(f"{error} Relance avec : reprendre {case_id}", file=sys.stderr)
        return 2
    except (ValueError, psycopg.Error) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
