import pytest
from conftest import PULL, refund

from agent_sav import policy
from agent_sav.database import CHECKPOINT_TABLES, SHOP_TABLES, initialize_database
from agent_sav.service import NotExpected

RETURN = ("propose_return", {"item_id": PULL, "reason": "changed_mind"})
DAMAGED = ("propose_return", {"item_id": PULL, "reason": "damaged"})
EXCHANGE = ("propose_exchange", {"item_id": PULL, "reason": "wrong_size", "size": "L", "color": "Noir"})


@pytest.fixture
def opened(support):
    def open_case(action, customer="CLI-001", key="test"):
        case, refusal = support.cases.open(key, customer, *action)
        assert refusal is None
        support.run_cases()
        return case["id"]
    return open_case


def status(support, case_id):
    return support.cases.get(case_id)["status"]


def titles(support):
    return [mail["body"].split("\n")[0] for mail in reversed(support.cases.mailbox("CLI-001"))]


def stock(query, product, size):
    return query("SELECT quantity FROM shop.stock WHERE product_id = %s AND size = %s", product, size)[0][0]


def test_a_return_until_the_refund(support, opened, query):
    case_id = opened(RETURN)
    assert status(support, case_id) == "awaiting_parcel"
    before = stock(query, "ART-001", "S")
    support.act(case_id, "received")
    assert status(support, case_id) == "completed"
    assert titles(support) == ["Demande SAV acceptée", "Colis reçu"]
    assert query("SELECT case_id, amount_cents FROM shop.refunds") == [(case_id, 8900)]
    assert stock(query, "ART-001", "S") == before + 1
    with pytest.raises(NotExpected):
        support.act(case_id, "received")
    assert len(query("SELECT 1 FROM shop.refunds")) == 1


def test_a_damaged_article_goes_through_the_shop_and_not_back_in_stock(support, opened, query):
    case_id = opened(DAMAGED)
    assert status(support, case_id) == "awaiting_shop"
    with pytest.raises(NotExpected):
        support.act(case_id, "received")
    before = stock(query, "ART-001", "S")
    support.act(case_id, "approve")
    support.act(case_id, "received")
    assert status(support, case_id) == "completed" and stock(query, "ART-001", "S") == before


def test_the_shop_rejects(support, opened, query):
    case_id = opened(("propose_exchange", {**EXCHANGE[1], "reason": "damaged"}))
    assert stock(query, "ART-021", "L") == 9
    support.act(case_id, "reject", {"reason": "Photo illisible"})
    assert status(support, case_id) == "rejected" and stock(query, "ART-021", "L") == 10
    assert support.cases.mailbox("CLI-001")[0]["body"].endswith("Motif : Photo illisible")
    with pytest.raises(NotExpected):
        support.act(case_id, "approve")


def test_an_exchanged_article_becomes_the_new_one(support, opened, query):
    support.act(opened(EXCHANGE), "received")
    assert query("SELECT product_id, size FROM shop.order_items WHERE item_id = %s", PULL) == [("ART-021", "L")]
    case_id = opened(RETURN, key="later")  # the new one can be returned
    assert support.cases.get(case_id)["amount_cents"] == 8900


def test_the_parcel_is_reminded_once_then_expires(support, opened, query, clock):
    case_id = opened(EXCHANGE)
    clock.forward(days=policy.PARCEL_REMINDER_DAYS, minutes=1)
    for _ in range(3):
        support.tick()
    assert status(support, case_id) == "awaiting_parcel"
    assert titles(support) == ["Demande SAV acceptée", "Rappel : votre colis est attendu"]
    clock.forward(days=policy.PARCEL_EXPIRY_DAYS - policy.PARCEL_REMINDER_DAYS)
    for _ in range(3):
        support.tick()
    assert status(support, case_id) == "expired" and stock(query, "ART-021", "L") == 10
    assert titles(support)[2:] == ["Dossier clos : colis non reçu"]


def test_a_parcel_after_the_reminder(support, opened, clock):
    case_id = opened(RETURN)
    clock.forward(days=8)
    support.tick()
    support.act(case_id, "received")
    clock.forward(days=30)
    support.tick()
    assert status(support, case_id) == "completed"


def test_a_server_down_for_weeks_only_expires_the_case(support, opened, clock):
    case_id = opened(RETURN)
    clock.forward(days=60)
    support.tick()
    assert status(support, case_id) == "expired"
    assert titles(support) == ["Demande SAV acceptée", "Dossier clos : colis non reçu"]


def test_small_and_large_refunds(support, opened, query):
    small = opened(refund())
    assert status(support, small) == "completed"
    goodwill = opened(refund("DEMO-1001", "goodwill", amount_euros=5), key="goodwill")
    assert status(support, goodwill) == "awaiting_shop"
    support.act(goodwill, "approve")
    assert query("SELECT amount_cents FROM shop.refunds ORDER BY 1") == [(500,), (4200,)]


def test_a_question_is_answered_once(support, opened):
    case_id = opened(("escalate", {"question": "Cartes cadeaux ?"}))
    with pytest.raises(NotExpected):
        support.act(case_id, "approve")
    support.act(case_id, "answer", {"answer": "Oui."})
    with pytest.raises(NotExpected):
        support.act(case_id, "answer", {"answer": "Non."})
    assert [mail["body"] for mail in support.cases.mailbox("CLI-001")] == ["Réponse de la boutique\nOui."]


def test_too_many_automatic_approvals_go_to_the_shop(support, query):
    for number in range(policy.AUTO_LIMIT):
        query("""INSERT INTO shop.cases (id, request_key, customer_id, kind, route, rule, order_id, reason, status, amount_cents)
                 VALUES (%s, %s, 'CLI-001', 'refund', 'auto', 'x', 'DEMO-1002', %s, 'completed', 1)""",
              f"SAV-{number}", f"old-{number}", f"old-{number}")
    verdict, _ = support.cases.propose("CLI-001", *RETURN)
    assert (verdict.outcome, verdict.rule) == ("shop", "many_requests")


@pytest.mark.parametrize("step", ["advance", "action_done"])
def test_a_crash_changes_nothing_in_the_end(support, opened, query, monkeypatch, step):
    """The refund is paid and the email sent once, whether the crash comes after the write or before noting the command."""
    case_id = opened(RETURN)
    real = getattr(support.cases, step)

    def crash(*args, **options):
        if step == "advance":
            real(*args, **options)
        raise RuntimeError("crash")

    monkeypatch.setattr(support.cases, step, crash)
    with pytest.raises(RuntimeError):
        support.act(case_id, "received")
    monkeypatch.setattr(support.cases, step, real)
    support.tick()
    support.tick()
    assert status(support, case_id) == "completed" and support.cases.to_run() == []
    assert titles(support) == ["Demande SAV acceptée", "Colis reçu"] and len(query("SELECT 1 FROM shop.refunds")) == 1


def test_one_broken_case_does_not_hold_up_the_others(support, query, monkeypatch):
    for key, customer, action in (("broken", "CLI-001", RETURN), ("fine", "CLI-001", refund())):
        support.cases.open(key, customer, *action)
    run_case = support.run_case
    broken = query("SELECT id FROM shop.cases WHERE request_key = 'broken'")[0][0]
    monkeypatch.setattr(support, "run_case", lambda case_id: 1 / 0 if case_id == broken else run_case(case_id))
    support.tick()
    assert query("SELECT request_key, status FROM shop.cases ORDER BY 1") == [("broken", "opened"), ("fine", "completed")]


def test_database_setup_is_idempotent(database, query):
    initialize_database(database)
    tables = set(query("SELECT schemaname, tablename FROM pg_tables WHERE schemaname IN ('shop', 'langgraph')"))
    assert tables == {("shop", table) for table in SHOP_TABLES} | {("langgraph", table) for table in CHECKPOINT_TABLES}


def test_graph_shapes():
    from agent_sav.studio import case, conversation

    edges = {(edge.source, edge.target) for edge in conversation.get_graph().edges}
    assert edges == {
        ("__start__", "agent"), ("agent", "tools"), ("agent", "guard"),
        ("guard", "__end__"), ("guard", "agent"), ("guard", "give_up"),
        ("tools", "policy"), ("tools", "agent"), ("tools", "give_up"),
        ("policy", "confirm"), ("policy", "apply"), ("policy", "agent"), ("policy", "give_up"),
        ("confirm", "apply"), ("confirm", "agent"), ("apply", "apply"), ("apply", "agent"), ("give_up", "__end__"),
    }
    assert {edge.source for edge in case.get_graph().edges if edge.target == "__end__"} == {
        "settle", "reject", "answer", "cancel", "expire"}
