from collections.abc import Callable
from typing import Annotated, NotRequired, TypedDict
from uuid import uuid4

from langchain_core.messages import AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.func import task
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import StringConstraints

from .demo_data import DEMO_CUSTOMER
from .model import Intent
from .shop import OrderArticle, OrderContext, OutOfStock, Shop
from .thread_id import operation_id, order_thread


class LostResponse(TimeoutError):
    pass


def merge_messages(previous, incoming):
    if not isinstance(incoming, list):
        incoming = [incoming]
    messages = [
        {**message, "type": "human"}
        if isinstance(message, dict) and "role" not in message and "type" not in message
        else message
        for message in incoming
    ]
    return add_messages(previous, messages)


class InputState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], merge_messages]
    line_number: int


class State(InputState, total=False):
    case_id: str
    customer_id: str
    order_id: str
    status: str
    action: str
    size: str | None
    color: str | None
    items: list[OrderArticle]
    first_name: str
    reason: str
    message_client: str
    approved: bool
    return_id: str


class MessageReply(TypedDict):
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    message_id: NotRequired[str]


class ShopDecision(TypedDict):
    approved: bool
    reason: NotRequired[str]


class ArticleReply(TypedDict):
    line_number: int


def client_message(state: State) -> str:
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage):
            return str(message.text)
    raise ValueError("Ajoute un message Human pour lancer la demande SAV.")


def ask_message(payload: dict, replies: list[HumanMessage]) -> HumanMessage:
    payload = {**payload, "_message_ids": [reply.id for reply in replies if reply.id]}
    while True:
        reply = interrupt(payload, response_schema=MessageReply)
        if isinstance(reply, dict) and isinstance(reply.get("message"), str):
            message = reply["message"].strip()
            if 0 < len(message) <= 1000:
                return HumanMessage(content=message, id=reply.get("message_id"))


def build_graph(
    checkpointer,
    shop: Shop,
    interpret: Callable[[str, bool], Intent],
    *,
    simulate_timeout: bool = False,
):
    @task
    def read_intent(message: str, awaiting_variant: bool) -> dict:
        return interpret(message, awaiting_variant).model_dump()

    def choose_action(state: State, config: RunnableConfig, runtime: Runtime[OrderContext]) -> State:
        thread_id = str(config.get("configurable", {}).get("thread_id", ""))
        context = runtime.context
        if context is None:
            raise ValueError("Fournissez les détails de la commande dans le contexte.")
        order = context["order"]
        customer_id, order_id = (
            order_thread(thread_id) if ":" in thread_id
            else (context.get("customer_id", DEMO_CUSTOMER), order["id"])
        )
        if (
            order["id"] != order_id
            or context.get("customer_id", customer_id) != customer_id
            or not context["items"]
            or ("line_number" in state and not any(
                item["line_number"] == state["line_number"] for item in context["items"]
            ))
        ):
            raise ValueError("Commande ou article introuvable.")
        if order["status"] != "livree":
            raise ValueError("Le parcours SAV commence après la livraison.")

        message = next((item for item in reversed(state.get("messages", [])) if isinstance(item, HumanMessage)), None)
        replies = []
        while True:
            if message is not None:
                intent = read_intent(str(message.text), False).result()
                if intent["action"] != "incertain":
                    break
            message = ask_message({
                "status": "demande_a_clarifier",
                "question": "Souhaitez-vous échanger ou retourner un article ?",
            }, replies)
            replies.append(message)
        return {
            "case_id": operation_id(thread_id) if ":" in thread_id else state.get("case_id") or str(uuid4()),
            "customer_id": customer_id,
            "order_id": order_id,
            "first_name": context["first_name"],
            "items": context["items"],
            "action": intent["action"],
            "size": intent["taille"] if intent["action"] == "echange" else None,
            "color": intent["coloris"] if intent["action"] == "echange" else None,
            "status": "action_choisie",
            "messages": replies,
        }

    def choose_item(state: State) -> State:
        items = state["items"]
        if state.get("line_number"):
            return {}
        while True:
            reply = interrupt({
                "question": "Quel article souhaitez-vous traiter ?",
                "articles": items,
            }, response_schema=ArticleReply)
            line = reply.get("line_number") if isinstance(reply, dict) else None
            if type(line) is int and any(item["line_number"] == line for item in items):
                return {"line_number": line}

    def choose_variant(state: State) -> State:
        item = shop.order_item(state["customer_id"], state["order_id"], state["line_number"])
        if item is None or item["status"] != "livree":
            raise ValueError("Article livré introuvable.")
        colors = shop.variants(item["product_id"])
        catalog = {name.casefold(): name for name in [item["color"], *colors]}
        size, color = state["size"], state["color"]
        replies = []
        while True:
            status = "taille_a_preciser"
            question = "Quelle taille ou quel coloris souhaitez-vous pour l’échange ?" if item["sized"] else (
                "Quel coloris souhaitez-vous pour l’échange ?"
            )
            requested_color = catalog.get(color.casefold()) if color else None
            if color and not requested_color:
                status, question = "coloris_inconnu", "Ce coloris ne figure pas au catalogue. Quelle variante souhaitez-vous ?"
            elif size and not item["sized"]:
                status, question = "article_sans_taille", "Cet article est proposé en taille unique. Quel coloris souhaitez-vous ?"
                size = None
            elif size or requested_color:
                target_size = size or item["size"]
                target_color = requested_color or item["color"]
                if (target_size, target_color) != (item["size"], item["color"]):
                    return {"size": target_size, "color": target_color, "messages": replies}
                status, question = "taille_identique", "Votre article correspond déjà à cette variante. Choisissez une autre variante."

            message = ask_message({
                "question": question, "status": status, "coloris": colors, "ordered_size": item["size"],
            }, replies)
            replies.append(message)
            intent = read_intent(str(message.text), True).result()
            size = intent["taille"] or (size if status == "coloris_inconnu" and intent["coloris"] else None)
            color = intent["coloris"]

    def check_stock(state: State, config: RunnableConfig) -> State:
        try:
            shop.reserve_exchange(state, config["configurable"]["thread_id"])
        except OutOfStock:
            return {"status": "stock_indisponible"}
        return {"status": "stock_disponible"}

    def offer_return(_state: State) -> State:
        return {"status": "stock_indisponible", "message_client": "Cette variante est en rupture. Souhaitez-vous retourner l’article ?"}

    def register_request(state: State, config: RunnableConfig) -> State:
        shop.register_request(state, config["configurable"]["thread_id"])
        return {"status": "attente_boutique"}

    def notify_shop(state: State) -> State:
        shop.notify_shop(state["case_id"])
        return {"message_client": "Votre demande a été transmise à la boutique."}

    def wait_for_shop(state: State) -> State:
        payload = {"commande": state["order_id"], "action": state["action"], "taille": state["size"],
                   "coloris": state["color"], "ligne": state["line_number"], "dossier": state["case_id"]}
        while True:
            decision = interrupt(payload, response_schema=ShopDecision)
            if isinstance(decision, dict) and type(decision.get("approved")) is bool:
                if decision["approved"]:
                    return {"approved": True}
                reason = decision.get("reason")
                if isinstance(reason, str) and 0 < len(reason.strip()) <= 1000:
                    return {"approved": False, "reason": reason.strip()}
            payload = {**payload, "question": "Indiquez une décision et, en cas de refus, un motif."}

    def send_refusal(state: State) -> State:
        shop.finish_step(state["case_id"], "refuse_boutique", state["reason"])
        return {"status": "refuse_boutique"}

    def send_confirmation(state: State) -> State:
        shop.finish_step(state["case_id"], "attente_client")
        return {"status": "attente_client"}

    def wait_for_client(state: State) -> State:
        decision = interrupt({"dossier": state["case_id"], "canal": "mail"})
        while type(decision) is not bool:
            decision = interrupt({"dossier": state["case_id"], "question": "Confirmer ou refuser ?"})
        return {"approved": decision}

    def cancel_request(state: State) -> State:
        shop.finish_step(state["case_id"], "annule")
        return {"status": "annule"}

    def send_summary(state: State) -> State:
        reference, created = shop.finish_step(state["case_id"], "termine")
        if created and simulate_timeout:
            raise LostResponse("Retour enregistré par la boutique, confirmation perdue.")
        return {"status": "termine", "return_id": reference}

    builder = StateGraph(State, input_schema=InputState, context_schema=OrderContext)
    builder.add_node("choisir_action", choose_action)
    builder.add_node("choisir_article", choose_item)
    builder.add_node("demander_taille", choose_variant)
    builder.add_node("check_stock", check_stock)
    builder.add_node("proposer_retour", offer_return)
    builder.add_node("enregistrer_demande", register_request)
    builder.add_node("notifier_boutique", notify_shop)
    builder.add_node("attendre_validation_boutique", wait_for_shop)
    builder.add_node("envoyer_mail_refus", send_refusal)
    builder.add_node("envoyer_mail_confirmation", send_confirmation)
    builder.add_node("attendre_confirmation_client", wait_for_client)
    builder.add_node("annuler_demande", cancel_request)
    builder.add_node("envoyer_mail_recapitulatif", send_summary)
    builder.add_edge(START, "choisir_action")
    builder.add_edge("choisir_action", "choisir_article")
    builder.add_conditional_edges(
        "choisir_article", lambda state: "échange" if state["action"] == "echange" else "retour",
        {"échange": "demander_taille", "retour": "enregistrer_demande"},
    )
    builder.add_edge("demander_taille", "check_stock")
    builder.add_conditional_edges(
        "check_stock", lambda state: "dispo" if state["status"] == "stock_disponible" else "rupture",
        {"dispo": "enregistrer_demande", "rupture": "proposer_retour"},
    )
    builder.add_edge("enregistrer_demande", "notifier_boutique")
    builder.add_edge("notifier_boutique", "attendre_validation_boutique")
    builder.add_conditional_edges(
        "attendre_validation_boutique", lambda state: "validée" if state["approved"] else "refusée",
        {"validée": "envoyer_mail_confirmation", "refusée": "envoyer_mail_refus"},
    )
    builder.add_edge("envoyer_mail_confirmation", "attendre_confirmation_client")
    builder.add_conditional_edges(
        "attendre_confirmation_client", lambda state: "confirme" if state["approved"] else "refuse",
        {"confirme": "envoyer_mail_recapitulatif", "refuse": "annuler_demande"},
    )
    for node in ("proposer_retour", "envoyer_mail_refus", "annuler_demande", "envoyer_mail_recapitulatif"):
        builder.add_edge(node, END)
    return builder.compile(checkpointer=checkpointer)
