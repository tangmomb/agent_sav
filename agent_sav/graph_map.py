"""Reads the two graphs, and the code of their nodes, for the graph explorer.

The edges come from the compiled graphs. What a node reads, returns and runs in the database comes from its
source: the analysis follows its calls through the package and keeps the conditions met on the way. It may list
a statement a node never reaches. It must not miss one: tests check it against what really runs.
"""

import ast
import inspect
import json
import os
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from types import ModuleType

from langgraph.graph import END, START

from . import case_graph, cases, conversation, graph_notes, model, shop, tools

MODULES = {module.__name__.split(".")[-1]: module for module in (conversation, case_graph, cases, shop, tools, model)}
# What the names the code uses for its collaborators stand for.
OBJECTS = {"cases": ("cases", "Cases"), "Cases": ("cases", "Cases"), "shop": ("shop", "Shop"), "Shop": ("shop", "Shop"),
           "toolbox": ("tools", "")}
Function = ast.FunctionDef | ast.Lambda
Condition = tuple[ast.expr, bool]
UNKNOWN = object()
PASSED = object()  # an argument given where the default is empty: not empty, but unknown otherwise
OPPOSITES = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is,
             ast.Lt: ast.GtE, ast.GtE: ast.Lt, ast.Gt: ast.LtE, ast.LtE: ast.Gt}


@dataclass(frozen=True)
class Source:
    module: str
    name: str  # dotted, as Cases.advance or build_case_graph.settle
    node: Function

    @property
    def place(self) -> str:
        return f"agent_sav/{self.module}.py:{self.node.lineno}"


@cache
def tree(module: str) -> ast.Module:
    return ast.parse(Path(inspect.getsourcefile(MODULES[module])).read_text(encoding="utf-8"))


@cache
def functions() -> dict[tuple[str, str], Source]:
    found = {}

    def visit(module: str, parent: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(parent):
            if isinstance(child, (ast.FunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                if isinstance(child, ast.FunctionDef):
                    found[module, name] = Source(module, name, child)
                visit(module, child, f"{name}.")

    for module in MODULES:
        visit(module, tree(module), "")
    return found


def resolve(called: ast.expr, source: Source) -> Source | None:
    """The function of this package an expression names, if any."""
    known = functions()
    if isinstance(called, ast.Name):
        prefix = source.name
        while prefix:  # a sibling, then a function of the module
            prefix = prefix.rpartition(".")[0]
            if target := known.get((source.module, f"{prefix}.{called.id}" if prefix else called.id)):
                return target
    if isinstance(called, ast.Attribute) and isinstance(called.value, ast.Name):
        owner = called.value.id
        if owner == "self":
            return known.get((source.module, f"{source.name.rpartition('.')[0]}.{called.attr}"))
        if owner in OBJECTS:
            module, name = OBJECTS[owner]
            return known.get((module, f"{name}.{called.attr}" if name else called.attr))
    return None


# Walking a function, with the conditions each expression is under.

def always_leaves(body: list[ast.stmt]) -> bool:
    return bool(body) and isinstance(body[-1], (ast.Return, ast.Raise, ast.Continue))


def walk(node: ast.AST, conditions: tuple[Condition, ...] = ()):
    """Yields every node inside, with the conditions met on the way. Does not enter nested functions."""
    if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
        return
    yield node, conditions
    if isinstance(node, (ast.If, ast.IfExp)):
        yield from walk(node.test, conditions)
        for branch, kept in ((node.body, True), (node.orelse, False)):
            inner = (*conditions, (node.test, kept))
            yield from walk_body(branch, inner) if isinstance(branch, list) else walk(branch, inner)
        return
    for _, value in ast.iter_fields(node):
        if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
            yield from walk_body(value, conditions)
        else:
            for child in value if isinstance(value, list) else [value]:
                if isinstance(child, ast.AST):
                    yield from walk(child, conditions)


def walk_body(body: list[ast.stmt], conditions: tuple[Condition, ...]):
    for statement in body:
        yield from walk(statement, conditions)
        if isinstance(statement, ast.If) and always_leaves(statement.body):
            conditions = (*conditions, (statement.test, False))


def inside(source: Source):
    body = source.node.body
    return walk_body(body, ()) if isinstance(body, list) else walk(body)


def text(expression: ast.AST, limit: int = 160) -> str:
    code = " ".join(ast.unparse(expression).split())
    return code if len(code) <= limit else code[:limit - 1] + "…"


# What is known of the arguments: enough to drop the branches a call cannot take.

def value_of(expression: ast.expr, known: dict):
    if isinstance(expression, ast.Constant):
        return expression.value
    if isinstance(expression, ast.Tuple) and all(isinstance(each, ast.Constant) for each in expression.elts):
        return tuple(each.value for each in expression.elts)
    if isinstance(expression, ast.Name):
        return known.get(expression.id, UNKNOWN)
    return UNKNOWN


def truth(expression: ast.expr, known: dict) -> bool | None:
    if isinstance(expression, ast.UnaryOp) and isinstance(expression.op, ast.Not):
        inner = truth(expression.operand, known)
        return None if inner is None else not inner
    if isinstance(expression, ast.Compare) and len(expression.ops) == 1:
        left, right = value_of(expression.left, known), value_of(expression.comparators[0], known)
        if any(each is UNKNOWN or each is PASSED for each in (left, right)):
            return None
        tests = {ast.Eq: left == right, ast.NotEq: left != right, ast.Is: left is right, ast.IsNot: left is not right}
        return tests.get(type(expression.ops[0]))
    value = value_of(expression, known)
    return None if value is UNKNOWN else bool(value)


def bind(target: Source, call: ast.Call | None, known: dict) -> dict:
    """What a called function knows of its parameters: constant arguments, and the defaults of the others."""
    if call is None:  # passed along, as effect=cases.settle: called later with anything
        return {}
    spec = target.node.args
    names = [arg.arg for arg in spec.args]
    if names[:1] == ["self"]:
        names = names[1:]
    given = dict(zip(names, call.args)) | {each.arg: each.value for each in call.keywords if each.arg}
    defaults = dict(zip([arg.arg for arg in spec.args][len(spec.args) - len(spec.defaults):], spec.defaults))
    defaults |= {arg.arg: default for arg, default in zip(spec.kwonlyargs, spec.kw_defaults) if default is not None}
    bound = {name: value_of(expression, known) for name, expression in given.items()}
    bound |= {name: PASSED for name in given if bound[name] is UNKNOWN and name in defaults
              and isinstance(defaults[name], ast.Constant) and not defaults[name].value}
    bound |= {name: value_of(default, {}) for name, default in defaults.items() if name not in given}
    return {name: value for name, value in bound.items() if value is not UNKNOWN}


class Substitute(ast.NodeTransformer):
    """Replaces names by the constants they are known to hold."""

    def __init__(self, known: dict):
        self.known = known

    def visit_Name(self, node: ast.Name) -> ast.expr:
        value = self.known.get(node.id, UNKNOWN)
        return node if value is UNKNOWN or value is PASSED else ast.parse(repr(value), mode="eval").body


def negated(expression: ast.expr) -> ast.expr:
    if isinstance(expression, ast.UnaryOp) and isinstance(expression.op, ast.Not):
        return expression.operand
    if isinstance(expression, ast.Compare) and len(expression.ops) == 1 and type(expression.ops[0]) in OPPOSITES:
        return ast.Compare(expression.left, [OPPOSITES[type(expression.ops[0])]()], expression.comparators)
    return ast.UnaryOp(ast.Not(), expression)


def simpler(conditions: list[str]) -> list[str]:
    """Without what another condition implies: « x == 'a' » makes « x != 'b' » useless."""
    equal = {each.split(" == ")[0] for each in conditions if " == " in each}
    return [each for each in dict.fromkeys(conditions) if not (" != " in each and each.split(" != ")[0] in equal)]


def kept(conditions: tuple[Condition, ...], known: dict) -> list[str] | None:
    """The conditions still to meet, as code. None when they cannot be met."""
    left = []
    for expression, expected in conditions:
        result = truth(expression, known)
        if result is not None:
            if result != expected:
                return None
        elif {each.id for each in ast.walk(expression) if isinstance(each, ast.Name)} - {"conn"}:
            shown = Substitute(known).visit(ast.parse(ast.unparse(expression), mode="eval").body)
            left.append(text(shown if expected else negated(shown)))
    return left


# The database.

@dataclass
class Statement:
    sql: str
    operation: str
    writes: list[str]
    reads: list[str]
    columns: list[str]
    place: str
    function: str
    when: list[list[str]] = field(default_factory=list)  # one list per way to get there

    def as_json(self) -> dict:
        ways = [] if any(not way for way in self.when) else self.when
        return {**vars(self), "when": ways}


def parse_sql(sql: str) -> tuple[str, list[str], list[str], list[str]]:
    """Operation, tables written, tables read, columns written."""
    operation = sql.split()[0].upper()
    tables = list(dict.fromkeys(re.findall(r"shop\.(\w+)", sql)))
    written, columns = [], []
    if match := re.match(r"\s*INSERT INTO shop\.(\w+)\s*\(([^)]*)\)", sql):
        written, columns = [match[1]], [column.strip() for column in match[2].split(",")]
    elif match := re.match(r"\s*UPDATE shop\.(\w+)", sql):
        written = [match[1]]
        assigned = re.search(r"\bSET\b(.*?)(?:\bFROM\b|\bWHERE\b|$)", sql, re.S)
        columns = re.findall(r"(\w+)\s*=", assigned[1])
    elif match := re.match(r"\s*DELETE FROM shop\.(\w+)", sql):
        written = [match[1]]
    reads = [table for table in tables if table not in written or operation == "SELECT"]
    return operation, written, reads, columns


def statements(source: Source) -> list[Statement]:
    found: dict[tuple[str, str], Statement] = {}

    def follow(current: Source, known: dict, before: list[str], path: tuple[Source, ...]) -> None:
        if path.count(current) > 1:
            return
        for node, conditions in inside(current):
            here = kept(conditions, known)
            if here is None:
                continue
            when = simpler([*before, *here])
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr == "execute" and node.args and isinstance(node.args[0], ast.Constant):
                    sql = inspect.cleandoc(node.args[0].value)
                    if "shop." in sql:
                        operation, writes, reads, columns = parse_sql(sql)
                        statement = found.setdefault((sql, current.name), Statement(
                            sql, operation, writes, reads, columns, f"agent_sav/{current.module}.py:{node.lineno}", current.name))
                        if when not in statement.when:
                            statement.when.append(when)
                elif target := resolve(node.func, current):
                    follow(target, bind(target, node, known), when, (*path, current))
                for passed in [*node.args, *(each.value for each in node.keywords)]:
                    if isinstance(passed, ast.Attribute) and (target := resolve(passed, current)):
                        follow(target, {}, when, (*path, current))

    follow(source, {}, [], ())
    return list(found.values())


def transitions(source: Source) -> list[dict]:
    """The changes of status a node asks for."""
    found = []
    for node, conditions in inside(source):
        if not (isinstance(node, ast.Call) and text(node.func) == "cases.advance"):
            continue
        mail = node.args[3] if len(node.args) > 3 else None
        titles = []
        if isinstance(mail, ast.Call) and mail.args:
            titles = [each.value for each in ast.walk(local_value(mail.args[0], source)) if isinstance(each, ast.Constant)
                      and isinstance(each.value, str) and " " in each.value]
        elif mail is not None:
            titles = [each.value.strip() for each in ast.walk(mail) if isinstance(each, ast.Constant) and isinstance(each.value, str)]
        options = {each.arg: each.value for each in node.keywords}
        found.append({
            "from": list(ast.literal_eval(node.args[1])), "to": ast.literal_eval(node.args[2]), "mails": titles,
            "sender": ast.literal_eval(options["sender"]) if "sender" in options else "sav",
            "effect": text(options["effect"]) if "effect" in options else None,
            "when": kept(conditions, {}) or [],
        })
    return found


# The state.

def local_value(expression: ast.expr, source: Source) -> ast.expr:
    """What a local variable was first set to."""
    if isinstance(expression, ast.Name):
        for node, _ in inside(source):
            if isinstance(node, ast.Assign) and any(isinstance(each, ast.Name) and each.id == expression.id for each in node.targets):
                return node.value
    return expression


def state_reads(source: Source, path: tuple[Source, ...] = ()) -> list[str]:
    """Keys of the state a function looks at, through the helpers it gives the state to."""
    found = []
    for node, _ in inside(source):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "state" and isinstance(node.slice, ast.Constant):
            found.append(node.slice.value)
        elif isinstance(node, ast.Call):
            if text(node.func) == "state.get" and isinstance(node.args[0], ast.Constant):
                found.append(node.args[0].value)
            elif any(isinstance(each, ast.Name) and each.id == "state" for each in node.args):
                target = resolve(node.func, source)
                if target and target not in path:
                    found += state_reads(target, (*path, source))
    return list(dict.fromkeys(found))


def built(expression: ast.expr, source: Source) -> list[ast.expr]:
    """The expressions a value is made of, through local variables and what is appended to them."""
    parts = [expression]
    for name in {each.id for each in ast.walk(expression) if isinstance(each, ast.Name)}:
        for node, _ in inside(source):
            if isinstance(node, ast.Assign) and any(isinstance(each, ast.Name) and each.id == name for each in node.targets):
                parts.append(node.value)
            elif isinstance(node, ast.Call) and text(node.func) == f"{name}.append":
                parts += node.args
    return parts


def inner_keys(expression: ast.expr, source: Source) -> list[str]:
    """Keys of the dictionaries put in a state key: the schema does not declare them."""
    found = []
    for part in built(expression, source):
        candidates = [part] if isinstance(part, ast.Dict) else part.elts if isinstance(part, ast.List) else []
        if isinstance(part, (ast.Name, ast.BinOp)):
            continue
        for each in candidates:
            if isinstance(each, ast.Dict):
                found += [key.value for key in each.keys if isinstance(key, ast.Constant)]
    return list(dict.fromkeys(found))


def message_kinds(expression: ast.expr, source: Source) -> list[str]:
    found = []
    for part in built(expression, source):
        for node in ast.walk(part):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id.endswith("Message"):
                    found.append(node.func.id)
                elif target := resolve(node.func, source):
                    found += [each.func.id for each, _ in inside(target)
                              if isinstance(each, ast.Call) and isinstance(each.func, ast.Name) and each.func.id.endswith("Message")]
                elif node.func.id == "model":
                    found.append("AIMessage")
    return list(dict.fromkeys(found))


def updates(source: Source, schema: set[str]) -> list[dict]:
    """What a node can return: one entry per return, with the keys and the conditions."""
    found = []

    def entries(expression: ast.expr, owner: Source) -> list[tuple[str, ast.expr]]:
        expression = local_value(expression, owner)
        if isinstance(expression, ast.Call) and (target := resolve(expression.func, owner)):
            return [pair for node, _ in inside(target) if isinstance(node, ast.Return) for pair in entries(node.value, target)]
        if not isinstance(expression, ast.Dict):
            return []
        pairs = []
        for key, value in zip(expression.keys, expression.values):
            pairs += entries(value, owner) if key is None else [(key.value, value)]
        return list(dict(pairs).items())

    for node, conditions in inside(source):
        if isinstance(node, ast.Return) and node.value is not None:
            found.append({"when": kept(conditions, {}) or [], "keys": [{
                "key": key, "value": text(value), "declared": key in schema, "inner": [] if key == "messages" else inner_keys(value, source),
                "messages": message_kinds(value, source) if key == "messages" else [],
            } for key, value in entries(node.value, source)]})
    return found


def artifacts(source: Source) -> list[str]:
    """Keys put in the artifact of tool messages: kept in the state, unseen by the model and the schema."""
    found = []
    for node, _ in inside(source):
        value = None
        if isinstance(node, ast.keyword) and node.arg == "artifact":
            value = node.value
        elif isinstance(node, ast.Assign) and text(node.targets[0]) == "artifact":
            value = node.value
        elif isinstance(node, ast.AugAssign) and text(node.target) == "artifact":
            found.append(f"…{text(node.value)}")
        if value is not None:
            found += [key.value for each in ast.walk(value) if isinstance(each, ast.Dict) for key in each.keys if isinstance(key, ast.Constant)]
    return list(dict.fromkeys(found))


def class_fields(module: ModuleType, name: str) -> list[dict]:
    """The fields of a class of the package, with their type as it is written, its bases included."""
    definition = next(node for node in tree(module.__name__.split(".")[-1]).body if isinstance(node, ast.ClassDef) and node.name == name)
    found = []
    for base in definition.bases:
        base_name = text(base)
        if base_name == "MessagesState":
            found.append({"name": "messages", "type": "list[AnyMessage]"})
        elif base_name not in ("TypedDict", "object"):
            found += class_fields(module, base_name)
    return found + [{"name": node.target.id, "type": ast.unparse(node.annotation)} for node in definition.body if isinstance(node, ast.AnnAssign)]


def pause(source: Source, module: ModuleType) -> dict | None:
    """The interrupt of a node: what it shows, what it expects on resume."""
    for node, _ in inside(source):
        if isinstance(node, ast.Call) and text(node.func) == "interrupt":
            shown = local_value(node.args[0], source)
            options = {each.arg: each.value for each in node.keywords}
            return {
                "payload": [{"key": key.value, "value": text(local_value(value, source))} for key, value in zip(shown.keys, shown.values)],
                "schema": text(options["response_schema"]), "fields": class_fields(module, text(options["response_schema"])),
            }
    return None


# Examples, kept from a real run (python -m evals examples).

EXAMPLES = Path(__file__).parent / "graph_examples.json"


def with_examples(graph: str, node: str, returned: list[dict], paused: dict | None, kept_artifacts: list[str]) -> dict:
    """Puts next to each key a value it really had. None when no run returned these keys together."""
    recorded = json.loads(EXAMPLES.read_text(encoding="utf-8")).get(graph, {}).get(node, {}) if EXAMPLES.exists() else {}
    for update in returned:
        names = sorted(key["key"] for key in update["keys"])
        found = next((each["values"] for each in recorded.get("updates", []) if each["keys"] == names), {})
        for key in update["keys"]:
            key["example"] = {"value": found[key["key"]]} if key["key"] in found else None
    if paused:
        paused["example"] = recorded.get("payload")
    return {"updates": returned, "pause": paused, "artifacts": kept_artifacts,
            "artifact_example": recorded.get("artifact") if kept_artifacts else None}


# What a node sends to the model.

def template(expression: ast.expr) -> str:
    """A text built in the code, with what is not constant in braces."""
    if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add):
        return template(expression.left) + template(expression.right)
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return expression.value
    return f"{{{text(expression)}}}"


def call_settings() -> list[dict]:
    """The options of the call to the model, but for the messages and the tools."""
    source = functions()["model", "chat"]
    found = []
    for node, _ in inside(source):
        if isinstance(node, ast.Call) and text(node.func) == "client.chat.complete":
            for each in node.keywords:
                if each.arg is None:
                    found += [(key.value, value) for inner in ast.walk(each.value) if isinstance(inner, ast.Dict) and inner.keys
                              for key, value in zip(inner.keys, inner.values)]
                else:
                    found.append((each.arg, each.value))
        elif isinstance(node, ast.Call) and text(node.func) == "Mistral":
            found += [(each.arg, each.value) for each in node.keywords if each.arg != "api_key"]
    shown = [{"name": name, "value": text(value)} for name, value in found if name not in ("model", "messages", "tools")]
    return [*shown, {"name": "DEADLINE", "value": f"{model.DEADLINE} s"}]


def prompt(source: Source) -> dict | None:
    """Everything a node gives the model: instructions, what is added to them, messages, tools and options."""
    call = next((node for node, _ in inside(source) if isinstance(node, ast.Call) and text(node.func) == "model"), None)
    if call is None:
        return None
    system, messages, _ = call.args
    formatted = local_value(system, source)
    definition = next(node for node in tree(source.module).body if isinstance(node, ast.Assign) and text(node.targets[0]) == text(formatted.func.value))
    return {
        "name": os.getenv("SAV_MODEL") or model.MODEL,
        "instructions": getattr(MODULES[source.module], text(formatted.func.value)),
        "place": f"agent_sav/{source.module}.py:{definition.lineno}",
        "additions": [{"when": kept(conditions, {}) or [], "text": template(node.value)} for node, conditions in inside(source)
                      if isinstance(node, ast.AugAssign) and text(node.target) == text(system)],
        "messages": text(messages), "history": conversation.HISTORY,
        "tools": [{"name": each["function"]["name"], "reads": each["function"]["name"] in tools.READ_TOOLS, "sent": each}
                  for each in tools.schemas()],
        "settings": call_settings(),
    }


# The graphs.

def code_of(function, module: str) -> Source:
    """The source of a routing function, lambda included."""
    code = function.__code__
    if code.co_name != "<lambda>":
        return next(each for each in functions().values()
                    if each.module == module and each.node.name == code.co_name and each.name.startswith("build_"))
    node = next(node for node in ast.walk(tree(module)) if isinstance(node, ast.Lambda) and node.lineno == code.co_firstlineno)
    return Source(module, f"{function.__qualname__.split('.')[0]}.<lambda>", node)


def routing_code(source: Source) -> str:
    return ast.unparse(source.node.body) if isinstance(source.node, ast.Lambda) else inspect.cleandoc(ast.unparse(source.node))


def graph_json(name: str, graph, module: ModuleType, builder_name: str) -> dict:
    builder, short = graph.builder, module.__name__.split(".")[-1]
    schema = class_fields(module, builder.state_schema.__name__)
    keys = {each["name"] for each in schema}
    exits: dict[str, list[dict]] = {}

    def add_exit(source: str, target: str, router: Source | None) -> None:
        route = graph_notes.ROUTES[name].get((source, target))
        exits.setdefault(source, []).append({
            "target": target, "when": graph_notes.EXITS[name].get((source, target), ""),
            "conditional": router is not None, "route": route,
        })

    routers = {}
    for source, target in sorted(builder.edges):
        add_exit(source, target, None)
    for source, branches in builder.branches.items():
        for branch in branches.values():
            router = code_of(branch.path.func, short)
            routers[source] = {"code": routing_code(router), "place": f"agent_sav/{short}.py:{router.node.lineno}",
                               "reads": state_reads(router), "sql": [each.as_json() for each in statements(router)]}
            for target in branch.ends.values():
                add_exit(source, target, router)

    nodes = []
    for node in (START, *builder.nodes, END):
        entry = {"id": node, "position": graph_notes.POSITIONS[name].get(node), "exits": exits.get(node, []),
                 "router": routers.get(node)}
        if node in builder.nodes:
            source = functions()[short, f"{builder_name}.{node}"]
            policy = builder.nodes[node].retry_policy
            sql = [each.as_json() for each in statements(source)]
            entry |= {
                "role": graph_notes.ROLES[name].get(node, ""), "place": source.place,
                "code": inspect.cleandoc(ast.unparse(source.node)),
                "reads": state_reads(source), "transitions": transitions(source), "model": prompt(source),
                **with_examples(name, node, updates(source, keys), pause(source, module), artifacts(source)),
                "sql": [each for each in sql if not any(
                    (node, f"{each['operation']} shop.{table}") in graph_notes.IMPOSSIBLE for table in each["writes"])],
                "retry": policy and {"attempts": policy.max_attempts, "on": policy.retry_on.__name__},
            }
        nodes.append(entry)

    reducers = {key: getattr(channel, "operator", None) for key, channel in builder.channels.items()}
    for each in schema:
        operator = reducers.get(each["name"])
        each |= {
            "note": graph_notes.KEYS[name].get(each["name"], ""),
            "reducer": None if operator is None else "add_messages" if "add_messages" in operator.__name__ else f"operator.{operator.__name__}",
            "read_by": [node["id"] for node in nodes if each["name"] in node.get("reads", []) or each["name"] in (node["router"] or {}).get("reads", [])],
            "written_by": [node["id"] for node in nodes if any(key["key"] == each["name"] for update in node.get("updates", []) for key in update["keys"])],
        }
    return {
        "id": name, "nodes": nodes, "schema": schema, "state": builder.state_schema.__name__,
        "input": {"name": builder.input_schema.__name__, "keys": [each["name"] for each in class_fields(module, builder.input_schema.__name__)]},
        "context": None if builder.context_schema is None else {
            "name": builder.context_schema.__name__, "fields": class_fields(module, builder.context_schema.__name__)},
    }


def tables() -> list[dict]:
    """The tables of the shop, as the setup creates them."""
    found: dict[str, dict] = {}
    for module in ("shop", "cases"):
        for node in ast.walk(tree(module)):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str) and "CREATE TABLE" in node.value):
                continue
            for name, body in re.findall(r"CREATE TABLE IF NOT EXISTS shop\.(\w+) \((.*?)\n\s*\);", node.value, re.S):
                columns = re.findall(r"^\s*([a-z_]+) (text|integer|date|bigint|jsonb|timestamptz)\b", body, re.M)
                found[name] = {"name": name, "columns": [{"name": column, "type": kind} for column, kind in columns], "indexes": []}
            for name, column, kind in re.findall(r"ALTER TABLE shop\.(\w+) ADD COLUMN IF NOT EXISTS (\w+) (\w+)", node.value):
                found[name]["columns"].append({"name": column, "type": kind})
            for index, name, rule in re.findall(r"CREATE UNIQUE INDEX IF NOT EXISTS (\w+) ON shop\.(\w+) (.*?);", node.value, re.S):
                found[name]["indexes"].append({"name": index, "rule": " ".join(rule.split())})
    return list(found.values())


@cache
def graph_map() -> dict:
    nothing = cases.Cases("")  # the graphs are only read, never run
    graphs = [
        graph_json("conversation", conversation.build_conversation_graph(None, shop.Shop(""), nothing), conversation, "build_conversation_graph"),
        graph_json("case", case_graph.build_case_graph(None, nothing), case_graph, "build_case_graph"),
    ]
    used = {table for graph in graphs for node in graph["nodes"] for each in [*node.get("sql", []), *((node["router"] or {}).get("sql", []))]
            for table in each["reads"] + each["writes"]}
    return {
        "graphs": graphs,
        "tables": [table for table in tables() if table["name"] in used],
        "bridges": [dict(zip(("from_graph", "from", "table", "to_graph", "to", "note"), bridge)) for bridge in graph_notes.BRIDGES],
    }


if __name__ == "__main__":
    print(json.dumps(graph_map(), ensure_ascii=False))
