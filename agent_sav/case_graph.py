"""Graph of one support case. Its thread is the case, so it can wait for days without blocking the chat.

    intake ├─ auto → approve
           └─ shop → submit → shop_review ├─ approve
                                          ├─ reject
                                          ├─ answer   (question)
                                          └─ cancel
    approve ├─ refund → settle
            └─ return / exchange → await_parcel ├─ received → settle
                                                ├─ remind → await_parcel
                                                ├─ expire
                                                └─ cancel

Pauses are resumed with a command (see Cases.command). Nothing is written before an interrupt in the same
node, and each other node does a single write that can be replayed.
"""

from typing import Literal, NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy, interrupt
from psycopg import OperationalError

from .cases import Cases
from .policy import PARCEL_EXPIRY_DAYS, euros


class CaseInput(TypedDict):
    case_id: str


class CaseState(CaseInput, total=False):
    command: dict  # the one that resumed the last pause
    last_command: int


# What each pause expects on resume. Studio shows them as forms; LangGraph rejects other values.
class ShopCommand(TypedDict):
    kind: Literal["approve", "reject", "answer", "cancel"]
    payload: NotRequired[dict]  # reason to reject, text to answer
    id: NotRequired[int]


class ParcelCommand(TypedDict):
    kind: Literal["received", "remind", "expire", "cancel"]
    payload: NotRequired[dict]
    id: NotRequired[int]


def build_case_graph(checkpointer, cases: Cases):
    def case_of(state: CaseState) -> dict:
        return cases.get(state["case_id"])

    def mail(title: str, case: dict, *lines: str) -> str:
        return "\n".join((title, cases.describe(case), *lines))

    def resumed(command: dict) -> CaseState:
        return {"command": command, "last_command": command.get("id", 0)}

    def intake(state: CaseState) -> CaseState:
        if case_of(state) is None:
            raise ValueError(f"Dossier {state['case_id']} introuvable.")
        return {"last_command": 0}

    def route_intake(state: CaseState) -> str:
        return "approve" if case_of(state)["route"] == "auto" else "submit"

    def submit(state: CaseState) -> CaseState:
        cases.advance(state["case_id"], ("opened",), "awaiting_shop")
        return {}

    def shop_review(state: CaseState) -> CaseState:
        """Waits for the shop's decision, or its answer to a question."""
        question = case_of(state)["kind"] == "question"
        accepts = ["answer"] if question else ["approve", "reject", "cancel"]
        return resumed(interrupt({"waiting_for": "shop", "accepts": accepts}, response_schema=ShopCommand))

    def approve(state: CaseState) -> CaseState:
        case = case_of(state)
        if case["kind"] != "refund":
            cases.advance(case["id"], ("opened", "awaiting_shop"), "awaiting_parcel", mail(
                "Demande SAV acceptée", case,
                f"Vous avez {PARCEL_EXPIRY_DAYS} jours pour renvoyer l’article avec l’étiquette jointe.",
            ), effect=cases.wait_for_parcel)
        return {}

    def await_parcel(state: CaseState) -> CaseState:
        """Waits for the article to come back. Deadlines arrive as commands."""
        accepts = ["received", "remind", "expire", "cancel"]
        return resumed(interrupt({"waiting_for": "parcel", "accepts": accepts}, response_schema=ParcelCommand))

    def remind(state: CaseState) -> CaseState:
        case = case_of(state)
        cases.remind(case["id"], mail("Rappel : votre colis est attendu", case,
                                      "Sans renvoi de votre part, le dossier sera clos."))
        return {}

    def settle(state: CaseState) -> CaseState:
        case = case_of(state)
        done = ("Le nouvel article est expédié." if case["kind"] == "exchange"
                else f"Remboursement : {euros(case['amount_cents'])}.")
        title = "Demande SAV acceptée" if case["kind"] == "refund" else "Colis reçu"
        cases.advance(case["id"], ("opened", "awaiting_shop", "awaiting_parcel"), "completed",
                      mail(title, case, done), effect=cases.settle)
        return {}

    def reject(state: CaseState) -> CaseState:
        case = case_of(state)
        reason = state["command"].get("payload", {}).get("reason", "")
        cases.advance(case["id"], ("awaiting_shop",), "rejected", mail("Demande SAV refusée", case, f"Motif : {reason}"),
                      sender="shop", effect=cases.release_stock)
        return {}

    def answer(state: CaseState) -> CaseState:
        case = case_of(state)
        text = state["command"].get("payload", {}).get("answer", "")
        cases.advance(case["id"], ("awaiting_shop",), "completed", f"Réponse de la boutique\n{text}", sender="shop")
        return {}

    def cancel(state: CaseState) -> CaseState:
        case = case_of(state)
        cases.advance(case["id"], ("awaiting_shop", "awaiting_parcel"), "cancelled",
                      mail("Dossier annulé à votre demande", case), effect=cases.release_stock)
        return {}

    def expire(state: CaseState) -> CaseState:
        case = case_of(state)
        cases.advance(case["id"], ("awaiting_parcel",), "expired",
                      mail("Dossier clos : colis non reçu", case), effect=cases.release_stock)
        return {}

    def route_command(state: CaseState) -> str:
        return {"received": "settle"}.get(state["command"]["kind"], state["command"]["kind"])

    database = RetryPolicy(retry_on=OperationalError)
    builder = StateGraph(CaseState, input_schema=CaseInput)
    for node in (intake, submit, shop_review, approve, await_parcel, remind, settle, reject, answer, cancel, expire):
        builder.add_node(node.__name__, node, retry_policy=database)
    builder.add_edge(START, "intake")
    builder.add_conditional_edges("intake", route_intake, ["approve", "submit"])
    builder.add_edge("submit", "shop_review")
    builder.add_conditional_edges("shop_review", route_command, ["approve", "reject", "answer", "cancel"])
    builder.add_conditional_edges(
        "approve", lambda state: "settle" if case_of(state)["kind"] == "refund" else "await_parcel",
        ["settle", "await_parcel"],
    )
    builder.add_conditional_edges("await_parcel", route_command, ["settle", "remind", "expire", "cancel"])
    builder.add_edge("remind", "await_parcel")
    for last in ("settle", "reject", "answer", "cancel", "expire"):
        builder.add_edge(last, END)
    return builder.compile(checkpointer=checkpointer)
