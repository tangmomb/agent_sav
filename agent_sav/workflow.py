import re
from collections.abc import Callable
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
    line_number: int | None
    initial_line_number: int


class CollectionUpdate(CollectionInput, total=False):
    customer_id: str
    order_id: str | None
    order_reference: str | None
    status: str | None
    action: str
    size: str | None
    color: str | None
    items: list[OrderArticle]
    first_name: str
    customer_message: str | None
    candidates: dict[str, list]
    collection_prompt: dict | None
    scoped_order_id: str | None


class State(CollectionUpdate, total=False):
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


def with_reply_ids(payload: dict, replies: list[HumanMessage]) -> dict:
    return {**payload, "_message_ids": [reply.id for reply in replies if reply.id]}


def ask_message(payload: dict, replies: list[HumanMessage]) -> HumanMessage:
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
    if state.get("collection_prompt"):
        return "continue_collection"

    article = next(
        (item for item in state.get("items", []) if item["line_number"] == state.get("line_number")), None,
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


def intention_route(state: CollectionUpdate) -> str:
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
    def read_variant(customer_id: str, order_id: str, line_number: int) -> tuple[dict, list[str]]:
        item = shop.order_item(customer_id, order_id, line_number)
        if item is None or item["status"] != "delivered":
            raise ValueError("Article livré introuvable.")
        return item, shop.variants(item["product_id"])

    @task
    def read_selection(message: str, question: str, options: list[str]) -> int | None:
        return select(message, question, options)

    def match_option(message: HumanMessage | None, question: str, options: list[str], replies: list[HumanMessage]) -> int | None:
        if message is None:
            return None
        replies.append(message)
        number = read_selection(str(message.text), question, options).result()
        if type(number) is int and 1 <= number <= len(options):
            return number - 1
        return None

    def next_collection_prompt(
        draft: CollectionUpdate, delivered: list[dict], *, retry_article: bool, unavailable_order: bool,
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
                draft.update(order_id=None, line_number=None, size=None, color=None)
                draft["candidates"] = {"line": [], "size": [], "color": []}
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
                "unknown_reference": bool(draft["order_reference"]) and not unavailable_order,
                "unavailable_order": unavailable_order,
                "action": draft["action"],
                "options": [
                    f"#{order['id']} · {order['ordered_on']} · " + ", ".join(item["name"] for item in order["items"])
                    for order in delivered
                ],
                "orders": [{"id": order["id"], "ordered_on": order["ordered_on"]} for order in delivered],
            }

        items = draft["items"]
        if not any(item["line_number"] == draft.get("line_number") for item in items):
            return {
                "status": "item_needed", "ambiguous_reply": retry_article, "order_id": order_id,
                "required_detail": "l’article de la commande",
                "action": draft["action"], "options": [article_option(item) for item in items], "items": items,
            }

        if draft["action"] == "return":
            draft.update(size=None, color=None)
            return {}

        item, colors = read_variant(draft["customer_id"], order_id, draft["line_number"]).result()
        size, color = draft["size"], draft["color"]
        candidates = draft["candidates"]
        if (item["sized"] and size is None and len(candidates["size"]) > 1) or (
            color is None and len(candidates["color"]) > 1
        ):
            return {
                "status": "variant_needed", "required_detail": "une seule variante parmi celles mentionnées",
                "uses_size": item["sized"], "colors": colors, "ordered_size": item["size"],
                "order_id": order_id, "line_number": draft["line_number"], "action": draft["action"],
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
            "order_id": order_id, "line_number": draft["line_number"], "action": draft["action"],
        }

    def collect_order(state: CollectionUpdate, config: RunnableConfig, runtime: Runtime[OrderContext]) -> CollectionUpdate:
        thread_id = str(config.get("configurable", {}).get("thread_id", ""))
        draft = collection_update({key: value for key, value in state.items() if key != "messages"})
        if not draft.get("customer_id"):
            context = initial_context(runtime.context).result()
            customer_id, scoped_order = (
                customer_thread(thread_id) if ":" in thread_id
                else ((context or {}).get("customer_id", DEMO_CUSTOMER), None)
            )
            if context:
                if context.get("customer_id", customer_id) != customer_id or scoped_order not in {None, context["order"]["id"]}:
                    raise ValueError("Commande ou article introuvable.")
                scoped_order = context["order"]["id"]
            draft.update(
                customer_id=customer_id, scoped_order_id=scoped_order, order_reference=scoped_order,
                action="uncertain", size=None, color=None, status=None,
                case_id=operation_id(thread_id) if ":" in thread_id else state.get("case_id") or str(uuid4()),
            )
            if state.get("line_number") is not None:
                draft["initial_line_number"] = state.get("initial_line_number", state["line_number"])

        customer_id, scoped_order = draft["customer_id"], draft["scoped_order_id"]
        orders = read_orders(customer_id).result()
        by_id = {order["id"]: order for order in orders}
        available = [order for order in orders if scoped_order is None or order["id"] == scoped_order]
        delivered = [order for order in available if order["status"] == "delivered"]
        if scoped_order and scoped_order not in by_id:
            raise ValueError("Commande ou article introuvable.")
        if scoped_order and draft.get("line_number") is not None and not any(
            item["line_number"] == draft["line_number"] for item in by_id[scoped_order]["items"]
        ):
            raise ValueError("Commande ou article introuvable.")

        candidates = {key: list(draft.get("candidates", {}).get(key, [])) for key in ("line", "size", "color")}
        draft["candidates"] = candidates
        prompt = draft.pop("collection_prompt", None) or {}
        conversation = state.get("messages", [])
        message = next((item for item in reversed(conversation) if isinstance(item, HumanMessage)), None)
        latest_text = str(message.text) if message is not None else None
        replies: list[AnyMessage] = []
        reply = None
        if prompt:
            if "question" not in prompt:
                prompt = {**prompt, "question": clarification_text(prompt)}
            reply = interrupt(prompt, response_schema=MESSAGE_REPLY_SCHEMA)
            message = read_message(reply)
            if message is not None:
                latest_text = str(message.text)
                replies.append(message)

        pending_order = None
        lines = []
        turn = {}
        if message is not None:
            discussion_thread = [
                {"role": "user" if isinstance(item, HumanMessage) else "assistant", "content": str(item.text)}
                for item in conversation if item is not message and isinstance(item, (HumanMessage, AIMessage))
            ]
            if prompt:
                last_assistant = {"role": "assistant", "content": prompt["question"]}
                if discussion_thread and discussion_thread[-1] == last_assistant:
                    discussion_thread[-1] = {"role": "assistant", "content": displayed_question(prompt)}
                elif not discussion_thread or discussion_thread[-1]["role"] != "assistant":
                    discussion_thread.append({"role": "assistant", "content": displayed_question(prompt)})
            turn = read_turn(latest_text, {
                "orders": [
                    {"id": order["id"], "status": order["status"], "ordered_on": order["ordered_on"], "items": [
                        {key: item[key] for key in ("line_number", "name", "size", "color")} for item in order["items"]
                    ]}
                    for order in delivered
                ],
                "draft": {
                    "action": draft["action"], "order_id": draft.get("order_id") or scoped_order,
                    "line_number": draft.get("line_number"), "size": draft["size"], "color": draft["color"],
                },
                "discussion_thread": discussion_thread,
            }).result()
            if turn["action"] is not None:
                draft["action"] = turn["action"]
            references = {reference.upper() for reference in ORDER_REFERENCE.findall(latest_text)}
            pending_order = next(iter(references)) if len(references) == 1 else turn["order_id"]
            if not scoped_order or pending_order in {None, scoped_order}:
                lines = turn["line"]
        elif isinstance(reply, dict):
            if prompt.get("status") == "order_needed":
                chosen = reply.get("order_id")
                if isinstance(chosen, str) and any(order["id"] == chosen for order in prompt["orders"]):
                    pending_order = chosen
                    replies.append(HumanMessage(content=chosen))
            elif prompt.get("status") == "item_needed":
                line = reply.get("line_number")
                article = next((item for item in prompt["items"] if item["line_number"] == line), None)
                if type(line) is int and article:
                    lines = [line]
                    replies.append(HumanMessage(content=article_option(article)))

        if pending_order:
            draft["order_reference"] = pending_order
        draft.update(collection_prompt=None, status=None, customer_message=None)
        if draft["action"] == "information":
            draft.update(size=None, color=None)
            return collection_update({**draft, "messages": replies})

        reference = scoped_order or draft["order_reference"]
        order = next((entry for entry in delivered if entry["id"] == reference), None)
        unavailable_order = reference in by_id and order is None
        if order is None and reference is None and len(delivered) == 1:
            order = delivered[0]

        previous_order = draft.get("order_id")
        previous_line = draft.get("line_number")
        order_id = order["id"] if order else None
        order_changed = previous_order is not None and previous_order != order_id
        if previous_order != order_id:
            draft.update(order_id=order_id, items=[])
        if order_changed:
            candidates["line"].clear()
        if order is None and reference is None:
            candidates["line"] = list(dict.fromkeys([*candidates["line"], *(line for line in lines if line > 0)]))
        elif order and previous_order is None and not lines:
            lines = candidates["line"]

        explicit_correction = previous_line is not None and message is not None and bool(re.search(
            r"\b(?:autre (?:article|produit)|pas cet article|trompe d['’ ]article|trompe de produit)\b",
            normalize(latest_text),
        ))
        if explicit_correction and not order_changed:
            lines = [line for line in lines if line != previous_line]
        valid_lines = list(dict.fromkeys(
            line for line in lines if order and any(item["line_number"] == line for item in order["items"])
        ))
        candidates["line"] = list(dict.fromkeys([*candidates["line"], *valid_lines]))
        article_changed = previous_line is not None and (
            explicit_correction or (lines and valid_lines != [previous_line])
        )
        if order_changed or article_changed:
            draft.update(line_number=None, size=None, color=None)
            candidates["size"].clear()
            candidates["color"].clear()
        if len(valid_lines) == 1:
            draft["line_number"] = valid_lines[0]

        if turn.get("size") and not turn.get("color") and prompt.get("status") == "unknown_color":
            draft["color"] = None
            candidates["color"].clear()
        for field in ("size", "color"):
            new_values = list(dict.fromkeys(turn.get(field, [])))
            if new_values:
                candidates[field] = list(dict.fromkeys([*candidates[field], *new_values]))
                draft[field] = new_values[0] if len(new_values) == 1 else None

        next_prompt = next_collection_prompt(
            draft, delivered,
            unavailable_order=unavailable_order,
            retry_article=bool(not valid_lines and message is not None and (
                article_changed or prompt.get("status") == "item_needed"
            )),
        )
        if next_prompt:
            next_prompt["question"] = clarification_text(next_prompt, turn.get("next_question"))
            replies.append(AIMessage(content=displayed_question(next_prompt)))
            draft.update(collection_prompt=next_prompt, status=next_prompt["status"])
        return collection_update({**draft, "messages": replies})

    def write_sav_message(state: CollectionUpdate) -> CollectionUpdate:
        question = ask_message({
            "status": "question_needed",
            "question": "Que souhaitez vous demander au sav ?",
        }, [])
        shop.submit_inquiry(state["case_id"], state["customer_id"], str(question.text))
        return {
            "status": "information",
            "customer_message": "Votre question a été transmise ! Vous recevrez une réponse par mail",
            "messages": [question],
        }

    def validate_intention(state: CollectionUpdate) -> CollectionUpdate:
        if state["action"] == "information":
            # Older checkpoints may still be paused in this node.
            return write_sav_message(state)
        article = next(item for item in state["items"] if item["line_number"] == state["line_number"])
        summary = {
            "action": state["action"], "item_name": article["name"], "order_id": state["order_id"],
            "size": state["size"], "color": state["color"],
        }
        replies = []
        text = write_reply({"status": "intent_confirmation_needed", "request_summary": summary}, client_message(state)).result()
        reply = ask_message({"status": "intent_confirmation_needed", "question": text, "request_summary": summary}, replies)
        replies.append(reply)
        answer = " ".join(re.findall(r"[\w'’]+", normalize(str(reply.text))))
        if re.fullmatch(r"oui(?: merci| je confirme)?|je confirme|d['’]accord|ok", answer):
            return {"status": "intent_confirmed", "messages": replies}
        if answer in {"annuler", "je veux annuler", "abandonner"}:
            return {"status": "request_abandoned", "customer_message": write_reply({"status": "request_abandoned"}).result(),
                    "messages": replies}
        if answer in {"non", "non merci", "a corriger"}:
            text = write_reply({"status": "intent_needs_correction", "required_detail": "la correction à apporter"}).result()
            replies.append(ask_message({"status": "intent_needs_correction", "question": text}, replies))
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
        replies = []
        while True:
            question = write_reply({
                "status": "out_of_stock", "ambiguous_reply": bool(replies),
                "required_detail": "l’accord du client pour retourner l’article ou abandonner la demande",
                "size": state["size"], "color": state["color"],
            }, str(replies[-1].text) if replies else client_message(state)).result()
            reply = ask_message({
                "status": "out_of_stock",
                "customer_message": question,
            }, replies)
            index = match_option(reply, question, options, replies)
            if index == 0:
                return {"action": "return", "size": None, "color": None, "messages": replies}
            if index == 1:
                message = write_reply({"status": "request_abandoned"}, str(reply.text)).result()
                return {"status": "request_abandoned", "customer_message": message, "messages": replies}

    def register_request(state: State, config: RunnableConfig) -> State:
        shop.register_request(state, config["configurable"]["thread_id"])
        return {"status": "awaiting_shop"}

    def notify_shop(state: State) -> State:
        message = write_reply({
            "status": "awaiting_shop", "action": state["action"], "request_submitted": True,
        }).result()
        shop.notify_shop(state["case_id"])
        return {"customer_message": message}

    def wait_for_shop(state: State) -> State:
        payload = {"order_id": state["order_id"], "action": state["action"], "size": state["size"],
                   "color": state["color"], "line_number": state["line_number"], "case_id": state["case_id"]}
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
    for name, node in (
        ("collect", collect_order),
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
        {"continue_collection": "collect", "information": "request_support_question", "request_complete": "validate_intent"},
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
