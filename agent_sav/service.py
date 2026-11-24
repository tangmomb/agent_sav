"""Runs the two graphs: chats on one side, cases on the other. The website, the CLI and the evaluation use it."""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

import psycopg
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from .case_graph import build_case_graph
from .cases import Cases
from .conversation import Context, build_conversation_graph
from .database import open_checkpointer, rename_state_key
from .model import Complete, complete
from .shop import Shop

log = logging.getLogger(__name__)
RESUMED = "Un incident avait interrompu votre message précédent, repris ici. Le dernier n’a pas été pris en compte."


class UnknownChat(ValueError):
    pass


class NotExpected(ValueError):
    pass


def message_ids(messages: list[AnyMessage]) -> list[str | None]:
    """The customer message each entry holds, if any: a message, or the « yes » kept with a tool result."""
    return [message.id if isinstance(message, HumanMessage)
            else (message.artifact or {}).get("reply_id") if isinstance(message, ToolMessage) else None
            for message in messages]


def events(messages: list[AnyMessage], taken_back: list[dict], every: list[AnyMessage] | None = None) -> list[dict]:
    """Messages as steps anyone can read: what was said, which tools were called, what they answered."""
    calls = {call["id"]: call for message in every or messages if isinstance(message, AIMessage) for call in message.tool_calls}
    answered = {message.tool_call_id for message in messages if isinstance(message, ToolMessage)}
    told = []
    for message in messages:
        if isinstance(message, HumanMessage):
            told.append({"kind": "customer", "title": "", "text": str(message.text)})
        elif isinstance(message, AIMessage):
            if message.text and not message.tool_calls:
                told.append({"kind": "agent", "title": "", "text": str(message.text)})
            told += [{"kind": "tool", "title": call["name"], "args": call["args"],
                      "text": "Autorisé par les règles. En attente de l’accord du client."}
                     for call in message.tool_calls if call["id"] not in answered]
        else:
            artifact = message.artifact or {}
            if artifact.get("recap"):
                told.append({"kind": "recap", "title": "", "text": artifact["recap"]})
            if artifact.get("reply"):
                told.append({"kind": "customer", "title": "", "text": artifact["reply"]})
            told.append({"kind": "tool", "title": message.name, "args": calls.get(message.tool_call_id, {}).get("args", {}),
                         "text": str(message.text)[:600]})
        told += [{"kind": "taken_back", "title": each["problem"], "text": each["text"]}
                 for each in taken_back if each["after"] == message.id]
    return told


class Support:
    def __init__(self, dsn: str, *, model: Complete = complete,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.dsn = dsn
        self.model = model
        self.shop = Shop(dsn)
        self.cases = Cases(dsn, clock)

    @contextmanager
    def thread(self, thread_id: str, build: Callable) -> Iterator[tuple]:
        """A graph and its config, with the thread locked: one run at a time."""
        config = {"configurable": {"thread_id": thread_id}, "run_name": "SAV", "tags": ["sav"]}
        with open_checkpointer(self.dsn) as checkpointer, psycopg.connect(self.dsn) as lock:
            lock.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (thread_id,))
            rename_state_key(checkpointer.conn, thread_id, "proposals", "pending_actions")  # chats begun under the old name; can go once they are over
            yield build(checkpointer), config

    # Chats.

    def chat(self, chat_id: str, message: str, message_id: str | None = None) -> dict:
        """Handles a customer message once. Sending it again resumes a failed run, or gives the same answer."""
        customer = self.shop.chat_customer(chat_id)
        if customer is None:
            raise UnknownChat("Chat inconnu.")
        context = Context(customer["id"], customer["first_name"])
        message_id = message_id or str(uuid4())
        try:
            with self.thread(chat_id, self.conversation_graph) as (graph, config):
                def run(entry):
                    graph.invoke(entry, config, context=context, durability="sync")

                snapshot = graph.get_state(config)
                late = None
                if snapshot.next and not snapshot.interrupts:  # a step failed last time: go on from there
                    stopped = [each for each in message_ids(snapshot.values["messages"]) if each][-1]
                    run(None)
                    snapshot = graph.get_state(config)
                    if stopped != message_id:
                        late = self.answer(snapshot, stopped)
                        if late["awaiting_confirmation"]:  # a yes must not agree to a summary nobody has seen
                            return {**late, "messages": [RESUMED, *late["messages"]]}
                if message_id not in message_ids(snapshot.values.get("messages", [])):
                    if snapshot.interrupts:
                        run(Command(resume={"message": message, "id": message_id}))
                    else:
                        run({"messages": [HumanMessage(content=message, id=message_id)],
                             "steps": 0, "tokens": 0, "rejected": [], "pending_actions": [], "reply": None})
                snapshot = graph.get_state(config)
        finally:
            self.run_cases()
        answer = self.answer(snapshot, message_id)
        return {**answer, **{key: [*late[key], *answer[key]] for key in ("messages", "steps")}} if late else answer

    def timeline(self, case_id: str) -> list[dict]:
        """The chat that led to a case, step by step, then what happened to the case."""
        case = self.cases.get(case_id)
        chat_id, _, _ = case["request_key"].partition(":")
        told = []
        if chat_id.startswith("chat-"):
            with open_checkpointer(self.dsn) as checkpointer:
                values = self.conversation_graph(checkpointer).get_state({"configurable": {"thread_id": chat_id}}).values
            told = events(values.get("messages", []), values.get("taken_back", []))
        return told + [{"kind": f"case_{each['kind']}", "title": "", "text": each["text"], "at": each["at"]}
                       for each in self.cases.history(case_id)]

    def conversation_graph(self, checkpointer):
        return build_conversation_graph(checkpointer, self.shop, self.cases, self.model)

    @staticmethod
    def answer(snapshot, message_id: str) -> dict:
        messages = snapshot.values["messages"]
        ids = message_ids(messages)
        position = ids.index(message_id) + 1
        following = next((i for i in range(position, len(ids)) if ids[i]), len(ids))
        texts = []
        for message in messages[position:following]:
            if isinstance(message, AIMessage) and message.text and not message.tool_calls:
                texts.append(str(message.text))
            elif isinstance(message, ToolMessage) and (message.artifact or {}).get("recap") and "reply_id" not in message.artifact:
                texts.append(message.artifact["recap"])
        waiting = bool(snapshot.interrupts) and following == len(ids)
        if waiting:
            texts.append(snapshot.interrupts[0].value["question"])
        # A « yes » is kept with the result of the action it agreed to.
        first = position - 1 if isinstance(messages[position - 1], ToolMessage) else position
        return {"messages": texts, "awaiting_confirmation": waiting,
                "steps": [event for event in events(messages[first:following], snapshot.values.get("taken_back", []), messages)
                          if event["kind"] in ("tool", "taken_back")]}

    # Cases.

    def run_cases(self) -> None:
        for case_id in self.cases.to_run():
            try:
                self.run_case(case_id)
            except Exception:  # one case must not hold up the others: the next tick tries again
                log.exception("Dossier %s interrompu", case_id)

    def run_case(self, case_id: str) -> None:
        """Starts the graph of a case if needed, then gives it its pending commands, in order and once each."""
        with self.thread(f"case:{case_id}", lambda checkpointer: build_case_graph(checkpointer, self.cases)) as (graph, config):
            def run(entry):
                graph.invoke(entry, config, durability="sync")
                return graph.get_state(config)

            snapshot = graph.get_state(config)
            if not snapshot.values:
                snapshot = run({"case_id": case_id})
            elif snapshot.next and not snapshot.interrupts:
                snapshot = run(None)
            while command := self.cases.next_command(case_id):
                if command["id"] <= snapshot.values.get("last_command", 0):  # handled, but stopped before noting it
                    self.cases.command_done(command["id"], "applied")
                elif not snapshot.interrupts or command["kind"] not in snapshot.interrupts[0].value["accepts"]:
                    self.cases.command_done(command["id"], "ignored")
                else:
                    snapshot = run(Command(resume=command))
                    self.cases.command_done(command["id"], "applied")

    def act(self, case_id: str, kind: str, payload: dict | None = None) -> dict:
        """What the shop does with a case: approve, reject, answer or received."""
        if self.cases.get(case_id) is None:
            raise NotExpected("Dossier introuvable.")
        command = self.cases.command(case_id, kind, f"shop:{uuid4()}", payload)
        self.run_case(case_id)
        if self.cases.command_outcome(command) == "ignored":
            raise NotExpected("Le dossier n’attend pas cette action.")
        return self.cases.get(case_id)

    def tick(self) -> None:
        """Applies the deadlines that have passed, and resumes what a crash left unfinished."""
        self.cases.add_due_commands()
        self.run_cases()
