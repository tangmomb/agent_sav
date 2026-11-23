from datetime import date, timedelta

import pytest

from agent_sav import policy
from agent_sav.confirmation import confirms

TODAY = date(2026, 6, 30)


def item(days=5, **values):
    return {"status": "delivered", "delivered_on": TODAY - timedelta(days=days), "active_case": None, "returned": False,
            "price_cents": 8900, "size": "S", "color": "Vert"} | values


def order(days=16, **values):
    return {"status": "in_transit", "ordered_on": TODAY - timedelta(days=days), "total_cents": 4200, "refunds": []} | values


@pytest.mark.parametrize(("article", "reason", "recent", "expected"), [
    (item(30), "changed_mind", 0, ("auto", "in_time")),
    (item(31), "changed_mind", 0, ("shop", "late")),
    (item(45), "changed_mind", 0, ("shop", "late")),
    (item(46), "changed_mind", 0, ("refused", "too_late")),
    (item(400), "damaged", 0, ("shop", "fault")),
    (item(731), "damaged", 0, ("refused", "warranty_over")),
    (item(5), "wrong_item", 0, ("shop", "fault")),
    (item(price_cents=10000), "changed_mind", 0, ("auto", "in_time")),
    (item(price_cents=10001), "changed_mind", 0, ("shop", "high_value")),
    (item(), "changed_mind", 3, ("shop", "many_requests")),
    (item(status="in_transit", delivered_on=None), "changed_mind", 0, ("refused", "not_delivered")),
    (item(active_case="SAV-1"), "damaged", 0, ("refused", "already_open")),
    (item(returned=True), "damaged", 0, ("refused", "already_returned")),
    (None, "damaged", 0, ("refused", "unknown_item")),
])
def test_returns(article, reason, recent, expected):
    verdict = policy.decide_return(article, reason, TODAY, recent)
    assert (verdict.outcome, verdict.rule) == expected
    assert verdict.amount_cents == (None if verdict.outcome == "refused" else article["price_cents"])


@pytest.mark.parametrize(("wanted", "stock", "reason", "expected"), [
    (("M", "Vert"), True, "wrong_size", ("auto", "in_time")),
    (("M", "Vert"), False, "wrong_size", ("refused", "out_of_stock")),
    (("S", "Vert"), True, "wrong_size", ("refused", "same_variant")),
    (("S", "Vert"), True, "damaged", ("shop", "fault")),  # the same one, in good condition
    (None, False, "wrong_size", ("refused", "unknown_variant")),
])
def test_exchanges(wanted, stock, reason, expected):
    verdict = policy.decide_exchange(item(), wanted, stock, reason, TODAY, 0)
    assert (verdict.outcome, verdict.rule) == expected


@pytest.mark.parametrize(("bought", "motive", "amount", "expected"), [
    (order(10), "lost_parcel", None, ("auto", "lost_parcel", 4200)),
    (order(9), "lost_parcel", None, ("refused", "not_lost_yet", None)),
    (order(total_cents=6001), "lost_parcel", None, ("shop", "lost_parcel", 6001)),
    (order(), "lost_parcel", 999999, ("auto", "lost_parcel", 4200)),  # the model's amount does not count
    (order(status="delivered"), "lost_parcel", None, ("refused", "delivered", None)),
    (order(status="confirmed"), "lost_parcel", None, ("refused", "not_shipped", None)),
    (order(refunds=["lost_parcel"]), "lost_parcel", None, ("refused", "already_refunded", None)),
    (order(status="delivered"), "goodwill", 840, ("shop", "goodwill", 840)),
    (order(status="delivered"), "goodwill", 841, ("refused", "goodwill_limit", None)),
    (order(status="delivered"), "goodwill", -5, ("refused", "goodwill_limit", None)),
    (order(status="delivered"), "goodwill", None, ("refused", "goodwill_limit", None)),
    (None, "lost_parcel", None, ("refused", "unknown_order", None)),
])
def test_refunds(bought, motive, amount, expected):
    verdict = policy.decide_refund(bought, motive, amount, TODAY, 0)
    assert (verdict.outcome, verdict.rule, verdict.amount_cents) == expected


def test_the_written_policy_shows_the_figures_of_the_rules():
    text = policy.customer_policy()
    assert f"dans les {policy.RETURN_DAYS} jours" in text and "100,00 €" in text
    assert "{" not in text


@pytest.mark.parametrize(("message", "expected"), [
    ("oui", True), ("ouai", True), ("Oui !", True), ("ok merci", True), ("Oui, c’est bon", True), ("Je confirme le retour", True),
    ("Oui, je confirme", True), ("Oui je confirme le retour du pull", True), ("d’accord, s’il vous plaît", True),
    ("oui pour cette demande", True),
    ("non", False), ("merci", False), ("oui mais en L", False), ("oui, enfin non", False), ("le pull", False),
    ("oui en noir", False), ("oui, en M", False), ("oui ?", False), ("oui et aussi le jean", False),
    ("oui rembourse 500", False), ("Annulez", False), ("ouistiti", False),
])
def test_only_a_plain_yes_confirms(message, expected):
    assert confirms(message, "Retour : Pull en laine côtelée (Vert mousse · S, commande DEMO-1001)") is expected
