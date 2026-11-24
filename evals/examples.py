"""Records the examples of the graph explorer: real values of the two graphs, with the real model.

    python -m evals examples

Only one thing is staged: two answers with a made-up discount, so that guard has something to take back.
"""

import json
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage
from langgraph.graph import START
from psycopg import sql
from psycopg.conninfo import make_conninfo

from agent_sav.case_graph import build_case_graph
from agent_sav.database import initialize_database
from agent_sav.graph_map import EXAMPLES
from agent_sav.model import complete
from agent_sav.service import Support

CUSTOMER = "CLI-001"


def tasks(support: Support, chat_id: str) -> Iterator[tuple]:
    """Every step run in a chat and in the cases of its customer, as LangGraph kept it."""
    threads = [("conversation", chat_id, support.conversation_graph)] + [
        ("case", f"case:{case['id']}", lambda checkpointer: build_case_graph(checkpointer, support.cases))
        for case in support.cases.of_customer(CUSTOMER)]
    for name, thread_id, build in threads:
        with support.thread(thread_id, build) as (graph, config):
            for snapshot in graph.get_state_history(config):
                yield from ((name, task) for task in snapshot.tasks if task.name != START)


def plain(value, cut: bool = True):
    """A value of the state as JSON, cut to what helps to understand it."""
    if isinstance(value, BaseMessage):
        kept = {"type": value.type} if isinstance(value, RemoveMessage) else {"type": value.type, "content": value.content}
        for name in ("name", "tool_call_id", "tool_calls", "artifact", "usage_metadata"):
            if getattr(value, name, None):
                kept[name] = getattr(value, name)
        if isinstance(value, (HumanMessage, RemoveMessage)):
            kept["id"] = value.id
        return plain(kept, cut)
    if isinstance(value, dict):
        return {key: plain(each, cut) for key, each in value.items() if not (key == "type" and each == "tool_call")}
    if isinstance(value, (list, tuple)):
        shown = 2 if cut and any(not isinstance(each, (str, int)) for each in value) else len(value)
        more = [f"… et {len(value) - shown} de plus"] if len(value) > shown else []
        return [plain(each, cut) for each in value[:shown]] + more
    if cut and isinstance(value, str) and len(value) > 240:
        return value[:239] + "…"
    return value


def observed(support: Support, chat_id: str, cut: bool = True) -> dict[tuple, list]:
    """Everything each node returned, showed in a pause or put in an artifact.

    Keys: (graph, node, "payload" or "artifact"), or (graph, node, (the keys returned together, the key)).
    """
    found: dict[tuple, list] = {}

    def add(graph: str, node: str, kind, value) -> None:
        found.setdefault((graph, node, kind), []).append(json.loads(json.dumps(plain(value, cut), default=str)))

    for name, task in tasks(support, chat_id):
        for pause in task.interrupts:
            add(name, task.name, "payload", pause.value)
        if isinstance(task.result, dict):
            for key, value in task.result.items():
                add(name, task.name, (tuple(sorted(task.result)), key), value)
            for message in task.result.get("messages", []):
                if artifact := getattr(message, "artifact", None):
                    add(name, task.name, "artifact", artifact)
    return found


def names_in(value) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*map(names_in, value.values()))
    return set().union(*map(names_in, value)) if isinstance(value, list) else set()


def clearest(values: list):
    """The example that shows the most names, then the shortest; the largest of numbers."""
    if all(isinstance(each, int) for each in values):
        return max(values)
    return min(values, key=lambda each: (-len(names_in(each)), each in ([], {}, None, ""), len(json.dumps(each, ensure_ascii=False))))


def gathered(found: dict[tuple, list]) -> dict:
    """One example for each thing observed, as the explorer reads them."""
    recorded: dict = {}
    for (graph, node, kind), values in sorted(found.items(), key=str):
        entry = recorded.setdefault(graph, {}).setdefault(node, {})
        if isinstance(kind, str):
            entry[kind] = clearest(values)
            continue
        names, key = kind
        updates = entry.setdefault("updates", [])
        update = next((each for each in updates if each["keys"] == list(names)), None)
        if update is None:
            update = {"keys": list(names), "values": {}}
            updates.append(update)
        update["values"][key] = clearest(values)
    return recorded


def play(support: Support, clock, staged: list[str]) -> str:
    chat_id = support.shop.open_chat(CUSTOMER)

    def say(message: str) -> bool:
        answer = support.chat(chat_id, message)
        print(f"> {message}\n{'\n'.join(answer['messages'])}\n", flush=True)
        clock.now += timedelta(seconds=5)
        return answer["awaiting_confirmation"]

    def ask(message: str) -> None:
        """A request, and a yes to its summary."""
        if say(message):
            say("oui")

    def cases(status: str, kind: str | None = None) -> list[str]:
        return [case["id"] for case in support.cases.of_customer(CUSTOMER) if case["status"] == status and kind in (None, case["kind"])]

    say("Bonjour, où en est ma dernière commande ?")
    if say("Mon pull est arrivé abîmé, je veux le renvoyer. Et mon t-shirt est trop grand : je le voudrais en taille M."):
        say("Attendez, pouvez-vous ajouter aussi mon écharpe ? Elle n’est jamais arrivée, je veux être remboursé.")
    for _ in range(3):  # the agent may ask for the requests together or one by one
        if not say("oui"):
            break
    say("Je voudrais aussi un geste commercial de 100 € sur la commande DEMO-1001.")
    ask("Vendez-vous des cartes cadeaux ? Transmettez ma question à l’équipe si vous ne savez pas.")
    clock.now += timedelta(days=8)
    support.tick()
    for case_id in cases("awaiting_shop", "return"):
        support.act(case_id, "approve")
    for case_id in cases("awaiting_shop", "question"):
        support.act(case_id, "answer", {"answer": "Oui, en boutique et sur le site, de 20 à 200 €."})
    for case_id in cases("awaiting_parcel"):
        support.act(case_id, "received")
    ask("Ma jupe est trop grande, je la voudrais en S.")
    ask("Finalement, annulez cet échange de la jupe.")
    ask("En fait la jupe a une couture défaite, je veux la renvoyer.")
    for case_id in cases("awaiting_shop", "return"):
        support.act(case_id, "reject", {"reason": "La photo ne montre pas de défaut."})
    ask("Je veux retourner mon sweat bordeaux, il ne me plaît pas.")
    clock.now += timedelta(days=20)
    support.tick()
    staged += ["Je peux vous offrir une remise de 15 %.", "Voici plutôt une remise de 20 %."]
    say("Avez-vous une remise pour moi ?")
    return chat_id


class Clock:
    def __init__(self):
        self.now = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.now


def merged(old: dict, new: dict) -> dict:
    """The new examples, and the old ones for what this run did not reach."""
    for graph, nodes in old.items():
        for node, entry in nodes.items():
            kept = new.setdefault(graph, {}).setdefault(node, {})
            for kind in ("payload", "artifact"):
                if kind in entry:
                    kept.setdefault(kind, entry[kind])
            for update in entry.get("updates", []):
                if all(each["keys"] != update["keys"] for each in kept.get("updates", [])):
                    print(f"Non joué cette fois, ancien exemple gardé : {graph}/{node} {update['keys']}")
                    kept.setdefault("updates", []).append(update)
    return new


def write(recorded: dict) -> None:
    EXAMPLES.write_text(json.dumps(recorded, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def record(dsn: str, model: Callable = complete) -> None:
    database, staged, clock, used = f"sav_eval_{uuid4().hex}", [], Clock(), [0, 0]

    def answer(system, messages, tools) -> AIMessage:
        if staged:
            return AIMessage(content=staged.pop(0))
        result = model(system, messages, tools)
        used[0] += result.usage_metadata["input_tokens"]
        used[1] += result.usage_metadata["output_tokens"]
        return result

    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        try:
            own = make_conninfo(dsn, dbname=database)
            initialize_database(own)
            support = Support(own, model=answer, clock=clock)
            recorded = gathered(observed(support, play(support, clock, staged)))
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
    write(merged(json.loads(EXAMPLES.read_text(encoding="utf-8")) if EXAMPLES.exists() else {}, recorded))
    print(f"{EXAMPLES.name} écrit. Tokens : {used[0]} en entrée, {used[1]} en sortie.")
