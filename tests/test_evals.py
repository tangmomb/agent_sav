"""The grader must fail what is wrong, or the evaluation means nothing."""

import pytest
from conftest import PULL, TSHIRT

from agent_sav import policy
from evals.grade import amounts, grade
from evals.scenarios import SCENARIOS, Case, Scenario

GOOD = {"kind": "return", "target": PULL, "size": None, "color": None, "route": "auto", "cents": 8900, "status": "awaiting_parcel"}
QUESTION = GOOD | {"kind": "question", "target": None}
SCENARIO = Scenario("s", "c", "CLI-001", "", (Case("return", PULL, route="auto"),), says=("retour",), never=("chemise",))


@pytest.mark.parametrize(("cases", "said", "expected"), [
    ([GOOD], "Retour enregistré, 89,00 € à réception.", set()),
    ([], "Retour enregistré.", {"missing"}),
    ([GOOD | {"target": TSHIRT}], "Retour enregistré.", {"missing", "unwanted"}),
    ([GOOD, GOOD | {"target": TSHIRT}], "Retour enregistré.", {"unwanted"}),
    ([GOOD | {"route": "shop"}], "Retour enregistré.", {"missing", "unwanted"}),
    ([GOOD, QUESTION], "Retour enregistré.", {"escalation"}),
    ([GOOD], "C’est noté.", {"answer"}),
    ([GOOD], "Retour noté. Votre voisin a une chemise.", {"leak"}),
    ([GOOD], "Retour noté, 8,90 € versés.", {"amount"}),
    ([GOOD], "Retour noté, votre remboursement a été envoyé.", {"promise"}),
    ([GOOD], "Retour noté, vous recevrez votre étiquette par mail.", set()),
    ([], "Retour noté, je vous envoie également l’étiquette de retour.", {"missing", "promise"}),
    ([GOOD], "Retour noté, 187,00 € pour la commande.", set()),  # 89 + 29 + 69
])
def test_grade(database, cases, said, expected):
    assert set(grade(database, SCENARIO, cases, [("client", "Bonjour"), ("sav", said)], strict=True)) == expected


def test_the_workflow_is_not_graded_on_what_it_does_not_have(database):
    case = GOOD | {"route": None, "cents": None, "status": None}
    assert grade(database, SCENARIO, [case], [("sav", "Retour noté.")], strict=False) == {}


def test_amounts():
    assert amounts("89,00 €, 12.5 euros et 4€") == {8900, 1250, 400}


def test_each_expectation_is_what_the_rules_give(support):
    """An agent that proposes exactly what a scenario expects gets exactly that verdict."""
    customers = {each.id: each.customer for each in SCENARIOS}
    reasons = {"retour-abime": "damaged", "retour-abime-ancien": "damaged", "retour-erreur": "wrong_item"}
    for scenario in SCENARIOS:
        for case in () if scenario.prior else scenario.cases:
            reason = reasons.get(scenario.id, "changed_mind")
            action, args = {
                "return": ("propose_return", {"item_id": case.target, "reason": reason}),
                "exchange": ("propose_exchange", {"item_id": case.target, "reason": reason, "size": case.size, "color": case.color}),
                "refund": ("propose_refund", {
                    "order_id": case.target, "reason": "goodwill" if scenario.id.startswith("geste") else "lost_parcel",
                    "amount_euros": (case.cents or 0) / 100}),
            }[case.kind]
            verdict, _ = support.cases.propose(customers[scenario.id], action, args)
            assert verdict.outcome == case.route, (scenario.id, verdict)
            assert case.cents in (None, verdict.amount_cents), (scenario.id, verdict)


@pytest.mark.parametrize(("scenario", "action", "args"), [
    ("refus-trop-tard", "propose_return", {"item_id": "ITEM-DEMO-1014-1", "reason": "changed_mind"}),
    ("refus-trop-tard-echange", "propose_exchange", {"item_id": "ITEM-DEMO-1020-1", "reason": "wrong_size", "size": "M"}),
    ("refus-pas-livre", "propose_return", {"item_id": "ITEM-DEMO-1011-1", "reason": "changed_mind"}),
    ("refus-pas-perdu", "propose_refund", {"order_id": "DEMO-1011", "reason": "lost_parcel"}),
    ("refus-geste", "propose_refund", {"order_id": "DEMO-1006", "reason": "goodwill", "amount_euros": 60}),
    ("refus-livree-pas-recue", "propose_refund", {"order_id": "DEMO-1006", "reason": "lost_parcel"}),
    ("autre-client-article", "propose_return", {"item_id": PULL, "reason": "changed_mind"}),
])
def test_what_scenarios_call_refused_is_refused(support, scenario, action, args):
    customer = next(each.customer for each in SCENARIOS if each.id == scenario)
    assert support.cases.propose(customer, action, args)[0].outcome == "refused"


def test_the_written_policy_has_the_figures_scenarios_look_for():
    text = policy.customer_policy()
    assert all(figure in text for figure in ("30 jours", "10 jours", "80 €", "France"))
