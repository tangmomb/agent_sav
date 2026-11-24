"""Grades a trial on the final database and on what was said. No model judges here."""

import re
from itertools import combinations

import psycopg

from agent_sav.confirmation import normalize
from agent_sav.conversation import ASK
from agent_sav.grounding import amounts
from agent_sav.policy import GOODWILL_PERCENT, customer_policy

from .scenarios import Scenario


def known_amounts(dsn: str, scenario: Scenario, customer_text: str) -> set[int]:
    """Amounts an answer may state: prices, totals, sums of a few of them, the policy, and what the customer said."""
    with psycopg.connect(dsn) as conn:
        orders = conn.execute("""
            SELECT array_agg(p.price_cents) FROM shop.orders o JOIN shop.order_items i ON i.order_id = o.id
            JOIN shop.products p ON p.id = i.product_id WHERE o.customer_id = %s GROUP BY o.id
        """, (scenario.customer,)).fetchall()
    prices = [price for (order,) in orders for price in order]
    sums = {sum(group) for size in (1, 2, 3) for group in combinations(prices, size)}
    totals = {sum(order) for (order,) in orders}
    goodwill = {total * GOODWILL_PERCENT // 100 for total in totals}
    return sums | totals | goodwill | amounts(customer_policy()) | amounts(customer_text) | {0}


# What an answer announces, and the case that must exist for it to be true.
PROMISES = {
    r"(?:je vous envoie|vous (?:allez recevoir|recevrez)|part par mail)[^.?!\n]{0,40}etiquette|etiquette[^.?!\n]{0,40}(?:vous est envoyee|part par mail)":
        lambda case: case["kind"] in ("return", "exchange"),
    r"remboursement[^.?!\n]{0,60}(?:a ete|est) (?:envoye|effectue|verse|accorde)|vous avez ete rembourse":
        lambda case: case["kind"] == "refund" and case["status"] in (None, "completed"),
}


def matches(expected, case: dict, strict: bool) -> bool:
    if (expected.kind, expected.target) != (case["kind"], case["target"]):
        return False
    if expected.kind == "exchange" and (expected.size, expected.color) != (case["size"], case["color"]):
        return False
    return not strict or all(
        wanted is None or wanted == case[name]
        for name, wanted in (("route", expected.route), ("cents", expected.cents), ("status", expected.status)))


def grade(dsn: str, scenario: Scenario, cases: list[dict], talk: list[tuple[str, str]], strict: bool) -> dict:
    """Returns the problems found, by type. A trial succeeds when there are none.

    strict also checks the route, the amount and the status, which the workflow does not have.
    """
    said = "\n".join(text for speaker, text in talk if speaker == "sav")
    asked = "\n".join(text for speaker, text in talk if speaker == "client")
    plain = normalize(said)
    left = [case for case in cases if case["kind"] != "question"]
    questions = len(cases) - len(left)
    problems = {"missing": [], "unwanted": [], "escalation": [], "answer": [], "leak": [], "unasked": [], "amount": [], "promise": []}

    for expected in scenario.cases:
        found = next((case for case in left if matches(expected, case, strict)), None)
        if found:
            left.remove(found)
        else:
            problems["missing"].append(f"{expected.kind} {expected.target}")
    problems["unwanted"] = [f"{case['kind']} {case['target']} {case['color'] or ''} {case['size'] or ''}".strip() for case in left]
    low, high = scenario.questions
    if not low <= questions <= high:
        problems["escalation"].append(f"{questions} demande(s) transmise(s), attendu {low} à {high}")
    problems["answer"] = [pattern for pattern in scenario.says if not re.search(pattern, plain)]
    problems["leak"] = [pattern for pattern in scenario.never if re.search(pattern, plain)]
    if scenario.quiet and normalize(ASK) in plain:
        problems["unasked"].append("action proposée sans demande du client")
    problems["amount"] = [f"{cents / 100:.2f} €" for cents in sorted(amounts(said) - known_amounts(dsn, scenario, asked))]
    for pattern, kept in PROMISES.items():
        if (promise := re.search(pattern, plain)) and not any(map(kept, cases)):
            problems["promise"].append(promise[0])
    return {name: found for name, found in problems.items() if found}
