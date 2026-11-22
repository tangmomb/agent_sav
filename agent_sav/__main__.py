import argparse
import json
import os
import re
import sys
from uuid import UUID, uuid4

import psycopg
from dotenv import load_dotenv
from langgraph.types import Command

from .database import initialize_database, open_checkpointer
from .demo_data import DEMO_CUSTOMER, DEMO_ORDER
from .model import (
    CollectionContext, CollectionTurn, Intent, collect_demo, collect_mistral, reply_demo, reply_mistral,
    select_demo, select_mistral,
)
from .shop import DEFAULT_DSN, Shop
from .thread_id import customer_thread, order_thread
from .workflow import MESSAGE_NODES, LostResponse, build_graph, trace_state


def parse_thread(value: str) -> str:
    try:
        if ":" in value:
            customer_thread(value)
            return value
        return str(UUID(value))
    except ValueError as error:
        raise argparse.ArgumentTypeError("Identifiant de thread invalide.") from error

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

    start = commands.add_parser("open", help="Ouvre un dossier et analyse la demande")
    start.add_argument("message")
    start.add_argument("--customer", default=DEMO_CUSTOMER)
    start.add_argument("--order", default=DEMO_ORDER)
    start.add_argument("--item-id", help="Identifiant de l’article acheté")
    start.add_argument("--demo", choices=["return", "exchange"], help="Utilise une intention simulée")

    article = commands.add_parser("item", help="Choisit un article dans la commande")
    article.add_argument("thread_id", type=parse_thread)
    article.add_argument("item_id")

    clarify = commands.add_parser("clarify", help="Répond en texte libre à la question du SAV")
    clarify.add_argument("thread_id", type=parse_thread)
    clarify.add_argument("message")
    clarify.add_argument("--demo", action="store_true", help="Utilise des règles sans clé API")

    approval = commands.add_parser("decide", help="Enregistre la décision de la boutique")
    approval.add_argument("thread_id", type=parse_thread)
    approval.add_argument("decision", choices=["yes", "no"])
    approval.add_argument("--reason")

    reply = commands.add_parser("reply", help="Répond en utilisant le lien signé du mail")
    reply.add_argument("link")
    reply.add_argument("--simulate-timeout", action="store_true")

    for name, help_text in (
        ("resume", "Rejoue une exécution interrompue"),
        ("status", "Affiche l'état sauvegardé"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("thread_id", type=parse_thread)

    args = parser.parse_args()
    load_dotenv()
    dsn = os.getenv("DATABASE_URL", DEFAULT_DSN)
    if args.command == "web":
        from .web import serve

        serve(dsn, args.port, use_mistral=args.use_mistral)
        return 0

    try:
        if args.command == "init":
            initialize_database(dsn)
            print("Base SAV initialisée.")
            return 0

        shop = Shop(dsn)
        if args.command == "reply":
            from .web import SIGNED_REPLY

            match = re.fullmatch(SIGNED_REPLY, args.link)
            args.thread_id = shop.request_thread(*match.groups()) if match else None
            if not args.thread_id:
                raise ValueError("Lien de réponse invalide.")

        with open_checkpointer(dsn) as checkpointer:
            demo = getattr(args, "demo", None)
            offline = bool(demo)

            def collect(message: str, context: CollectionContext) -> CollectionTurn:
                if demo in {"return", "exchange"}:
                    return collect_demo(message, context, interpret=lambda *_: Intent(
                        action=demo, size="M" if demo == "exchange" else None, color=None,
                    ))
                return collect_demo(message, context) if offline else collect_mistral(message, context)

            def respond(event: dict, message: str | None) -> str:
                return (reply_demo if offline else reply_mistral)(event, message)

            def select(message: str, question: str, options: list[str]) -> int | None:
                return (select_demo if offline else select_mistral)(message, question, options)

            graph = build_graph(
                checkpointer, shop, collect, respond=respond, select=select,
                simulate_timeout=getattr(args, "simulate_timeout", False),
            )

            if args.command == "open":
                thread_id = f"{args.customer}:{args.order}:{uuid4()}"
                order_thread(thread_id)
                config = {"configurable": {"thread_id": thread_id}}
                prepared = shop.order_context(args.customer, args.order)
                if prepared is None:
                    raise ValueError("Commande introuvable pour ce client.")
                context, _ = prepared
                if args.item_id is not None and not any(item["item_id"] == args.item_id for item in context["items"]):
                    raise ValueError("Cet article ne fait pas partie de la commande.")
                status = context["order"]["status"]
                if status != "delivered":
                    result = "order_in_transit" if status == "in_transit" else "order_confirmed"
                    print(json.dumps({"thread_id": thread_id, "status": result}, ensure_ascii=False, indent=2))
                    return 0
                data = {"messages": [{"type": "human", "content": args.message}]}
                if args.item_id is not None:
                    data["item_id"] = args.item_id
                if offline:
                    data["demo_mode"] = True
                entry = Command(update=data) if args.item_id is not None or offline else data
                graph.invoke(
                    entry,
                    config,
                    context=context,
                    durability="sync",
                )
                snapshot = graph.get_state(config)
            else:
                thread_id = args.thread_id
                config = {"configurable": {"thread_id": thread_id}}
                with psycopg.connect(dsn) as lock:
                    lock.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (thread_id,))
                    snapshot = graph.get_state(config)
                    if not snapshot.values:
                        raise ValueError("Dossier SAV introuvable.")
                    offline = offline or snapshot.values.get("demo_mode", False)
                    if args.command in {"item", "clarify", "decide", "reply"} and snapshot.next and not snapshot.interrupts:
                        graph.invoke(None, config, durability="sync")
                        snapshot = graph.get_state(config)

                    if args.command == "item":
                        if (
                            snapshot.next != ("collect",) or not snapshot.interrupts
                            or snapshot.interrupts[0].value.get("status") != "item_needed"
                        ):
                            raise ValueError("Ce dossier n’attend pas de choix d’article.")
                        if not any(item["item_id"] == args.item_id for item in snapshot.interrupts[0].value["items"]):
                            raise ValueError("Cet article ne fait pas partie de la commande.")
                        graph.invoke(Command(resume={"item_id": args.item_id}), config, durability="sync")
                    elif args.command == "clarify":
                        if snapshot.next not in MESSAGE_NODES or not snapshot.interrupts:
                            raise ValueError("Ce dossier n’attend pas de précision.")
                        graph.invoke(Command(resume={"message": args.message}), config, durability="sync")
                    elif args.command == "decide":
                        if snapshot.next != ("wait_for_shop_approval",) or not snapshot.interrupts:
                            raise ValueError("Ce dossier n’attend pas la décision de la boutique.")
                        reason = (args.reason or "").strip()
                        if args.decision == "no" and not 0 < len(reason) <= 1000:
                            raise ValueError("Indiquez un motif avec --reason.")
                        graph.invoke(Command(resume={"approved": args.decision == "yes", "reason": reason}), config, durability="sync")
                    elif args.command == "reply":
                        if snapshot.next != ("wait_for_customer_confirmation",) or not snapshot.interrupts:
                            raise ValueError("Ce dossier n’attend pas la réponse du client.")
                        graph.invoke(Command(resume=match[2] in {"confirm", "confirmer"}), config, durability="sync")
                    elif args.command == "resume":
                        if not snapshot.next or snapshot.interrupts:
                            raise ValueError("Ce dossier n'a pas d'exécution à reprendre.")
                        graph.invoke(None, config, durability="sync")

                    snapshot = graph.get_state(config)

            output = {"thread_id": thread_id, **trace_state(snapshot.values)}
            output.pop("demo_mode", None)
            if snapshot.interrupts:
                output["interrupt"] = {key: val for key, val in snapshot.interrupts[0].value.items() if key != "_message_ids"}
            if output.get("status") == "awaiting_customer":
                for mail in shop.mailbox(output["customer_id"]):
                    if mail.get("links", {}).get("confirm", "").startswith(f"/support/reply/{output['case_id']}/"):
                        output["email_links"] = mail["links"]
                        break
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
