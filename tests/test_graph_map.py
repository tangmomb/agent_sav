"""The graph explorer must show the graphs as they are: checked against the code, and against what really runs."""

import inspect
import json

import psycopg
import pytest
from conftest import PULL, TSHIRT, Chat, propose, refund
from langgraph.config import get_config
from langgraph.graph import END, START

from agent_sav import graph_notes
from agent_sav.case_graph import build_case_graph
from agent_sav.graph_map import Source, graph_map, kept, parse_sql, updates
from agent_sav.studio import case, conversation


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
    model.plays([propose("return", reason="damaged"), propose("exchange", TSHIRT, size="M"), refund(), ("escalate", {"question": "Cadeaux ?"})])
    chat.say("Beaucoup de demandes")
    model.plays("Non.")
    chat.say("Non, attendez")
    model.plays([propose("return", reason="damaged"), propose("exchange", TSHIRT, size="M"), refund(), ("escalate", {"question": "Cadeaux ?"})], "Fait.")
    chat.say("Beaucoup de demandes")
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
    model.plays("Une remise de 15 %.", "Une remise de 20 %.")
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
    threads = [("conversation", chat.id, support.conversation_graph)] + [
        ("case", f"case:{case['id']}", lambda checkpointer: build_case_graph(checkpointer, support.cases))
        for case in support.cases.of_customer("CLI-001")]
    seen = set()
    for name, thread_id, build in threads:
        with support.thread(thread_id, build) as (graph, config):
            for snapshot in graph.get_state_history(config):
                for task in snapshot.tasks:
                    if isinstance(task.result, dict) and task.name != START:
                        seen.add((name, task.name))
                        assert set(task.result) <= shown[name, task.name], (name, task.name)
    assert len(seen) >= 14
