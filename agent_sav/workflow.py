from collections.abc import Callable
from typing import Annotated, Literal, Required, TypedDict
from uuid import uuid4

from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.types import interrupt

from .model import Intent
from .shop import DEMO_ORDER, OutOfStock, Shop


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
    messages: Required[Annotated[list[AnyMessage], merge_messages]]
    case_id: str
    order_id: str
    status: str
    action: str
    size: str | None
    approved: bool
    return_id: str


class State(InputState, total=False):
    # Keep the old channel readable when resuming an existing checkpoint.
    message: str


def client_message(state: State) -> str:
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage):
            return str(message.text)
    if not state.get("messages") and "message" in state:
        return state["message"]
    raise ValueError("Ajoute un message Human pour lancer la demande SAV.")


def build_graph(
    checkpointer,
    shop: Shop,
    interpret: Callable[[str], Intent],
    *,
    simulate_timeout: bool = False,
):
    def understand(state: State) -> State:
        intent = interpret(client_message(state))
        return {
            "case_id": state.get("case_id") or str(uuid4()),
            "order_id": state.get("order_id", DEMO_ORDER),
            "action": intent.action,
            "size": intent.taille if intent.action == "echange" else None,
        }

    def check_eligibility(state: State) -> State:
        if state["order_id"] != DEMO_ORDER:
            return {"status": "commande_inconnue"}
        if state["action"] == "incertain":
            return {"status": "demande_a_clarifier"}
        if state["action"] == "echange" and not state["size"]:
            return {"status": "taille_a_preciser"}
        if state["action"] == "echange" and state["size"] == "S":
            return {"status": "taille_identique"}
        return {"status": "accord_requis"}

    def wait_for_approval(state: State) -> State:
        approved = interrupt({
            "commande": state["order_id"],
            "action": state["action"],
            "taille": state["size"],
        }) is True
        return {"approved": approved, "status": "execution" if approved else "annule"}

    def execute(state: State) -> State:
        try:
            return_id, created = shop.create_return(
                state["case_id"], state["order_id"], state["action"], state["size"]
            )
        except OutOfStock:
            return {"status": "stock_indisponible"}

        if created and simulate_timeout:
            raise LostResponse("Retour enregistré par la boutique, confirmation perdue.")

        return {"status": "termine", "return_id": return_id}

    def after_eligibility(state: State) -> Literal["attendre_accord", "__end__"]:
        return "attendre_accord" if state["status"] == "accord_requis" else END

    def after_approval(state: State) -> Literal["executer", "__end__"]:
        return "executer" if state["approved"] else END

    builder = StateGraph(State, input_schema=InputState)
    builder.add_node("comprendre", understand)
    builder.add_node("verifier", check_eligibility)
    builder.add_node("attendre_accord", wait_for_approval)
    builder.add_node("executer", execute)
    builder.add_edge(START, "comprendre")
    builder.add_edge("comprendre", "verifier")
    builder.add_conditional_edges("verifier", after_eligibility)
    builder.add_conditional_edges("attendre_accord", after_approval)
    builder.add_edge("executer", END)
    return builder.compile(checkpointer=checkpointer)
