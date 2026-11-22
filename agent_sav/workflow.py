import re
from collections.abc import Callable
from inspect import getdoc
from typing import Annotated, NotRequired, TypedDict, cast
from uuid import uuid4

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.func import task
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.runtime import Runtime
from langgraph.types import TracePolicy, interrupt
from pydantic import StringConstraints, TypeAdapter

from .demo_data import DEMO_CUSTOMER, SIZES
from .model import ORDER_REFERENCE, CollectionContext, CollectionTurn, article_option, normalize
from .shop import OrderArticle, OrderContext, OutOfStock, Shop
from .thread_id import customer_thread, operation_id

MESSAGE_NODES = {("collect",), ("request_support_question",), ("validate_intent",), ("offer_return",)}


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


class CollectionInput(InputState, total=False):
    case_id: str
    item_id: str | None


class CollectionUpdate(CollectionInput, total=False):
    customer_id: str
    order_id: str | None
    action: str
    size: str | None
    color: str | None
    items: list[OrderArticle]
    first_name: str


class State(CollectionUpdate, total=False):
    status: str
    reason: str
    approved: bool
    return_id: str
    demo_mode: bool


class MessageReply(TypedDict):
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


# Studio uses this form schema. The chat API and CLI validate their own resume payloads.
MESSAGE_REPLY_SCHEMA = TypeAdapter(MessageReply).json_schema()


class ShopDecision(TypedDict):
    approved: bool
    reason: NotRequired[str]


def client_message(state: InputState) -> str:
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage):
            return str(message.text)
    raise ValueError("Ajoute un message Human pour lancer la demande SAV.")


def read_message(reply) -> HumanMessage | None:
    if isinstance(reply, dict) and isinstance(reply.get("message"), str):
        message = reply["message"].strip()
        if 0 < len(message) <= 1000:
            return HumanMessage(content=message, id=reply.get("message_id"))
    return None


def with_reply_ids(payload: dict, replies: list[AnyMessage]) -> dict:
    return {**payload, "_message_ids": [reply.id for reply in replies if isinstance(reply, HumanMessage) and reply.id]}


def ask_message(payload: dict, replies: list[AnyMessage]) -> HumanMessage:
    payload = with_reply_ids(payload, replies)
    while True:
        message = read_message(interrupt(payload, response_schema=MESSAGE_REPLY_SCHEMA))
        if message is not None:
            return message


def collection_update(values: dict) -> CollectionUpdate:
    allowed_keys = CollectionUpdate.__annotations__
    return cast(CollectionUpdate, {key: value for key, value in values.items() if key in allowed_keys})


def trace_state(value):
    if not isinstance(value, dict) or not isinstance(value.get("messages"), list):
        return value
    messages = []
    for message in value["messages"]:
        if isinstance(message, (HumanMessage, AIMessage)):
            entry = {"role": "user" if isinstance(message, HumanMessage) else "assistant", "content": message.content}
            if message.id is not None:
                entry["id"] = message.id
            messages.append(entry)
        else:
            messages.append(message)
    return {**value, "messages": messages}


def clarification_text(prompt: dict, proposed: str | None = None) -> str:
    if prompt.get("no_delivered_orders"):
        return "Aucune commande livrée à traiter. Renseignement à préciser."
    if prompt.get("unavailable_order"):
        return "Cette commande est indisponible pour un échange ou un retour. Commande livrée à préciser."
    if prompt.get("unknown_reference"):
        return "Cette commande ne figure pas sur votre compte. Commande à préciser."
    if proposed is None and prompt.get("ambiguous_reply"):
        return f"Réponse ambiguë. À préciser : {prompt['required_detail']}."
    return proposed or f"À préciser : {prompt['required_detail']}."


def displayed_question(prompt: dict) -> str:
    lines = [prompt["question"]]
    if options := prompt.get("options"):
        lines.extend(["", *(f"{index}. {option}" for index, option in enumerate(options, 1))])
    if ordered_size := prompt.get("ordered_size"):
        sizes = ", ".join(size for size in SIZES if size != ordered_size)
        lines.append(f"Tailles : {sizes}.")
    if colors := prompt.get("colors"):
        lines.append("Coloris : " + ", ".join(colors) + ".")
    return "\n".join(lines)


def collection_route(state: CollectionUpdate) -> str:
    if state.get("action") == "information":
        return "information"
    article = next(
        (item for item in state.get("items", []) if item["item_id"] == state.get("item_id")), None,
    )
    if article and state.get("case_id") and state.get("customer_id") and state.get("order_id"):
        size, color = state.get("size"), state.get("color")
        if state.get("action") == "return" and "size" in state and "color" in state and size is None and color is None:
            return "request_complete"
        if (
            state.get("action") == "exchange" and "size" in state and color
            and (size is None) == (article["size"] is None)
            and (size, color) != (article["size"], article["color"])
        ):
            return "request_complete"
    raise ValueError("La collecte s’est arrêtée avant que la demande soit complète.")


def intention_route(state: State) -> str:
    if state["status"] == "intent_confirmed":
        return state["action"]
    if state["status"] == "intent_needs_correction":
        return "intent_needs_correction"
    return "end"


def build_graph(
    checkpointer,
    shop: Shop,
    collect: Callable[[str, CollectionContext], CollectionTurn],
    *,
    respond: Callable[[dict, str | None], str],
    select: Callable[[str, str, list[str]], int | None],
    simulate_timeout: bool = False,
):
    @task
    def read_turn(message: str, context: CollectionContext) -> dict:
        return collect(message, context).model_dump()

    @task
    def write_reply(event: dict, message: str | None = None) -> str:
        return respond(event, message)

    @task
    def read_orders(customer_id: str) -> list[dict]:
        return shop.orders(customer_id) or []

    @task
    def initial_context(context: OrderContext | None) -> OrderContext | None:
        return context

    @task
    def read_context(customer_id: str, order_id: str) -> OrderContext | None:
        prepared = shop.order_context(customer_id, order_id)
        return prepared[0] if prepared else None

    @task
    def read_variant(customer_id: str, order_id: str, item_id: str) -> tuple[dict, list[str]]:
        item = shop.order_item(customer_id, order_id, item_id)
        if item is None or item["status"] != "delivered":
            raise ValueError("Article livré introuvable.")
        return item, shop.variants(item["product_id"])

    @task
    def read_selection(message: str, question: str, options: list[str]) -> int | None:
        return select(message, question, options)

    def match_option(message: HumanMessage | None, question: str, options: list[str], replies: list[AnyMessage]) -> int | None:
        if message is None:
            return None
        replies.append(message)
        number = read_selection(str(message.text), question, options).result()
        if type(number) is int and 1 <= number <= len(options):
            return number - 1
        return None

    def clarification_prompt(
        draft: CollectionUpdate, delivered: list[dict], *,
        retry_article: bool, unknown_reference: bool, unavailable_order: bool, ambiguous_variant: bool,
    ) -> dict:
        if draft["action"] == "uncertain":
            return {"status": "intent_needed", "required_detail": "l’intention (renseignement, échange ou retour)"}

        order_id = draft.get("order_id")
        if order_id is not None and not draft.get("items"):
            context = read_context(draft["customer_id"], order_id).result()
            if context and context["order"]["status"] == "delivered":
                draft.update(first_name=context["first_name"], items=context["items"])
            else:
                delivered = [order for order in delivered if order["id"] != order_id]
                draft.update(order_id=None, item_id=None, size=None, color=None)
                order_id = None
                unavailable_order = True
        if order_id is None:
            if not delivered:
                return {
                    "status": "intent_needed",
                    "required_detail": "le renseignement recherché",
                    "no_delivered_orders": True,
                }
            return {
                "status": "order_needed",
                "required_detail": "la commande",
                "unknown_reference": unknown_reference,
                "unavailable_order": unavailable_order,
                "action": draft["action"],
                "options": [
                    f"#{order['id']} · {order['ordered_on']} · " + ", ".join(item["name"] for item in order["items"])
                    for order in delivered
                ],
                "orders": [{"id": order["id"], "ordered_on": order["ordered_on"]} for order in delivered],
            }

        items = draft["items"]
        if not any(item["item_id"] == draft.get("item_id") for item in items):
            return {
                "status": "item_needed", "ambiguous_reply": retry_article, "order_id": order_id,
                "required_detail": "l’article de la commande",
                "action": draft["action"], "options": [article_option(item) for item in items], "items": items,
            }

        if draft["action"] == "return":
            draft.update(size=None, color=None)
            return {}

        item, colors = read_variant(draft["customer_id"], order_id, draft["item_id"]).result()
        size, color = draft["size"], draft["color"]
        if ambiguous_variant:
            return {
                "status": "variant_needed", "required_detail": "une seule variante parmi celles mentionnées",
                "uses_size": item["sized"], "colors": colors, "ordered_size": item["size"],
                "order_id": order_id, "item_id": draft["item_id"], "action": draft["action"],
            }
        catalog = {name.casefold(): name for name in [item["color"], *colors]}
        requested_color = catalog.get(color.casefold()) if color else None
        status = "variant_needed"
        if color and not requested_color:
            status = "unknown_color"
        elif size and not item["sized"]:
            status = "item_without_size"
            draft["size"] = None
        elif size or requested_color:
            target_size, target_color = size or item["size"], requested_color or item["color"]
            if (target_size, target_color) != (item["size"], item["color"]):
                draft.update(size=target_size, color=target_color)
                return {}
            status = "same_variant"
        return {
            "status": status, "uses_size": item["sized"], "colors": colors, "ordered_size": item["size"],
            "required_detail": "la taille et/ou le coloris souhaité" if item["sized"] else "le coloris souhaité",
            "order_id": order_id, "item_id": draft["item_id"], "action": draft["action"],
        }

    def collect_order(state: CollectionUpdate, config: RunnableConfig, runtime: Runtime[OrderContext]) -> CollectionUpdate:
        """Identifie l’intention du client et recueille les détails de sa demande SAV.

        Chaque article acheté est identifié par son `item_id`. Le nœud demande les
        précisions manquantes via des interruptions et reprend à chaque réponse du client.

        Conditions de sortie

        - Vers `request_support_question` → la demande est un renseignement (`action` vaut `information`).
        - Vers `validate_intent` → le retour est complet (`action` vaut `return`, `case_id`,
          `customer_id` et `order_id` sont renseignés, `item_id` correspond à un article de `items`,
          et `size` et `color` valent `None`).
        - Vers `validate_intent` → l’échange est complet (`action` vaut `exchange`, les mêmes clés
          identifient le dossier et l’article, et la variante diffère de celle achetée.
          Le coloris (`color`) est renseigné, ainsi que la taille (`size`) si l’article en a une).

        Clés que le nœud peut produire

        - `case_id` (state) identifie le dossier SAV.
        - `customer_id` (state) (fournie par contexte) identifie le client.
        - `order_id` (state) désigne la commande vérifiée.
        - `first_name` (state) contient le prénom du client.
        - `items` (state) contient les articles de la commande.
        - `item_id` (state) identifie l’article acheté sélectionné.
        - `action` (state) contient l’intention du client.
        - `size` (state) contient la taille de remplacement.
        - `color` (state) contient le coloris de remplacement.
        - `next_question` contient la question proposée par le modèle pour formuler l’interruption.
        - `messages` (state) reçoit les questions du SAV et les réponses du client à la sortie du nœud.
        """
        thread_id = str(config.get("configurable", {}).get("thread_id", ""))
        draft = collection_update({key: value for key, value in state.items() if key != "messages"})
        thread_customer, scoped_order = customer_thread(thread_id) if ":" in thread_id else (None, None)
        context = initial_context(runtime.context).result()
        if not draft.get("customer_id"):
            draft.update(
                customer_id=thread_customer or (context or {}).get("customer_id", DEMO_CUSTOMER),
                action="uncertain", size=None, color=None,
                case_id=state.get("case_id") or (operation_id(thread_id) if thread_id else str(uuid4())),
            )
        customer_id = draft["customer_id"]
        if context and (order_context := context.get("order")):
            if context.get("customer_id", customer_id) != customer_id or scoped_order not in {None, order_context["id"]}:
                raise ValueError("Commande ou article introuvable.")
            scoped_order = order_context["id"]

        conversation = list(state.get("messages", []))
        message = next((item for item in reversed(conversation) if isinstance(item, HumanMessage)), None)
        # A checkpoint from the former collection loop may end at an already asked question.
        if draft.get("order_id") and conversation and isinstance(conversation[-1], AIMessage):
            message = None
        replies: list[AnyMessage] = []
        prompt = {}
        selection: dict = {}
        while True:
            orders = read_orders(customer_id).result()
            by_id = {order["id"]: order for order in orders}
            delivered = [order for order in orders if order["status"] == "delivered" and (
                scoped_order is None or order["id"] == scoped_order
            )]
            if scoped_order and scoped_order not in by_id:
                raise ValueError("Commande ou article introuvable.")
            article_orders = {item["item_id"]: order["id"] for order in delivered for item in order["items"]}
            previous_order = draft.get("order_id")
            previous_item = draft.get("item_id")
            if scoped_order and previous_item and article_orders.get(previous_item) != scoped_order:
                raise ValueError("Commande ou article introuvable.")

            turn = {}
            asked_order = selection.get("order_id")
            asked_item = selection.get("item_id")
            if message is not None:
                latest_text = str(message.text)
                turn = read_turn(latest_text, {
                    "customer_orders": [
                        {"id": order["id"], "status": order["status"], "ordered_on": order["ordered_on"], "items": [
                            {key: item[key] for key in ("item_id", "name", "size", "color")} for item in order["items"]
                        ]}
                        for order in delivered
                    ],
                    "draft": {
                        "action": draft["action"], "order_id": previous_order or scoped_order,
                        "item_id": previous_item, "size": draft["size"], "color": draft["color"],
                    },
                    "discussion_thread": [
                        {"role": "user" if isinstance(item, HumanMessage) else "assistant", "content": str(item.text)}
                        for item in [*conversation, *replies]
                        if item is not message and isinstance(item, (HumanMessage, AIMessage))
                    ],
                }).result()
                if turn["action"] is not None:
                    draft["action"] = turn["action"]
                references = {reference.upper() for reference in ORDER_REFERENCE.findall(latest_text)}
                asked_order = next(iter(references)) if len(references) == 1 else turn["order_id"]
                explicit_items = {item_id for item_id in article_orders if re.search(
                    rf"(?<![\w-]){re.escape(item_id)}(?![\w-])", latest_text, re.IGNORECASE,
                )}
                asked_item = next(iter(explicit_items)) if len(explicit_items) == 1 else (
                    None if explicit_items else turn["item_id"]
                )

            if draft["action"] == "information":
                draft.update(size=None, color=None)
                return collection_update({**draft, "messages": replies})

            reference = scoped_order or asked_order or article_orders.get(asked_item) or previous_order
            order = next((order for order in delivered if order["id"] == reference), None)
            unknown_reference = bool(reference not in by_id) if reference else bool(prompt.get("unknown_reference"))
            unavailable_order = order is None and reference in by_id if reference else bool(prompt.get("unavailable_order"))
            if reference is None and len(delivered) == 1 and not (unknown_reference or unavailable_order):
                order = delivered[0]
            order_id = order["id"] if order else None
            if order_id != previous_order:
                draft.update(order_id=order_id, items=[])
                if previous_order:
                    draft.update(item_id=None, size=None, color=None)

            valid_item = asked_item if asked_item and article_orders.get(asked_item) == order_id and order_id else None
            if scoped_order and asked_order not in {None, scoped_order}:
                valid_item = None
            explicit_correction = previous_item and message is not None and re.search(
                r"\b(?:autre (?:article|produit)|pas cet article|trompe d['’ ]article|trompe de produit)\b",
                normalize(str(message.text)),
            )
            if explicit_correction and valid_item == previous_item:
                valid_item = None
            if explicit_correction or (valid_item and previous_item and valid_item != previous_item):
                draft.update(item_id=None, size=None, color=None)
            if valid_item:
                draft["item_id"] = valid_item

            if turn.get("size") and not turn.get("color") and prompt.get("status") == "unknown_color":
                draft["color"] = None
            ambiguous_variant = False
            for field in ("size", "color"):
                values = list(dict.fromkeys(turn.get(field, [])))
                if values:
                    draft[field] = values[0] if len(values) == 1 else None
                    ambiguous_variant |= len(values) > 1

            prompt = clarification_prompt(
                draft, delivered, unknown_reference=unknown_reference, unavailable_order=unavailable_order,
                retry_article=not valid_item and (bool(explicit_correction) or prompt.get("status") == "item_needed"),
                ambiguous_variant=ambiguous_variant,
            )
            if not prompt:
                return collection_update({**draft, "messages": replies})

            prompt["question"] = clarification_text(prompt, turn.get("next_question"))
            replies.append(AIMessage(content=displayed_question(prompt)))
            details = {key: draft[key] for key in ("action", "order_id", "item_id", "size", "color") if key in draft}
            payload = with_reply_ids({**details, **prompt}, replies)
            selection = {}
            message = None
            while not selection and message is None:
                reply = interrupt(payload, response_schema=MESSAGE_REPLY_SCHEMA)
                message = read_message(reply)
                if message is not None:
                    replies.append(message)
                elif isinstance(reply, dict):
                    if prompt["status"] == "order_needed":
                        choice = next((order for order in prompt["orders"] if order["id"] == reply.get("order_id")), None)
                        if choice is not None:
                            selection = {"order_id": choice["id"]}
                            replies.append(HumanMessage(content=choice["id"]))
                    elif prompt["status"] == "item_needed":
                        choice = next((item for item in prompt["items"] if item["item_id"] == reply.get("item_id")), None)
                        if choice is not None:
                            selection = {"item_id": choice["item_id"]}
                            replies.append(HumanMessage(content=article_option(choice)))

    def write_sav_message(state: CollectionUpdate) -> State:
        question = "Que souhaitez vous demander au sav ?"
        reply = ask_message({"status": "question_needed", "question": question}, [])
        shop.submit_inquiry(state["case_id"], state["customer_id"], str(reply.text))
        return {
            "status": "information",
            "messages": [
                AIMessage(content=question), reply,
                AIMessage(content="Votre question a été transmise ! Vous recevrez une réponse par mail"),
            ],
        }

    def validate_intention(state: CollectionUpdate) -> State:
        if state["action"] == "information":
            # Older checkpoints may still be paused in this node.
            return write_sav_message(state)
        article = next(item for item in state["items"] if item["item_id"] == state["item_id"])
        summary = {
            "action": state["action"], "item_id": article["item_id"], "item_name": article["name"], "order_id": state["order_id"],
            "size": state["size"], "color": state["color"],
        }
        replies: list[AnyMessage] = []
        text = write_reply({"status": "intent_confirmation_needed", "request_summary": summary}).result()
        reply = ask_message({"status": "intent_confirmation_needed", "question": text, "request_summary": summary}, replies)
        replies.extend([AIMessage(content=text), reply])
        answer = " ".join(re.findall(r"[\w'’]+", normalize(str(reply.text))))
        if re.fullmatch(r"oui(?: merci| je confirme)?|je confirme|d['’]accord|ok", answer):
            return {"status": "intent_confirmed", "messages": replies}
        if answer in {"annuler", "je veux annuler", "abandonner"}:
            text = write_reply({"status": "request_abandoned"}).result()
            return {"status": "request_abandoned", "messages": [*replies, AIMessage(content=text)]}
        if answer in {"non", "non merci", "a corriger"}:
            text = write_reply({"status": "intent_needs_correction", "required_detail": "la correction à apporter"}).result()
            correction = ask_message({"status": "intent_needs_correction", "question": text}, replies)
            replies.extend([AIMessage(content=text), correction])
        return {"status": "intent_needs_correction", "messages": replies}

    def check_stock(state: State, config: RunnableConfig) -> State:
        # A return scheduled by an older checkpoint can still arrive here.
        if state["action"] == "return":
            return {}
        try:
            shop.reserve_exchange(state, config["configurable"]["thread_id"])
        except OutOfStock:
            return {"status": "out_of_stock"}
        return {"status": "in_stock"}

    def offer_return(state: State) -> State:
        options = ["Oui, retourner l’article", "Non, abandonner la demande"]
        replies: list[AnyMessage] = []
        while True:
            question = write_reply({
                "status": "out_of_stock", "ambiguous_reply": bool(replies),
                "required_detail": "l’accord du client pour retourner l’article ou abandonner la demande",
                "size": state["size"], "color": state["color"],
            }, str(replies[-1].text) if replies else client_message(state)).result()
            reply = ask_message({
                "status": "out_of_stock",
                "question": question,
            }, replies)
            replies.append(AIMessage(content=question))
            index = match_option(reply, question, options, replies)
            if index == 0:
                return {"action": "return", "size": None, "color": None, "messages": replies}
            if index == 1:
                message = write_reply({"status": "request_abandoned"}, str(reply.text)).result()
                return {"status": "request_abandoned", "messages": [*replies, AIMessage(content=message)]}

    def register_request(state: State, config: RunnableConfig) -> State:
        shop.register_request(state, config["configurable"]["thread_id"])
        return {"status": "awaiting_shop"}

    def notify_shop(state: State) -> State:
        message = write_reply({
            "status": "awaiting_shop", "action": state["action"], "request_submitted": True,
        }).result()
        shop.notify_shop(state["case_id"])
        return {"messages": [AIMessage(content=message)]}

    def wait_for_shop(state: State) -> State:
        payload = {"order_id": state["order_id"], "action": state["action"], "size": state["size"],
                   "color": state["color"], "item_id": state["item_id"], "case_id": state["case_id"]}
        while True:
            decision = interrupt(payload, response_schema=ShopDecision)
            if isinstance(decision, dict) and type(decision.get("approved")) is bool:
                if decision["approved"]:
                    return {"approved": True}
                reason = decision.get("reason")
                if isinstance(reason, str) and 0 < len(reason.strip()) <= 1000:
                    return {"approved": False, "reason": reason.strip()}
            payload = {**payload, "invalid_decision": True, "rejection_reason_required": True}

    def send_refusal(state: State) -> State:
        shop.finish_step(state["case_id"], "rejected_by_shop", state["reason"])
        return {"status": "rejected_by_shop"}

    def send_confirmation(state: State) -> State:
        shop.finish_step(state["case_id"], "awaiting_customer")
        return {"status": "awaiting_customer"}

    def wait_for_client(state: State) -> State:
        decision = interrupt({"case_id": state["case_id"], "channel": "email"})
        while type(decision) is not bool:
            decision = interrupt({"case_id": state["case_id"], "channel": "email", "invalid_decision": True})
        return {"approved": decision}

    def cancel_request(state: State) -> State:
        shop.finish_step(state["case_id"], "cancelled")
        return {"status": "cancelled"}

    def send_summary(state: State) -> State:
        reference, created = shop.finish_step(state["case_id"], "completed")
        if created and simulate_timeout:
            raise LostResponse("Retour enregistré par la boutique, confirmation perdue.")
        return {"status": "completed", "return_id": reference}

    builder = StateGraph(State, input_schema=InputState, context_schema=OrderContext)
    trace_policy = TracePolicy(process_inputs=trace_state, process_outputs=trace_state)
    builder.add_node(
        "collect", collect_order, input_schema=CollectionUpdate, trace_policy=trace_policy,
        metadata={"description": getdoc(collect_order)},
    )
    for name, node in (
        ("request_support_question", write_sav_message),
        ("validate_intent", validate_intention),
    ):
        builder.add_node(name, node, input_schema=CollectionUpdate, trace_policy=trace_policy)
    for name, node in (
        ("check_stock", check_stock),
        ("offer_return", offer_return),
        ("register_request", register_request),
        ("notify_shop", notify_shop),
        ("wait_for_shop_approval", wait_for_shop),
        ("send_rejection_email", send_refusal),
        ("send_confirmation_email", send_confirmation),
        ("wait_for_customer_confirmation", wait_for_client),
        ("cancel_request", cancel_request),
        ("send_summary_email", send_summary),
    ):
        builder.add_node(name, node, trace_policy=trace_policy)
    builder.add_edge(START, "collect")
    builder.add_conditional_edges(
        "collect", collection_route,
        {"information": "request_support_question", "request_complete": "validate_intent"},
    )
    builder.add_conditional_edges(
        "validate_intent", intention_route,
        {"exchange": "check_stock", "return": "register_request", "intent_needs_correction": "collect", "end": END},
    )
    builder.add_conditional_edges(
        "check_stock", lambda state: "out_of_stock" if state.get("status") == "out_of_stock" else "available",
        {"available": "register_request", "out_of_stock": "offer_return"},
    )
    builder.add_conditional_edges(
        "offer_return", lambda state: "return" if state["action"] == "return" else "abandon",
        {"return": "register_request", "abandon": END},
    )
    builder.add_edge("register_request", "notify_shop")
    builder.add_edge("notify_shop", "wait_for_shop_approval")
    builder.add_conditional_edges(
        "wait_for_shop_approval", lambda state: "approved" if state["approved"] else "rejected",
        {"approved": "send_confirmation_email", "rejected": "send_rejection_email"},
    )
    builder.add_edge("send_confirmation_email", "wait_for_customer_confirmation")
    builder.add_conditional_edges(
        "wait_for_customer_confirmation", lambda state: "confirmed" if state["approved"] else "declined",
        {"confirmed": "send_summary_email", "declined": "cancel_request"},
    )
    for node in ("request_support_question", "send_rejection_email", "cancel_request", "send_summary_email"):
        builder.add_edge(node, END)
    return builder.compile(checkpointer=checkpointer)
