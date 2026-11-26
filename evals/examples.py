"""Records the examples of the graph explorer: real values of the two graphs, with the real model.

    python -m evals examples

Only one thing is staged: two answers with a made-up discount, so that guard has something to take back.
"""

import json
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from functools import partial
from unittest.mock import patch
from uuid import uuid4

import httpx2
import psycopg
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.graph import START
from langsmith import tracing_context
from mistralai.client import Mistral
from mistralai.client.errors import SDKError
from psycopg import sql
from psycopg.conninfo import make_conninfo

from agent_sav import model as language_model
from agent_sav.case_graph import build_case_graph
from agent_sav.database import initialize_database
from agent_sav.graph_map import EXAMPLES, filled_keys, graph_map
from agent_sav.graph_notes import CASES
from agent_sav.model import complete
from agent_sav.service import Support

CUSTOMER = "CLI-001"


def tasks(support: Support, chat_id: str) -> Iterator[tuple]:
    """Every step run in a chat and in the cases of its customer, as LangGraph kept it, with the state it was given."""
    threads = [("conversation", chat_id, support.conversation_graph)] + [
        ("case", f"case:{case['id']}", lambda checkpointer: build_case_graph(checkpointer, support.cases))
        for case in support.cases.of_customer(CUSTOMER)]
    for name, thread_id, build in threads:
        with support.thread(thread_id, build) as (graph, config):
            for snapshot in graph.get_state_history(config):
                yield from ((name, task, snapshot.values) for task in snapshot.tasks if task.name != START)


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


def recent(messages: list[BaseMessage], cut: bool) -> list:
    """A thread a node reads: the end is what it looks at first."""
    older = len(messages) - 3 if cut else 0
    return ([f"… et {older} avant"] if older > 0 else []) + [plain(each, cut) for each in messages[max(older, 0):]]


def observed(support: Support, chat_id: str, cut: bool = True) -> dict[tuple, list]:
    """Everything each node read in the state, returned, showed in a pause or put in an artifact.

    Keys: (graph, node, "read", "payload" or "artifact"), or (graph, node, (the keys returned together, the key)).
    And (graph, node, "runs"): all this for each run of the node, as it happened.
    """
    found: dict[tuple, list] = {}
    reads = {(graph["id"], node["id"]): node.get("reads", []) for graph in graph_map()["graphs"] for node in graph["nodes"]}

    def shown(value):
        return json.loads(json.dumps(plain(value, cut), default=str))

    def add(graph: str, node: str, kind, value) -> None:
        found.setdefault((graph, node, kind), []).append(value)

    for name, task, state in tasks(support, chat_id):
        given = {key: recent(state[key], cut) if key == "messages" else plain(state[key], cut)
                 for key in reads[name, task.name] if key in state}
        run = {}
        if given:
            run["read"] = json.loads(json.dumps(given, default=str))
        for pause in task.interrupts:
            run["payload"] = shown(pause.value)
        for kind, value in run.items():
            add(name, task.name, kind, value)
        if isinstance(task.result, dict):
            run["values"] = {key: shown(value) for key, value in task.result.items()}
            for key, value in run["values"].items():
                add(name, task.name, (tuple(sorted(task.result)), key), value)
            for message in task.result.get("messages", []):
                if artifact := getattr(message, "artifact", None):
                    add(name, task.name, "artifact", shown(artifact))
        add(name, task.name, "runs", run)
    return found


def posted(system: str, messages: list[AnyMessage], tools: list[dict]) -> dict:
    """What the site posts to the API for this call, to the letter, without posting it."""
    bodies = []

    def keep(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(400, json={})

    with httpx2.Client(transport=httpx2.MockTransport(keep)) as client, tracing_context(enabled=False), \
            patch.object(language_model, "Mistral", partial(Mistral, client=client)):
        try:
            complete(system, messages, tools)
        except SDKError:
            pass
    return bodies[0]


def exchange(calls: list[tuple]) -> list[dict] | None:
    """The messages of the first call that follows a whole exchange, with a tool. The instructions are shown elsewhere."""
    call = next((each for each in calls if isinstance(each[1][-1], HumanMessage)
                 and any(isinstance(message, ToolMessage) for message in each[1])), None)
    return call and posted(*call)["messages"][1:]


def thread(messages: list[AnyMessage]) -> list[dict] | None:
    """The same exchange as the state keeps it, up to the answer the agent has just added."""
    asked = next((index for index, message in enumerate(messages) if isinstance(message, HumanMessage)
                  and any(isinstance(before, ToolMessage) for before in messages[:index])), None)
    return None if asked is None else json.loads(json.dumps([plain(each) for each in messages[:asked + 2]], default=str))


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
    """The examples of each node, as the explorer reads them. Those of one case all come from the same run."""
    recorded: dict = {}
    returns = {(graph["id"], node["id"]): [tuple(sorted(key["key"] for key in update["keys"])) for update in node.get("updates", [])]
               for graph in graph_map()["graphs"] for node in graph["nodes"]}
    for (graph, node, kind), runs in sorted(found.items(), key=str):
        if kind != "runs":
            continue
        cases = list(CASES[graph].get(node, {}))
        groups: dict[tuple, list] = {}
        for run in runs:
            if values := run.get("values"):
                groups.setdefault((tuple(sorted(values)), cases.index(filled_keys(values)) if cases else 0), []).append(run)
        order = sorted(groups, key=lambda each: (returns[graph, node].index(each[0]), each[1]))
        chosen = {each: clearest(groups[each]) for each in order}
        main = chosen[order[0]] if order else clearest(runs)
        entry = recorded.setdefault(graph, {}).setdefault(node, {})
        for part in ("read", "payload"):
            if any(part in run for run in runs):
                entry[part] = main[part] if part in main else clearest([run[part] for run in runs if part in run])
        if artifacts := found.get((graph, node, "artifact")):
            entry["artifact"] = clearest(artifacts)
        for names, case in order:
            updates = entry.setdefault("updates", [])
            values = chosen[names, case]["values"]
            known = next((each for each in updates if each["keys"] == list(names)), None)
            if known is None:
                updates.append({"keys": list(names), "values": values})
            else:
                known.setdefault("more", []).append(values)
    return recorded


def play(support: Support, clock, staged: list[str]) -> str:
    chat_id = support.chats.open(CUSTOMER)

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
            for kind in ("read", "payload", "artifact", "sent", "thread"):
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
    calls: list[tuple] = []

    def answer(system, messages, tools) -> AIMessage:
        calls.append((system, messages, tools))
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
            chat_id = play(support, clock, staged)
            recorded = gathered(observed(support, chat_id))
            if sent := exchange(calls):
                recorded["conversation"]["agent"]["sent"] = sent
            with support.thread(chat_id, support.conversation_graph) as (graph, config):
                if kept := thread(graph.get_state(config).values["messages"]):
                    entry = recorded["conversation"]["agent"]
                    entry["thread"] = kept
                    then = next(each.values for each in graph.get_state_history(config) if len(each.values.get("messages", [])) == len(kept))
                    entry["updates"][0]["values"] |= {key: then[key] for key in ("steps", "tokens")}  # of the same moment
                    before = next(each.values for each in graph.get_state_history(config)
                                  if len(each.values.get("messages", [])) == len(kept) - 1)
                    entry["read"] = {key: recent(before[key], True) if key == "messages" else plain(before[key]) for key in entry["read"]}
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
    write(merged(json.loads(EXAMPLES.read_text(encoding="utf-8")) if EXAMPLES.exists() else {}, recorded))
    print(f"{EXAMPLES.name} écrit. Tokens : {used[0]} en entrée, {used[1]} en sortie.")
