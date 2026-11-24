"""The agent's tools. None takes a customer: it comes from the signed-in chat.

Amounts are shown in euros: given cents, the model sometimes misreads them.

Reading tools run as soon as the model asks. Action tools only describe a proposal: the policy node
decides what happens to it.
"""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import policy
from .cases import Cases
from .policy import euros
from .shop import Shop


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArgs(Args):
    pass


class ItemArgs(Args):
    item_id: str = Field(description="item_id exact, donné par list_orders")


Reason = Literal["wrong_size", "changed_mind", "damaged", "wrong_item"]
REASON = ("wrong_size : taille ou coupe ; changed_mind : ne plaît pas ; damaged : abîmé ou défectueux ; "
          "wrong_item : la boutique a envoyé un autre article que celui commandé")
DETAILS = "Ce que le client a dit d’utile à la boutique, en une phrase, en français. Vide sinon."


class ReturnArgs(ItemArgs):
    reason: Reason = Field(description=REASON)
    details: str = Field("", max_length=500, description=DETAILS)


class ExchangeArgs(ItemArgs):
    size: Literal["S", "M", "L", "XL"] | None = Field(None, description="Taille voulue ; null pour garder la même")
    color: str | None = Field(None, description="Coloris voulu ; null pour garder le même")
    reason: Reason = Field(description=REASON)
    details: str = Field("", max_length=500, description=DETAILS)


class RefundArgs(Args):
    order_id: str = Field(description="Numéro de commande, donné par list_orders")
    reason: Literal["lost_parcel", "goodwill"] = Field(
        description="lost_parcel : colis jamais arrivé ; goodwill : geste commercial demandé par le client")
    amount_euros: float | None = Field(None, description="goodwill seulement : montant demandé par le client, en euros")
    details: str = Field("", max_length=500, description=DETAILS)


class CancelArgs(Args):
    case_id: str = Field(description="Numéro de dossier SAV-…, donné par list_cases")


class EscalateArgs(Args):
    question: str = Field(min_length=1, max_length=1000, description="La demande du client, complète et autonome")


READ_TOOLS = {
    "list_orders": (NoArgs, "Commandes du client : statut, dates, articles achetés et leurs item_id."),
    "check_stock": (ItemArgs, "Tailles et coloris existants pour un article acheté, et leur disponibilité."),
    "list_cases": (NoArgs, "Dossiers SAV du client : statut, échéance et dernier mail envoyé."),
    "read_policy": (NoArgs, "Politique SAV : délais, conditions, remboursements, livraison."),
}
ACTION_TOOLS = {
    "propose_return": (ReturnArgs, "Propose le retour d’un article. Le client devra confirmer."),
    "propose_exchange": (ExchangeArgs, "Propose l’échange d’un article contre une autre taille ou un autre coloris. "
                                       "Le client devra confirmer."),
    "propose_refund": (RefundArgs, "Propose un remboursement sans retour d’article : colis perdu ou geste commercial. "
                                   "Le client devra confirmer."),
    "cancel_case": (CancelArgs, "Propose l’annulation d’un retour, d’un échange ou d’un remboursement en cours. Le client devra confirmer."),
    "escalate": (EscalateArgs, "Transmet une demande à l’équipe de la boutique, qui répond par mail. Pour ce que ni les "
                               "autres outils ni la politique SAV ne règlent."),
}
ARGS = {name: args for name, (args, _) in (READ_TOOLS | ACTION_TOOLS).items()}


def parameters(args: type[Args]) -> dict:
    """The schema of the arguments, without the titles Pydantic adds: they only repeat the names."""
    schema = args.model_json_schema()
    for described in (schema, *schema["properties"].values()):
        described.pop("title", None)
    return schema


def schemas() -> list[dict]:
    return [{"type": "function", "function": {
        "name": name, "description": description, "parameters": parameters(args),
    }} for name, (args, description) in (READ_TOOLS | ACTION_TOOLS).items()]


def dump(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def read(name: str, args: dict, customer_id: str, shop: Shop, cases: Cases) -> str:
    if name == "list_orders":
        return dump([{
            "order_id": order["id"], "status": order["status"], "ordered_on": order["ordered_on"],
            "delivered_on": order["delivered_on"], "total": euros(order["total_cents"]),
            "items": [{key: item[key] for key in ("item_id", "name", "color", "size")} | {"price": euros(item["price_cents"])}
                      for item in order["items"]],
        } for order in shop.orders(customer_id) or []])
    if name == "check_stock":
        with cases.connect() as conn:
            item = cases.item(conn, customer_id, args["item_id"])
            if item is None:
                return dump({"error": "Article inconnu pour ce client : reprends un item_id de list_orders."})
            return dump([{"color": each["color"], "size": each["size"] or None, "available": each["quantity"] > 0}
                         for each in cases.variants(conn, item["product_id"])])
    if name == "list_cases":
        return dump([{key: value for key, value in case.items() if key != "amount_cents" and value is not None}
                     | ({"amount": euros(case["amount_cents"])} if case["amount_cents"] else {})
                     for case in cases.of_customer(customer_id)])
    return policy.customer_policy()
