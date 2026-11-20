from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal, NotRequired, TypedDict
from uuid import uuid4

from langchain_core.messages import AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import StringConstraints

from .model import Intent
from .demo_data import DEMO_CUSTOMER, DEMO_ORDER
from .shop import OrderContext, OrderStatus, OutOfStock, Shop
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
    active_order_count: int


class State(InputState, total=False):
    case_id: str
    customer_id: str
    order_id: str
    order_status: OrderStatus
    has_other_active_orders: bool
    status: str
    action: str
    size: str | None
    approved: bool
    return_id: str
    # Keep the old channel readable when resuming an existing checkpoint.
    message: str
    ordered_size: str | None


@dataclass(frozen=True)
class OrderSubject:
    customer_id: str
    order_id: str
    line_number: int


def order_subject(state: State, config: RunnableConfig) -> OrderSubject:
    thread_id = str(config.get("configurable", {}).get("thread_id", ""))
    if ":" in thread_id:
        customer_id, order_id = order_thread(thread_id)
    else:
        # Studio local and checkpoints created before the composite thread IDs use UUIDs.
        customer_id = state.get("customer_id", DEMO_CUSTOMER)
        order_id = state.get("order_id", DEMO_ORDER)
    return OrderSubject(customer_id, order_id, state.get("line_number", 1))


class SizeReply(TypedDict):
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    message_id: NotRequired[str]


def client_message(state: State) -> str:
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage):
            return str(message.text)
    if not state.get("messages") and "message" in state:
        return state["message"]
    raise ValueError("Ajoute un message Human pour lancer la demande SAV.")


def awaits_size(state: State) -> bool:
    return state.get("action") == "echange" and state.get("status") in {"taille_a_preciser", "taille_identique"}


def build_graph(
    checkpointer,
    shop: Shop,
    interpret: Callable[[str, bool], Intent],
    *,
    simulate_timeout: bool = False,
):
    def verify_order_status(state: State, config: RunnableConfig, runtime: Runtime[OrderContext]) -> State:
        subject = order_subject(state, config)
        thread_id = str(config.get("configurable", {}).get("thread_id", ""))
        context = runtime.context
        if context is None:
            raise ValueError("Fournissez les détails de la commande dans le contexte.")
        order = context["order"]
        if order["id"] != subject.order_id or not any(
            item["line_number"] == subject.line_number for item in context["items"]
        ):
            return {"status": "commande_inconnue"}

        return {
            "case_id": operation_id(thread_id) if ":" in thread_id else state.get("case_id") or str(uuid4()),
            "customer_id": subject.customer_id,
            "order_id": subject.order_id,
            "line_number": subject.line_number,
            "order_status": order["status"],
            "status": state["status"] if awaits_size(state) else "demande_recue",
        }

    def after_order_status(state: State) -> Literal[
        "verifier_autres_commandes", "suivre_livraison", "informer_preparation", "__end__"
    ]:
        if state["status"] == "commande_inconnue":
            return END
        if state["order_status"] == "en_livraison":
            return "suivre_livraison"
        if state["order_status"] == "validee":
            return "informer_preparation"
        if state["order_status"] == "livree":
            return "verifier_autres_commandes"
        return END

    def verify_other_orders(state: State) -> State:
        return {"has_other_active_orders": state["active_order_count"] > 0}

    def after_other_orders(state: State) -> Literal["comprendre", "__end__"]:
        return "comprendre" if state.get("messages") or state.get("message") else END

    def track_delivery(_state: State) -> State:
        return {"status": "commande_en_transit"}

    def inform_preparation(_state: State) -> State:
        return {"status": "commande_validee"}

    def understand(state: State) -> State:
        awaiting_size = awaits_size(state)
        intent = interpret(client_message(state), awaiting_size)
        action = intent.action
        if action == "incertain" and awaiting_size:
            action = state["action"]
        return {
            "case_id": state.get("case_id") or str(uuid4()),
            "action": action,
            "size": intent.taille if action == "echange" else None,
        }

    def check_eligibility(state: State, config: RunnableConfig) -> State:
        context = order_subject(state, config)
        item = shop.order_item(context.customer_id, context.order_id, context.line_number)
        if item is None:
            return {"status": "commande_inconnue"}
        if item["status"] == "validee":
            return {"status": "commande_validee"}
        if item["status"] == "en_livraison":
            return {"status": "commande_en_transit"}
        if state["action"] == "incertain":
            return {"status": "demande_a_clarifier"}
        if state["action"] == "echange":
            if not item["sized"]:
                return {"status": "article_sans_taille"}
            if not state["size"]:
                return {"status": "taille_a_preciser"}
            if state["size"] == item["size"]:
                return {"status": "taille_identique", "ordered_size": item["size"]}
        return {"status": "accord_requis"}

    def ask_size(state: State) -> State:
        question = (
            f"Votre article est déjà en taille {state.get('ordered_size', 'S')}. "
            "Quelle autre taille souhaitez-vous ?"
            if state["status"] == "taille_identique"
            else "Quelle taille souhaitez-vous pour l’échange ?"
        )
        reply = interrupt({"question": question}, response_schema=SizeReply)
        return {"messages": [HumanMessage(content=reply["message"], id=reply.get("message_id"))]}

    def wait_for_approval(state: State, config: RunnableConfig) -> State:
        context = order_subject(state, config)
        approved = interrupt({
            "commande": context.order_id,
            "ligne": context.line_number,
            "action": state["action"],
            "taille": state["size"],
        }) is True
        return {"approved": approved, "status": "execution" if approved else "annule"}

    def execute(state: State, config: RunnableConfig) -> State:
        context = order_subject(state, config)
        try:
            return_id, created = shop.create_return(
                state["case_id"], context.order_id, state["action"], state["size"],
                customer_id=context.customer_id,
                line_number=context.line_number,
            )
        except OutOfStock:
            return {"status": "stock_indisponible"}

        if created and simulate_timeout:
            raise LostResponse("Retour enregistré par la boutique, confirmation perdue.")

        return {"status": "termine", "return_id": return_id}

    def after_eligibility(state: State) -> Literal["demander_taille", "attendre_accord", "__end__"]:
        if awaits_size(state):
            return "demander_taille"
        if state["status"] == "accord_requis":
            return "attendre_accord"
        return END

    def after_approval(state: State) -> Literal["executer", "__end__"]:
        return "executer" if state["approved"] else END

    builder = StateGraph(State, input_schema=InputState, context_schema=OrderContext)
    builder.add_node("verifier_statut_commande", verify_order_status)
    builder.add_node("verifier_autres_commandes", verify_other_orders)
    builder.add_node("suivre_livraison", track_delivery)
    builder.add_node("informer_preparation", inform_preparation)
    builder.add_node("comprendre", understand)
    builder.add_node("verifier", check_eligibility)
    builder.add_node("demander_taille", ask_size)
    builder.add_node("attendre_accord", wait_for_approval)
    builder.add_node("executer", execute)
    builder.add_edge(START, "verifier_statut_commande")
    builder.add_conditional_edges("verifier_statut_commande", after_order_status)
    builder.add_conditional_edges("verifier_autres_commandes", after_other_orders)
    builder.add_edge("suivre_livraison", END)
    builder.add_edge("informer_preparation", END)
    builder.add_edge("comprendre", "verifier")
    builder.add_conditional_edges("verifier", after_eligibility)
    builder.add_edge("demander_taille", "comprendre")
    builder.add_conditional_edges("attendre_accord", after_approval)
    builder.add_edge("executer", END)
    return builder.compile(checkpointer=checkpointer)
