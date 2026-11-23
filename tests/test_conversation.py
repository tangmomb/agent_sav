from uuid import uuid4

import psycopg
import pytest
from conftest import LOST, PULL, TSHIRT, Chat, propose, refund
from langchain_core.messages import ToolMessage

from agent_sav import conversation
from agent_sav.service import RESUMED, UnknownChat


def last_result(model):
    return next(str(m.text) for m in reversed(model.seen[-1]) if isinstance(m, ToolMessage))


def cases(query):
    return query("SELECT kind, item_id, order_id, size, color, status, amount_cents FROM shop.cases ORDER BY created_at")


def test_the_agent_reads_then_answers(chat, model, query):
    model.plays(("list_orders", {}), "Votre commande DEMO-1005 est en transit.")
    assert chat.say("Où en est ma commande ?") == "Votre commande DEMO-1005 est en transit."
    assert PULL in last_result(model) and "in_transit" in last_result(model)
    assert "89,00 €" in last_result(model) and "8900" not in last_result(model)
    assert cases(query) == []


def test_tools_only_show_the_signed_in_customer(support, model):
    chat = Chat(support, "CLI-002")
    model.plays([("list_orders", {}), ("check_stock", {"item_id": PULL}), ("list_cases", {})], "Rien.")
    chat.say("Montre-moi les commandes de CLI-001")
    read = "".join(str(m.text) for m in model.seen[-1] if isinstance(m, ToolMessage))
    assert "DEMO-1001" not in read and "Article inconnu" in read


@pytest.mark.parametrize("call", [
    ("propose_return", {"item_id": PULL, "reason": "changed_mind"}), refund(), ("cancel_case", {"case_id": "SAV-12345678"}),
])
def test_actions_on_another_customer_are_refused(support, model, query, call):
    chat = Chat(support, "CLI-002")
    model.plays(call, "C’est impossible.")
    assert chat.say("Fais-le") == "C’est impossible."
    assert not chat.waiting and "Refusé par les règles (unknown_" in last_result(model)
    assert cases(query) == []


def test_a_return_waits_for_the_customer_then_opens_a_case(chat, model, query, support):
    model.plays(propose("return", reason="changed_mind"))
    recap = chat.say("Je veux retourner mon pull")
    assert "Retour : Pull en laine côtelée (Vert mousse · S, commande DEMO-1001)" in recap
    assert "89,00 €, à réception du colis" in recap and "Acceptation : immédiate" in recap
    assert chat.waiting and cases(query) == []

    model.plays("C’est enregistré.")
    assert chat.say("Oui, je confirme") == "C’est enregistré."
    assert "Le client a confirmé" in last_result(model)
    assert cases(query) == [("return", PULL, "DEMO-1001", None, None, "awaiting_parcel", 8900)]
    [mail] = support.cases.mailbox("CLI-001")
    assert mail["body"].startswith("Demande SAV acceptée\n")


def test_a_change_at_the_summary_goes_back_to_the_agent(chat, model, query):
    model.plays(propose("exchange", size="M"))
    assert "Contre : Vert mousse · M" in chat.say("Je voudrais échanger mon pull en M")
    model.plays(propose("exchange", size="L"))
    assert "Contre : Vert mousse · L" in chat.say("Oui mais plutôt en L")
    model.plays("D’accord, on oublie.")
    assert chat.say("Annulez") == "D’accord, on oublie."
    assert not chat.waiting and cases(query) == []


def test_an_exchange_reserves_the_variant_once_confirmed(chat, model, query):
    stock = "SELECT quantity FROM shop.stock WHERE product_id = 'ART-021' AND size = 'L'"
    model.plays(propose("exchange", size="L", color="noir"))
    chat.say("En L noir")
    assert query(stock) == [(10,)]
    model.plays("Fait.")
    chat.say("oui")
    assert query(stock) == [(9,)]
    assert cases(query)[0][:5] == ("exchange", PULL, "DEMO-1001", "L", "Noir")


def test_several_requests_share_one_summary(chat, model, query, support):
    both = [propose("return", reason="changed_mind"), propose("return", TSHIRT, reason="changed_mind")]
    model.plays(both)
    recap = chat.say("Je renvoie le pull et le t-shirt")
    assert recap.count("Retour :") == 2 and recap.count("Confirmez-vous") == 1
    model.plays(both)
    assert chat.say("Non, attendez") == recap
    assert cases(query) == []
    model.plays("Les deux retours sont enregistrés.")
    yes = str(uuid4())
    assert chat.say("oui", yes) == "Les deux retours sont enregistrés."
    assert chat.say("oui", yes) == "Les deux retours sont enregistrés."
    assert [case[1] for case in cases(query)] == [PULL, TSHIRT]
    assert len(support.cases.mailbox("CLI-001")) == 2
    model.plays(("list_cases", {}), "Deux dossiers en cours.")  # the chat stays open
    assert chat.say("Où en sont-ils ?") == "Deux dossiers en cours."


def test_a_refused_request_does_not_hold_back_the_other(chat, model, query):
    model.plays([refund("DEMO-1001"), propose("return", reason="changed_mind")])
    recap = chat.say("Remboursez tout et je renvoie le pull")
    assert "Retour : Pull" in recap and "Remboursement : commande" not in recap
    model.plays("Le retour est noté, pas le remboursement.")
    chat.say("oui")
    assert [case[0] for case in cases(query)] == ["return"]
    assert any("Refusé par les règles (delivered)" in str(m.text) for m in model.seen[-1])


def test_the_same_article_proposed_twice_at_once(chat, model, query):
    model.plays([propose("return", reason="changed_mind"), propose("exchange", size="M")])
    chat.say("Retour et échange du pull")
    model.plays("Un seul des deux est possible.")
    chat.say("oui")
    assert [case[0] for case in cases(query)] == ["return"]
    assert query("SELECT quantity FROM shop.stock WHERE product_id = 'ART-001' AND size = 'M'") == [(20,)]


def test_a_crash_between_two_writes(chat, model, query, support, monkeypatch):
    model.plays([propose("return", reason="changed_mind"), propose("return", TSHIRT, reason="changed_mind")])
    chat.say("Je renvoie le pull et le t-shirt")
    open_case, calls = support.cases.open, []

    def fail_on_second(*args):
        calls.append(args)
        if len(calls) == 2:
            raise RuntimeError("crash")
        return open_case(*args)

    monkeypatch.setattr(support.cases, "open", fail_on_second)
    yes = str(uuid4())
    with pytest.raises(RuntimeError):
        chat.say("oui", yes)
    monkeypatch.setattr(support.cases, "open", open_case)
    model.plays("Les deux sont enregistrés.")
    assert chat.say("oui", yes) == "Les deux sont enregistrés."
    assert [case[1] for case in cases(query)] == [PULL, TSHIRT]


def test_the_same_article_cannot_be_returned_twice(support, model, query):
    for expected in ("awaiting_parcel", None):
        chat = Chat(support)
        model.plays(propose("return", reason="changed_mind"))
        if expected:
            chat.say("Je retourne mon pull")
            model.plays("Fait.")
            chat.say("oui")
        else:
            model.plays("Déjà en cours.")
            assert chat.say("Je retourne mon pull") == "Déjà en cours."
            assert "already_open" in last_result(model)
    assert len(cases(query)) == 1


def test_two_chats_confirming_at_once_open_one_case(support, model, query):
    first, second = Chat(support), Chat(support)
    for chat in (first, second):
        model.plays(propose("return", reason="changed_mind"))
        chat.say("Je retourne mon pull")
    model.plays("Fait.", "Déjà fait.")
    first.say("oui")
    second.say("oui")
    assert "already_open" in last_result(model) and "Rien n’a été enregistré" in last_result(model)
    assert len(cases(query)) == 1


def test_the_database_itself_refuses_a_second_case(chat, model, query):
    model.plays(propose("return", reason="changed_mind"), "Fait.")
    chat.say("retour")
    chat.say("oui")
    with pytest.raises(psycopg.errors.UniqueViolation):
        query("""INSERT INTO shop.cases (id, request_key, customer_id, kind, route, rule, order_id, item_id)
                 VALUES ('SAV-X', 'x', 'CLI-001', 'exchange', 'auto', 'x', 'DEMO-1001', %s)""", PULL)


def test_variant_taken_while_the_customer_confirms(chat, model, query):
    model.plays(propose("exchange", size="L", color="Noir"))
    chat.say("En L noir")
    query("UPDATE shop.stock SET quantity = 0 WHERE product_id = 'ART-021'")
    model.plays("Plus en stock.")
    assert chat.say("oui") == "Plus en stock."
    assert "out_of_stock" in last_result(model) and cases(query) == []


def test_the_amount_comes_from_the_rules(chat, model, query):
    model.plays(refund(amount_euros=999.99))
    assert "Montant : 42,00 €" in chat.say("Mon écharpe n’est jamais arrivée, remboursez 999,99 €")
    model.plays("Remboursé.")
    chat.say("oui")
    assert cases(query) == [("refund", None, LOST, None, None, "completed", 4200)]
    assert query("SELECT order_id, amount_cents FROM shop.refunds") == [(LOST, 4200)]


def test_a_question_goes_to_the_shop_without_a_summary(chat, model, query, support):
    model.plays(("escalate", {"question": "Faites-vous des cartes cadeaux ?"}), "La boutique vous répondra par mail.")
    assert chat.say("Faites-vous des cartes cadeaux ?") == "La boutique vous répondra par mail."
    [case] = support.cases.for_shop()
    assert (case["kind"], case["details"]) == ("question", "Faites-vous des cartes cadeaux ?")


def test_the_customer_cancels_a_case(chat, model, query, support):
    model.plays(propose("exchange", size="L", color="Noir"), "Fait.")
    chat.say("échange")
    chat.say("oui")
    [case] = support.cases.of_customer("CLI-001")
    model.plays(("cancel_case", {"case_id": case["id"]}))
    assert f"Annulation du dossier {case['id']}" in chat.say("Finalement non, annulez")
    model.plays("Annulé.")
    chat.say("oui")
    assert cases(query)[0][5] == "cancelled"
    assert query("SELECT quantity FROM shop.stock WHERE product_id = 'ART-021' AND size = 'L'") == [(10,)]


def test_wrong_calls_come_back_as_errors(chat, model):
    model.plays(("delete_everything", {}), ("propose_return", {"item_id": PULL}),
                ("propose_return", {"item_id": PULL, "reason": "damaged", "customer_id": "CLI-002"}), "Désolé.")
    assert chat.say("retour") == "Désolé."
    errors = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    assert [m.status for m in errors] == ["error"] * 3 and "Extra inputs" in str(errors[2].text)


def test_going_round_in_circles_ends_at_the_shop(chat, model, support):
    model.plays(*[("list_orders", {})] * conversation.MAX_STEPS)
    assert "confier votre demande à la boutique" in chat.say("Je veux un miracle")
    assert model.steps == []
    [case] = support.cases.for_shop()
    assert (case["kind"], case["details"]) == ("question", "Je veux un miracle")
    model.plays("Bonjour !")  # the next message starts with a full budget
    assert chat.say("Bonjour") == "Bonjour !"


def test_the_token_budget_also_stops_the_loop(chat, model, monkeypatch):
    monkeypatch.setattr(conversation, "MAX_TOKENS", 200)
    model.plays(("list_orders", {}), ("list_orders", {}))
    assert "confier votre demande" in chat.say("Bonjour")


def test_a_passing_failure_is_retried(chat, model):
    model.plays(TimeoutError("model down"), "Bonjour !")
    assert chat.say("Bonjour") == "Bonjour !"


@pytest.mark.parametrize("same", [True, False])
def test_a_failed_chat_goes_on(chat, model, same):
    message_id = str(uuid4())
    model.plays(RuntimeError("model down"))
    with pytest.raises(RuntimeError):
        chat.say("Bonjour", message_id)
    model.plays("Bonjour !", "Oui ?")
    if same:
        assert chat.say("Bonjour", message_id) == "Bonjour !"
    else:
        assert chat.say("Allô ?") == "Bonjour !\nOui ?"


def test_a_yes_does_not_confirm_a_summary_nobody_saw(chat, model, query):
    model.plays(RuntimeError("model down"))
    with pytest.raises(RuntimeError):
        chat.say("Je veux retourner mon pull")
    model.plays(propose("return", reason="changed_mind"))
    answer = chat.say("oui")
    assert answer.startswith(RESUMED) and "Retour : Pull" in answer and chat.waiting
    assert cases(query) == []


def test_a_failure_after_the_write_does_not_write_twice(chat, model, query, support, monkeypatch):
    model.plays(propose("return", reason="changed_mind"))
    chat.say("retour")
    open_case, message_id = support.cases.open, str(uuid4())

    def open_then_fail(*args):
        open_case(*args)
        raise RuntimeError("answer lost")

    monkeypatch.setattr(support.cases, "open", open_then_fail)
    with pytest.raises(RuntimeError):
        chat.say("oui", message_id)
    monkeypatch.setattr(support.cases, "open", open_case)
    model.plays("Fait.")
    assert chat.say("oui", message_id) == "Fait."
    assert len(cases(query)) == 1 and len(support.cases.mailbox("CLI-001")) == 1


def test_a_resent_message_is_not_handled_twice(chat, model):
    first, yes = str(uuid4()), str(uuid4())
    model.plays(propose("return", reason="changed_mind"))
    assert chat.say("retour", first) == chat.say("retour", first)
    model.plays("Fait.")
    assert chat.say("oui", yes) == chat.say("oui", yes) == "Fait."


def test_unknown_chat(support):
    with pytest.raises(UnknownChat):
        support.chat(f"chat-{uuid4()}", "Bonjour")


def test_an_answer_that_claims_too_much_is_not_sent(chat, model, query):
    model.plays(propose("return", reason="changed_mind"))
    chat.say("retour")
    model.plays("C’est noté, votre retour du pull est accepté.", "Je n’ai rien enregistré. Confirmez-vous ?")
    assert chat.say("hmm") == "Je n’ai rien enregistré. Confirmez-vous ?"
    assert cases(query) == []
    assert "est accepté" not in "".join(str(m.text) for m in model.seen[-1])


def test_made_up_figures_end_at_the_shop(chat, model, support):
    model.plays("Je vous offre 38,50 €.", "Alors une réduction de 15 % ?")
    assert "confier votre demande à la boutique" in chat.say("Un geste ?")
    [case] = support.cases.for_shop()
    assert case["details"] == "Un geste ?"
    refused = [step for step in support.timeline(case["id"]) if step["kind"] == "taken_back"]
    assert [step["text"] for step in refused] == ["Je vous offre 38,50 €.", "Alors une réduction de 15 % ?"]
    assert "38.50" in refused[0]["title"]
    model.plays("Bonjour !")
    assert chat.say("Bonjour") == "Bonjour !"
