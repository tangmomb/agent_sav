"""The graph explorer must show the graphs as they are: checked against the code, and against what really runs."""

import inspect
import json
import shutil

import psycopg
import pytest
from conftest import PULL, TSHIRT, Chat, propose, refund
from langchain_core.messages import AIMessage
from langgraph.config import get_config
from langgraph.graph import END, START

from agent_sav import graph_notes
from agent_sav.graph_map import EXAMPLES, Source, graph_map, kept, parse_sql, updates
from agent_sav.graph_watch import ROOT, Unreadable, Watch
from agent_sav.studio import case, conversation
from evals.examples import gathered, merged, observed, tasks


@pytest.fixture(scope="module")
def graphs():
    return {graph["id"]: graph for graph in json.loads(json.dumps(graph_map()))["graphs"]}


def nodes(graph):
    return {node["id"]: node for node in graph["nodes"]}


@pytest.mark.parametrize(("name", "compiled"), [("conversation", conversation), ("case", case)])
def test_the_map_has_the_nodes_and_exits_of_the_graph(graphs, name, compiled):
    shown = graphs[name]
    edges = {(edge.source, edge.target) for edge in compiled.get_graph().edges}
    assert {(node["id"], leaving["target"]) for node in shown["nodes"] for leaving in node["exits"]} == edges
    assert set(nodes(shown)) == {START, END, *compiled.builder.nodes}
    assert {key["name"] for key in shown["schema"]} == set(compiled.get_output_jsonschema()["properties"])


@pytest.mark.parametrize("name", ["conversation", "case"])
def test_notes_cover_the_graph_and_nothing_else(graphs, name):
    shown = graphs[name]
    edges = {(node["id"], leaving["target"]) for node in shown["nodes"] for leaving in node["exits"]}
    assert set(graph_notes.ROLES[name]) == set(nodes(shown)) - {START, END}
    assert set(graph_notes.EXITS[name]) == set(graph_notes.ROUTES[name]) == edges
    assert set(graph_notes.KEYS[name]) == {key["name"] for key in shown["schema"]}
    assert set(graph_notes.POSITIONS[name]) == set(nodes(shown))


def test_bridges_name_real_things(graphs):
    tables = {table["name"] for table in graph_map()["tables"]}
    for bridge in graph_map()["bridges"]:
        assert bridge["from"] in nodes(graphs[bridge["from_graph"]]) and bridge["to"] in nodes(graphs[bridge["to_graph"]])
        assert bridge["table"] in tables


def test_exceptions_hide_something(graphs):
    """An exception no longer needed would hide a real statement one day."""
    from agent_sav.graph_map import functions, statements

    for node, operation in graph_notes.IMPOSSIBLE:
        source = next(each for each in functions().values() if each.name.startswith("build_") and each.node.name == node)
        assert operation in {f"{each.operation} shop.{table}" for each in statements(source) for table in each.writes}


def test_the_prompt_shown_is_the_one_sent(graphs, database, clock):
    """Against two real runs of the node: an ordinary answer, and one after guard took an answer back."""
    from agent_sav.service import Support

    sent, answers = [], ["Voici une remise de 15 %.", "Bonjour !"]

    def model(system, messages, tools):
        sent.append((system, tools))
        return AIMessage(content=answers.pop(0))

    chat = Chat(Support(database, model=model, clock=clock))
    chat.say("Bonjour")
    shown = nodes(graphs["conversation"])["agent"]["model"]
    ordinary = shown["instructions"].format(first_name="Camille", today=f"{clock():%Y-%m-%d}")
    assert sent[0][0] == ordinary
    [addition] = shown["additions"]
    start, end = addition["text"].split("{rejected[-1]}")
    assert sent[1][0].startswith(ordinary + start) and sent[1][0].endswith(end)
    assert "rejected" in addition["when"][0]
    assert [each["sent"] for each in shown["tools"]] == sent[0][1]


def test_the_options_shown_are_those_of_the_call(graphs, monkeypatch):
    from agent_sav import model

    given = {}

    class Client:
        def __init__(self, api_key, **options):
            given.update(options)
            self.chat = self

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def complete(self, **options):
            given.update(options)
            raise ConnectionError

    monkeypatch.setenv("MISTRAL_API_KEY", "none")
    monkeypatch.setattr(model, "Mistral", Client)
    with pytest.raises(ConnectionError):
        model.complete("", [], [{"type": "function"}])
    shown = {each["name"]: each["value"] for each in nodes(graphs["conversation"])["agent"]["model"]["settings"]}
    assert shown.pop("DEADLINE") == f"{model.DEADLINE} s"
    assert shown == {name: repr(value) for name, value in given.items() if name not in ("model", "messages", "tools")}


def test_nothing_is_returned_outside_the_schema(graphs):
    assert [(node["id"], key["key"]) for graph in graphs.values() for node in graph["nodes"]
            for update in node.get("updates", []) for key in update["keys"] if not key["declared"]] == []


def test_a_key_outside_the_schema_would_show():
    import ast

    node = ast.parse("def node(state):\n    if state['a']:\n        return {'a': 1, 'forgotten': {'x': 2}}\n    return {}").body[0]
    [first, second] = updates(Source("cases", "node", node), {"a"})
    assert [(key["key"], key["declared"], key["inner"]) for key in first["keys"]] == [("a", True, []), ("forgotten", False, ["x"])]
    assert first["when"] == ["state['a']"] and second == {"when": ["not state['a']"], "keys": []}


def test_some_contracts(graphs):
    chat, dossier = nodes(graphs["conversation"]), nodes(graphs["case"])
    assert chat["agent"]["model"] and not any(node.get("model") for name, node in {**chat, **dossier}.items() if name != "agent")
    assert chat["agent"]["sql"] == chat["guard"]["sql"] == chat["confirm"]["sql"] == []
    assert chat["confirm"]["pause"]["schema"] == "CustomerReply"
    assert chat["agent"]["retry"] == {"attempts": 3, "on": "is_transient"}
    [accepted, declined] = chat["confirm"]["updates"]
    assert "reply" in [key["key"] for key in accepted["keys"]] and "messages" in [key["key"] for key in declined["keys"]]
    writes = {name: {table for each in node["sql"] for table in each["writes"]} for name, node in dossier.items() if "sql" in node}
    assert writes["await_parcel"] == writes["shop_review"] == writes["intake"] == set()
    assert writes["settle"] == {"cases", "case_messages", "stock", "order_items", "refunds"}
    assert writes["answer"] == {"cases", "case_messages"} and writes["submit"] == {"cases"}
    [change] = dossier["reject"]["transitions"]
    assert (change["from"], change["to"], change["mails"], change["sender"]) == (
        ["awaiting_shop"], "rejected", ["Demande SAV refusée"], "shop")
    assert dossier["intake"]["router"]["sql"], "the exit of intake depends on the database"


@pytest.mark.parametrize(("sql", "expected"), [
    ("INSERT INTO shop.refunds (case_id, amount_cents) VALUES (%s, %s)", ("INSERT", ["refunds"], [], ["case_id", "amount_cents"])),
    ("UPDATE shop.cases SET status = %s, due_at = NULL WHERE id = %s", ("UPDATE", ["cases"], [], ["status", "due_at"])),
    ("SELECT 1 FROM shop.orders o JOIN shop.order_items i ON true", ("SELECT", [], ["orders", "order_items"], [])),
])
def test_parse_sql(sql, expected):
    assert parse_sql(sql) == expected


def test_conditions_are_followed_and_simplified():
    import ast

    def condition(code, expected=True):
        return ast.parse(code, mode="eval").body, expected

    assert kept((condition("action == 'a'"),), {"action": "b"}) is None
    assert kept((condition("action == 'a'"),), {"action": "a"}) == []
    assert kept((condition("x is None", False),), {}) == ["x is not None"]
    assert kept((condition("case['status'] == after"),), {"after": "done"}) == ["case['status'] == 'done'"]


# Against what really runs.

@pytest.fixture
def played(support, model, clock, query, monkeypatch):
    """Plays many situations, and notes the statements and the keys of each node."""
    executed, real = set(), psycopg.Connection.execute

    def execute(self, sql, *args, **options):
        if isinstance(sql, str) and "shop." in sql:
            try:
                config = get_config()
            except RuntimeError:
                config = {}
            if node := config.get("metadata", {}).get("langgraph_node"):
                graph = "case" if config["configurable"]["thread_id"].startswith("case:") else "conversation"
                executed.add((graph, node, inspect.cleandoc(sql)))
        return real(self, sql, *args, **options)

    monkeypatch.setattr(psycopg.Connection, "execute", execute)
    chat = Chat(support)

    def latest():
        return next(row["id"] for row in support.cases.of_customer("CLI-001") if row["status"] == "awaiting_shop")

    model.plays([("list_orders", {}), ("list_cases", {}), ("check_stock", {"item_id": PULL}), ("read_policy", {})], "Voilà.")
    chat.say("Mes commandes ?")
    model.plays(refund("DEMO-1001"), "Ce remboursement est refusé.")
    chat.say("Remboursez la commande DEMO-1001, jamais arrivée")
    model.plays([propose("return", reason="damaged"), propose("exchange", TSHIRT, size="M"), refund(), ("escalate", {"question": "Cadeaux ?"})])
    chat.say("Mon pull est abîmé, le t-shirt est trop grand, et mon écharpe n’est jamais arrivée")
    model.plays("Non.")
    chat.say("Non, attendez")
    model.plays([propose("return", reason="damaged"), propose("exchange", TSHIRT, size="M"), refund(), ("escalate", {"question": "Cadeaux ?"})], "Fait.")
    chat.say("Mon pull est abîmé, le t-shirt est trop grand, et mon écharpe n’est jamais arrivée")
    chat.say("oui")
    by_kind = {row["kind"]: row["id"] for row in support.cases.of_customer("CLI-001")}
    clock.forward(days=8)
    support.tick()
    support.act(by_kind["return"], "approve")
    support.act(by_kind["return"], "received")
    support.act(by_kind["exchange"], "received")
    support.act(by_kind["question"], "answer", {"answer": "Oui."})
    model.plays(propose("exchange", "ITEM-DEMO-1001-3", size="L", reason="damaged"), "Fait.")
    chat.say("La jupe est abîmée")
    chat.say("oui")
    model.plays(("cancel_case", {"case_id": latest()}), "Annulé.")
    chat.say("Annulez")
    chat.say("oui")
    model.plays(propose("exchange", "ITEM-DEMO-1001-3", size="L", reason="damaged"), "Fait.")
    chat.say("La jupe, finalement")
    chat.say("oui")
    support.act(latest(), "reject", {"reason": "Non"})
    model.plays(propose("return", "ITEM-DEMO-1002-1", reason="changed_mind"), "Fait.")
    chat.say("Le sweat")
    if chat.waiting:
        chat.say("oui")
    clock.forward(days=30)
    support.tick()
    model.plays("Je peux vous offrir une remise de 15 %.", "Voici plutôt une remise de 20 %.")
    chat.say("Une remise ?")
    return executed, chat


def test_every_statement_run_by_a_node_is_shown(graphs, played):
    executed, _ = played
    shown = {(name, node["id"], each["sql"]) for name, graph in graphs.items() for node in graph["nodes"]
             for each in [*node.get("sql", []), *(node["router"] or {}).get("sql", [])]}
    assert executed - shown == set()
    assert len({(graph, node) for graph, node, _ in executed}) >= 12  # the situations do reach most nodes


def test_every_key_returned_by_a_node_is_shown(graphs, played, support):
    _, chat = played
    shown = {(name, node["id"]): {key["key"] for update in node.get("updates", []) for key in update["keys"]}
             for name, graph in graphs.items() for node in graph["nodes"]}
    seen = set()
    for name, task in tasks(support, chat.id):
        if isinstance(task.result, dict):
            seen.add((name, task.name))
            assert set(task.result) <= shown[name, task.name], (name, task.name)
    assert len(seen) >= 14


# Examples.

def fits(value, options: list) -> bool:
    """Whether a recorded value is built like one of those the code makes now."""
    if isinstance(value, list):
        elements = [each for option in options if isinstance(option, list) for each in option]
        return any(isinstance(option, list) for option in options) and all(
            fits(each, elements) for each in value if not (isinstance(each, str) and each.startswith("… et ")))
    if isinstance(value, dict):
        if value.get("type") in ("ai", "human", "tool", "remove"):  # what the model wrote varies
            return any(isinstance(option, dict) and option.get("type") == value["type"] for option in options)
        return any(isinstance(option, dict) and set(option) == set(value) and all(
            key == "args" or fits(each, [option[key]]) for key, each in value.items()) for option in options)
    return any(type(option) is type(value) for option in options)


def test_fits():
    message, proposal = {"type": "ai", "content": ""}, {"call": {"id": "a"}, "args": {}}
    assert fits([], [[proposal]]) and fits([proposal | {"args": {"size": "L"}}], [[proposal]])
    assert not fits([{"call": {}}], [[proposal]]) and not fits("a", [1]) and not fits([], ["a"])
    assert fits([message | {"tool_calls": []}, "… et 2 de plus"], [[message]]) and not fits([message], [[{"type": "tool"}]])


def test_examples_look_like_what_the_nodes_return(played, support):
    """Otherwise the code has changed: record them again with python -m evals examples."""
    _, chat = played
    found = observed(support, chat.id, cut=False)
    for graph, every in json.loads(EXAMPLES.read_text(encoding="utf-8")).items():
        for node, entry in every.items():
            for kind in ("payload", "artifact"):
                assert kind not in entry or fits(entry[kind], found[graph, node, kind]), (graph, node, kind)
            for update in entry.get("updates", []):
                for key, value in update["values"].items():
                    assert fits(value, found[graph, node, (tuple(update["keys"]), key)]), (graph, node, key)


def test_examples_are_gathered_then_merged(played, support):
    _, chat = played
    recorded = gathered(observed(support, chat.id))
    assert {node for nodes in recorded.values() for node in nodes} >= {"agent", "tools", "policy", "confirm", "apply", "guard", "give_up"}
    partial = {"conversation": {"guard": {"updates": recorded["conversation"]["guard"]["updates"][:1]}}}
    assert len(merged(recorded, partial)["conversation"]["guard"]["updates"]) == 2
    assert "payload" in partial["conversation"]["confirm"]


def test_every_key_has_an_example(graphs):
    for graph in graph_map()["graphs"]:
        for node in graph["nodes"]:
            for update in node.get("updates", []):
                assert all(key["example"] for key in update["keys"]), (graph["id"], node["id"])
            if node.get("pause"):
                assert node["pause"]["example"], node["id"]
            assert not node.get("artifacts") or node["artifact_example"], node["id"]


# Hot reload.

@pytest.fixture
def copied(tmp_path):
    """The package and its page, in a folder that can be edited."""
    for folder in ("agent_sav", "interface"):
        shutil.copytree(ROOT / folder, tmp_path / folder, ignore=shutil.ignore_patterns("__pycache__", "boutique"))
    return tmp_path


def edit(path, old, new):
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_the_map_follows_the_code(copied):
    watch, code = Watch(copied), copied / "agent_sav" / "case_graph.py"
    first = watch.version()
    before = nodes({graph["id"]: graph for graph in watch.graphs()["graphs"]}["case"])["submit"]
    assert watch.graphs() is watch.graphs()  # made once
    edit(code, '"awaiting_shop")\n        return {}', '"awaiting_shop")\n        return {"last_command": 7, "forgotten": 1}')
    assert watch.version()["code"] != first["code"] and watch.version()["page"] == first["page"]
    after = nodes({graph["id"]: graph for graph in watch.graphs()["graphs"]}["case"])["submit"]
    assert before["updates"][0]["keys"] == []
    assert [(key["key"], key["declared"]) for key in after["updates"][0]["keys"]] == [("last_command", True), ("forgotten", False)]


def test_code_that_cannot_be_read_is_explained_then_forgotten(copied):
    watch, code = Watch(copied), copied / "agent_sav" / "case_graph.py"
    edit(code, "def submit(", "def submit((")
    with pytest.raises(Unreadable, match="SyntaxError"):
        watch.graphs()
    edit(code, "def submit((", "def submit(")
    assert len(watch.graphs()["graphs"]) == 2


def test_styles_and_page_are_told_apart(copied):
    watch = Watch(copied)
    first = watch.version()
    edit(copied / "interface" / "graphes.css", ":root", "/* edited */ :root")
    second = watch.version()
    edit(copied / "interface" / "graphes.js", "const WIDTH", "/* edited */ const WIDTH")
    third = watch.version()
    changed = [{name for name in first if a[name] != b[name]} for a, b in ((first, second), (second, third))]
    assert changed == [{"style"}, {"page"}]


def test_the_map_of_a_fresh_process_is_the_same():
    assert Watch().graphs() == json.loads(json.dumps(graph_map()))
