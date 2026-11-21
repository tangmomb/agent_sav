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
from .model import Intent, interpret_demo, interpret_mistral
from .shop import DEFAULT_DSN, Shop
from .thread_id import order_thread
from .workflow import LostResponse, build_graph, client_message


def parse_thread(value: str) -> str:
    try:
        if ":" in value:
            order_thread(value)
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

    start = commands.add_parser("ouvrir", help="Ouvre un dossier et analyse la demande")
    start.add_argument("message")
    start.add_argument("--client", default=DEMO_CUSTOMER)
    start.add_argument("--commande", default=DEMO_ORDER)
    start.add_argument("--ligne", type=int, help="Numéro de l’article dans la commande")
    start.add_argument("--demo", choices=["retour", "echange"], help="Utilise une intention simulée")

    article = commands.add_parser("article", help="Choisit un article dans la commande")
    article.add_argument("dossier", type=parse_thread)
    article.add_argument("ligne", type=int)

    clarify = commands.add_parser("preciser", help="Précise le type de demande ou la variante")
    clarify.add_argument("dossier", type=parse_thread)
    clarify.add_argument("message")
    clarify.add_argument("--demo", action="store_true", help="Utilise des règles sans clé API")

    approval = commands.add_parser("decider", help="Enregistre la décision de la boutique")
    approval.add_argument("dossier", type=parse_thread)
    approval.add_argument("decision", choices=["oui", "non"])
    approval.add_argument("--motif")

    reply = commands.add_parser("repondre", help="Répond en utilisant le lien signé du mail")
    reply.add_argument("lien")
    reply.add_argument("--simuler-timeout", action="store_true")

    for name, help_text in (
        ("reprendre", "Rejoue une exécution interrompue"),
        ("statut", "Affiche l'état sauvegardé"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("dossier", type=parse_thread)

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
        if args.command == "repondre":
            from .web import SIGNED_REPLY

            match = re.fullmatch(SIGNED_REPLY, args.lien)
            args.dossier = shop.request_thread(*match.groups()) if match else None
            if not args.dossier:
                raise ValueError("Lien de réponse invalide.")

        with open_checkpointer(dsn) as checkpointer:
            def interpret(message: str, awaiting_size: bool) -> Intent:
                demo = getattr(args, "demo", None)
                if demo == "retour":
                    return Intent(action="retour", taille=None, coloris=None)
                if demo == "echange":
                    return Intent(action="echange", taille="M", coloris=None)
                if args.command == "preciser" and demo:
                    return interpret_demo(message, awaiting_size)
                return interpret_mistral(message, awaiting_size)

            graph = build_graph(
                checkpointer, shop, interpret,
                simulate_timeout=getattr(args, "simuler_timeout", False),
            )

            if args.command == "ouvrir":
                thread_id = f"{args.client}:{args.commande}:{uuid4()}"
                order_thread(thread_id)
                config = {"configurable": {"thread_id": thread_id}}
                if args.ligne is not None and not 0 < args.ligne <= 2**31 - 1:
                    raise ValueError("Le numéro de ligne est invalide.")
                prepared = shop.order_context(args.client, args.commande)
                if prepared is None:
                    raise ValueError("Commande introuvable pour ce client.")
                context, _ = prepared
                if args.ligne is not None and not any(item["line_number"] == args.ligne for item in context["items"]):
                    raise ValueError("Cet article ne fait pas partie de la commande.")
                status = context["order"]["status"]
                if status != "livree":
                    result = "commande_en_transit" if status == "en_livraison" else "commande_validee"
                    print(json.dumps({"thread_id": thread_id, "status": result}, ensure_ascii=False, indent=2))
                    return 0
                entry = {"messages": [{"type": "human", "content": args.message}]}
                if args.ligne is not None:
                    entry["line_number"] = args.ligne
                graph.invoke(
                    entry,
                    config,
                    context=context,
                    durability="sync",
                )
                snapshot = graph.get_state(config)
            else:
                thread_id = args.dossier
                config = {"configurable": {"thread_id": thread_id}}
                with psycopg.connect(dsn) as lock:
                    lock.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (thread_id,))
                    snapshot = graph.get_state(config)
                    if not snapshot.values:
                        raise ValueError("Dossier SAV introuvable.")

                    if args.command == "article":
                        if snapshot.next != ("choisir_article",) or not snapshot.interrupts:
                            raise ValueError("Ce dossier n’attend pas de choix d’article.")
                        if not any(item["line_number"] == args.ligne for item in snapshot.values["items"]):
                            raise ValueError("Cet article ne fait pas partie de la commande.")
                        graph.invoke(Command(resume={"line_number": args.ligne}), config, durability="sync")
                    elif args.command == "preciser":
                        if snapshot.next not in {("choisir_action",), ("demander_taille",)} or not snapshot.interrupts:
                            raise ValueError("Ce dossier n’attend pas de précision.")
                        context = None
                        if snapshot.next == ("choisir_action",):
                            prepared = shop.order_context(*order_thread(thread_id))
                            if prepared is None:
                                raise ValueError("Commande introuvable pour ce client.")
                            context, _ = prepared
                        graph.invoke(Command(resume={"message": args.message}), config, context=context, durability="sync")
                    elif args.command == "decider":
                        if snapshot.next != ("attendre_validation_boutique",) or not snapshot.interrupts:
                            raise ValueError("Ce dossier n’attend pas la décision de la boutique.")
                        reason = (args.motif or "").strip()
                        if args.decision == "non" and not 0 < len(reason) <= 1000:
                            raise ValueError("Indiquez un motif avec --motif.")
                        graph.invoke(Command(resume={"approved": args.decision == "oui", "reason": reason}), config, durability="sync")
                    elif args.command == "repondre":
                        if snapshot.next != ("attendre_confirmation_client",) or not snapshot.interrupts:
                            raise ValueError("Ce dossier n’attend pas la réponse du client.")
                        graph.invoke(Command(resume=match[2] == "confirmer"), config, durability="sync")
                    elif args.command == "reprendre":
                        if not snapshot.next or snapshot.interrupts:
                            raise ValueError("Ce dossier n'a pas d'exécution à reprendre.")
                        context = None
                        if snapshot.next == ("choisir_action",):
                            context, _ = shop.order_context(*order_thread(thread_id))
                        graph.invoke(None, config, context=context, durability="sync")

                    snapshot = graph.get_state(config)

            output = {"thread_id": thread_id, **snapshot.values}
            if output.get("messages"):
                output["message"] = client_message(snapshot.values)
            output.pop("messages", None)
            if snapshot.interrupts:
                output["interruption"] = {key: val for key, val in snapshot.interrupts[0].value.items() if key != "_message_ids"}
            if output.get("status") == "attente_client":
                for mail in shop.mailbox(output["customer_id"]):
                    if mail.get("links", {}).get("confirmer", "").startswith(f"/sav/reponse/{output['case_id']}/"):
                        output["liens_mail"] = mail["links"]
                        break
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0
    except LostResponse as error:
        print(f"{error} Relance avec : reprendre {thread_id}", file=sys.stderr)
        return 2
    except (ValueError, psycopg.Error) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
