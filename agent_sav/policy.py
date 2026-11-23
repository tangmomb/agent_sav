"""What the shop allows. The agent proposes; these rules decide, and no model is involved.

Each rule returns a Verdict:
- auto: allowed once the customer agrees;
- shop: allowed once the customer agrees, then the shop has the last word;
- refused: not allowed, with the reason to give the customer.
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

RETURN_DAYS = 30  # after delivery
LATE_DAYS = 45  # until then, the shop may still accept
WARRANTY_DAYS = 730  # damaged or wrong article
AUTO_RETURN_CENTS = 10000
LOST_AFTER_DAYS = 10  # in transit for this long since the order
AUTO_REFUND_CENTS = 6000
GOODWILL_PERCENT = 20
AUTO_LIMIT = 3  # automatic approvals per customer...
AUTO_LIMIT_DAYS = 90  # ...over this period
MAX_OPEN_QUESTIONS = 3
PARCEL_REMINDER_DAYS = 7
PARCEL_EXPIRY_DAYS = 14

FAULT_REASONS = ("damaged", "wrong_item")


@dataclass(frozen=True)
class Verdict:
    outcome: Literal["auto", "shop", "refused"]
    rule: str
    message: str = ""
    amount_cents: int | None = None  # what the customer gets back, never chosen by the model


def refused(rule: str, message: str) -> Verdict:
    return Verdict("refused", rule, message)


def euros(cents: int) -> str:
    return f"{cents / 100:.2f} €".replace(".", ",")


def customer_policy() -> str:
    """The policy shown to customers. Its figures are the ones the rules apply."""
    text = (Path(__file__).parent / "politique_sav.md").read_text(encoding="utf-8")
    return text.format(
        return_days=RETURN_DAYS, late_days=LATE_DAYS, warranty_years=WARRANTY_DAYS // 365,
        auto_return=euros(AUTO_RETURN_CENTS), auto_refund=euros(AUTO_REFUND_CENTS), lost_days=LOST_AFTER_DAYS,
        reminder_days=PARCEL_REMINDER_DAYS, expiry_days=PARCEL_EXPIRY_DAYS,
    )


def send_back(item: dict | None, reason: str, today: date, recent_auto: int, auto_cents: int | None) -> Verdict:
    """Common to returns and exchanges: may the customer send this article back?"""
    if item is None:
        return refused("unknown_item", "Cet article ne figure pas dans les achats du client.")
    if item["status"] != "delivered":
        return refused("not_delivered", "La commande n’est pas encore livrée : retour et échange seulement après livraison.")
    if item["active_case"]:
        return refused("already_open", f"Le dossier {item['active_case']} est déjà en cours pour cet article.")
    if item["returned"]:
        return refused("already_returned", "Cet article a déjà été retourné.")
    days = (today - item["delivered_on"]).days
    if reason in FAULT_REASONS:
        if days <= WARRANTY_DAYS:
            return Verdict("shop", "fault")
        return refused("warranty_over", f"L’article a été livré il y a plus de {WARRANTY_DAYS // 365} ans.")
    if days > LATE_DAYS:
        return refused("too_late", f"L’article a été livré il y a {days} jours ; le délai est de {RETURN_DAYS} jours.")
    if days > RETURN_DAYS:
        return Verdict("shop", "late")
    if recent_auto >= AUTO_LIMIT:
        return Verdict("shop", "many_requests")
    if auto_cents is not None and item["price_cents"] > auto_cents:
        return Verdict("shop", "high_value")
    return Verdict("auto", "in_time")


def decide_return(item: dict | None, reason: str, today: date, recent_auto: int) -> Verdict:
    verdict = send_back(item, reason, today, recent_auto, AUTO_RETURN_CENTS)
    return verdict if verdict.outcome == "refused" else Verdict(
        verdict.outcome, verdict.rule, amount_cents=item["price_cents"])


def decide_exchange(
    item: dict | None, wanted: tuple[str | None, str] | None, in_stock: bool, reason: str, today: date, recent_auto: int,
) -> Verdict:
    verdict = send_back(item, reason, today, recent_auto, None)
    if verdict.outcome == "refused":
        return verdict
    if wanted is None:
        return refused("unknown_variant", "Cette taille ou ce coloris n’existe pas pour cet article.")
    if wanted == (item["size"], item["color"]) and reason not in FAULT_REASONS:
        return refused("same_variant", "C’est la variante que le client a déjà.")
    if not in_stock:
        return refused("out_of_stock", "Cette variante est en rupture de stock.")
    return verdict


def decide_refund(order: dict | None, motive: str, amount_cents: int | None, today: date, recent_auto: int) -> Verdict:
    """A refund without sending anything back."""
    if order is None:
        return refused("unknown_order", "Cette commande ne figure pas dans les achats du client.")
    if motive in order["refunds"]:
        return refused("already_refunded", "Cette commande a déjà un dossier de remboursement pour ce motif.")
    if motive == "goodwill":
        limit = order["total_cents"] * GOODWILL_PERCENT // 100
        if amount_cents is None or not 0 < amount_cents <= limit:
            return refused("goodwill_limit", f"Un geste commercial va jusqu’à {euros(limit)} pour cette commande.")
        return Verdict("shop", "goodwill", amount_cents=amount_cents)
    if order["status"] == "delivered":
        return refused("delivered", "La commande est notée livrée. Transmettre le cas à la boutique.")
    if order["status"] == "confirmed":
        return refused("not_shipped", "La commande n’est pas encore expédiée.")
    days = (today - order["ordered_on"]).days
    if days < LOST_AFTER_DAYS:
        return refused("not_lost_yet", f"Un colis est considéré perdu après {LOST_AFTER_DAYS} jours ; la commande en a {days}.")
    small = order["total_cents"] <= AUTO_REFUND_CENTS and recent_auto < AUTO_LIMIT
    return Verdict("auto" if small else "shop", "lost_parcel", amount_cents=order["total_cents"])


def decide_cancel(case: dict | None) -> Verdict:
    if case is None:
        return refused("unknown_case", "Ce dossier n’appartient pas au client.")
    if case["status"] not in ("opened", "awaiting_shop", "awaiting_parcel"):
        return refused("closed", "Ce dossier est déjà clos.")
    return Verdict("auto", "cancel")


def decide_question(open_questions: int) -> Verdict:
    if open_questions >= MAX_OPEN_QUESTIONS:
        return refused("too_many_questions", "Le client a déjà plusieurs questions en attente de réponse par mail.")
    return Verdict("shop", "question")
