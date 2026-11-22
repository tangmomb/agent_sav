"""SAV conversation graph.

The agent first finds out what the customer wants, then asks only for what that action needs:

- information: the question for the shop;
- return: the purchased article;
- exchange: the purchased article and the wanted size and/or color, in stock.

    understand ⇄ ask            (one question per turn until the request is complete)
        ├─ information → send_question
        └─ return / exchange → register_request → shop_review
"""

from typing import Annotated, Literal, NotRequired, TypedDict

from langchain_core.messages import AIMessage, HumanMessage
from pydantic import StringConstraints
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt

from .demo_data import SIZES
from .model import understand as read_message
from .shop import OutOfStock, Shop
from .thread_id import customer_of

Action = Literal["information", "return", "exchange"]


class State(MessagesState, total=False):
    action: Action | None
    item_id: str | None  # return and exchange
    size: str | None  # exchange
    color: str | None  # exchange
    question: str | None  # information
    status: str  # where the request stands, once it is complete


# What each pause expects on resume. Studio shows them as forms; LangGraph rejects other values.
class CustomerReply(TypedDict):
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    id: NotRequired[str]  # the chat's message id, so a resent message is not handled twice


class ShopDecision(TypedDict):
    approved: bool
    reason: NotRequired[str]  # required to refuse


class LostResponse(TimeoutError):
    pass


def thread(config: RunnableConfig) -> str:
    """The LangGraph thread also identifies the request in the shop database."""
    return config["configurable"]["thread_id"]


def article(item: dict) -> str:
    variant = " · ".join(value for value in (item["color"], item["size"]) if value)
    return f"{item['name']} ({variant}, commande {item['order_id']})"


def build_graph(checkpointer, shop: Shop, *, simulate_timeout: bool = False):
    def next_question(request: dict, items: list[dict]) -> str | None:
        """Returns what to ask next, or None when the request is complete. May clear invalid values."""
        action = request["action"]
        if action is None:
            return "Bonjour ! Souhaitez-vous poser une question, échanger ou retourner un article ?"
        if action == "information":
            return None if request["question"] else "Quelle est votre question ?"
        if not items:
            request["action"] = None
            return ("Vous n’avez aucun article livré : un échange ou un retour n’est possible qu’après la livraison. "
                    "Avez-vous une question ?")
        item = next((item for item in items if item["item_id"] == request["item_id"]), None)
        if item is None:
            request["item_id"] = None
            options = "\n".join(f"{number}. {article(item)}" for number, item in enumerate(items, 1))
            return f"Quel article est concerné ?\n{options}"
        if action == "return":
            return None

        colors = [item["color"], *shop.variants(item["product_id"])]
        sizes = SIZES if item["size"] else ()
        choices = (f"Tailles : {', '.join(sizes)}. " if sizes else "") + f"Coloris : {', '.join(colors)}."
        color = next((color for color in colors if request["color"] and color.casefold() == request["color"].casefold()), None)
        size = request["size"] if request["size"] in sizes else None
        if not size and not color:
            request.update(size=None, color=None)
            return f"Quelle taille ou quel coloris souhaitez-vous à la place ? {choices}"
        size, color = size or item["size"], color or item["color"]
        request.update(size=size, color=color)
        if (size, color) == (item["size"], item["color"]):
            request.update(size=None, color=None)
            return f"C’est la variante que vous avez déjà. Laquelle souhaitez-vous ? {choices}"
        if not shop.in_stock(item["product_id"], size, color):
            request.update(size=None, color=None)
            return f"Cette variante est en rupture de stock. Choisissez-en une autre ou demandez un retour. {choices}"
        return None

    def understand(state: State, config: RunnableConfig) -> State:
        """Reads the customer's latest message and asks for what is still missing."""
        items = shop.delivered_items(customer_of(thread(config)))
        request = {key: state.get(key) for key in ("action", "item_id", "size", "color", "question")}
        message = state["messages"][-1]
        found = read_message(str(message.text), request, items, [
            {"role": "client" if isinstance(m, HumanMessage) else "sav", "content": str(m.text)}
            for m in state["messages"][-7:-1]
        ])
        if found.get("action") and found["action"] != request["action"]:
            request.update(item_id=None, size=None, color=None, question=None)
        request.update({key: value for key, value in found.items() if value is not None})
        question = next_question(request, items)
        return {**request, "messages": [AIMessage(content=question)] if question else []}

    def ask(state: State) -> State:
        """Waits for the customer's answer to the last question."""
        reply = interrupt({"question": str(state["messages"][-1].text)}, response_schema=CustomerReply)
        return {"messages": [HumanMessage(content=reply["message"], id=reply.get("id"))]}

    def route(state: State) -> str:
        if isinstance(state["messages"][-1], AIMessage):
            return "ask"
        return "send_question" if state["action"] == "information" else "register_request"

    def send_question(state: State, config: RunnableConfig) -> State:
        shop.submit_inquiry(thread(config), customer_of(thread(config)), state["question"])
        return {"status": "question_sent", "messages": [
            AIMessage(content="Votre question a été transmise à la boutique. Vous recevrez la réponse par mail."),
        ]}

    def register_request(state: State, config: RunnableConfig) -> State:
        try:
            shop.register(thread(config), customer_of(thread(config)), state)
        except OutOfStock:
            return {"status": "out_of_stock", "messages": [
                AIMessage(content="Cette variante vient de passer en rupture de stock. Vous pouvez refaire une demande."),
            ]}
        return {"status": "awaiting_shop", "messages": [
            AIMessage(content="Votre demande a été transmise à la boutique. Vous recevrez la suite par mail."),
        ]}

    def shop_review(state: State, config: RunnableConfig) -> State:
        """Waits for the shop's decision. The customer gets the outcome by email."""
        decision = interrupt({"waiting_for": "shop"}, response_schema=ShopDecision)
        if not decision["approved"]:
            shop.finish_step(thread(config), "rejected_by_shop", decision.get("reason", ""))
            return {"status": "rejected_by_shop"}
        created = shop.finish_step(thread(config), "completed")
        if created and simulate_timeout:
            raise LostResponse("Demande validée par la boutique, réponse perdue.")
        return {"status": "completed"}

    # A conversation starts from the customer's message only; the agent fills in the other fields.
    builder = StateGraph(State, input_schema=MessagesState)
    for node in (understand, ask, send_question, register_request, shop_review):
        builder.add_node(node.__name__, node)
    builder.add_edge(START, "understand")
    builder.add_conditional_edges("understand", route, ["ask", "send_question", "register_request"])
    builder.add_edge("ask", "understand")
    builder.add_edge("send_question", END)
    builder.add_conditional_edges(
        "register_request", lambda state: "shop_review" if state["status"] == "awaiting_shop" else END,
        ["shop_review", END],
    )
    builder.add_edge("shop_review", END)
    return builder.compile(checkpointer=checkpointer)
