from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

from langchain_core.messages import AIMessage
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Interrupt

from .demo_data import purchased_item_id

NODE_NAMES = {
    "collecter": "collect",
    "rediger_message_sav": "request_support_question",
    "validation_intention": "validate_intent",
    "proposer_retour": "offer_return",
    "enregistrer_demande": "register_request",
    "notifier_boutique": "notify_shop",
    "attendre_validation_boutique": "wait_for_shop_approval",
    "envoyer_mail_refus": "send_rejection_email",
    "envoyer_mail_confirmation": "send_confirmation_email",
    "attendre_confirmation_client": "wait_for_customer_confirmation",
    "annuler_demande": "cancel_request",
    "envoyer_mail_recapitulatif": "send_summary_email",
}
STATE_KEYS = {
    "line_number": "item_id",
    "a_preciser": "required_detail",
    "reference_inconnue": "unknown_reference",
    "commande_indisponible": "unavailable_order",
    "aucune_commande_livree": "no_delivered_orders",
    "commandes": "orders",
    "reponse_ambigue": "ambiguous_reply",
    "articles": "items",
    "taille_applicable": "uses_size",
    "coloris": "colors",
}
LEGACY_REPLY_CHANNELS = ("customer_message", "message_client")
REMOVED_CHANNELS = {"candidats", "candidates", "collection_prompt", "initial_line_number", *LEGACY_REPLY_CHANNELS}
ACTION_VALUES = {"echange": "exchange", "retour": "return", "incertain": "uncertain"}
STATUS_VALUES = {
    "demande_a_clarifier": "intent_needed",
    "commande_a_preciser": "order_needed",
    "article_a_preciser": "item_needed",
    "taille_a_preciser": "variant_needed",
    "coloris_inconnu": "unknown_color",
    "article_sans_taille": "item_without_size",
    "taille_identique": "same_variant",
    "information_a_preciser": "question_needed",
    "intention_a_valider": "intent_confirmation_needed",
    "intention_a_corriger": "intent_needs_correction",
    "intention_validee": "intent_confirmed",
    "demande_abandonnee": "request_abandoned",
    "stock_indisponible": "out_of_stock",
    "stock_disponible": "in_stock",
    "attente_boutique": "awaiting_shop",
    "attente_client": "awaiting_customer",
    "refuse_boutique": "rejected_by_shop",
    "termine": "completed",
    "annule": "cancelled",
}


def channel_name(name: str) -> str:
    prefix, separator, node = name.rpartition(":")
    if separator and prefix == "branch:to":
        return f"branch:to:{NODE_NAMES.get(node, node)}"
    return STATE_KEYS.get(name, name)


def channel_value(name: str, value, order_id: str | None = None):
    if name == "action" and isinstance(value, str):
        return ACTION_VALUES.get(value, value)
    if name == "status" and isinstance(value, str):
        return STATUS_VALUES.get(value, value)
    if name == "line_number" and isinstance(value, int):
        return purchased_item_id(order_id, value) if order_id else None
    if name == "items" and isinstance(value, list):
        return [{channel_name(key): channel_value(key, item, order_id) for key, item in article.items()} for article in value]
    if name == "__interrupt__" and isinstance(value, (list, tuple)):
        events = []
        for event in value:
            if isinstance(event, Interrupt) and isinstance(event.value, dict):
                payload = dict(event.value)
                for key in LEGACY_REPLY_CHANNELS:
                    if text := payload.pop(key, None):
                        payload.setdefault("question", text)
                event = replace(event, value=payload)
            events.append(event)
        return type(value)(events)
    return value


def translate_checkpoint(saved: CheckpointTuple) -> CheckpointTuple:
    checkpoint = saved.checkpoint
    values = checkpoint["channel_values"]
    thread_id = saved.config.get("configurable", {}).get("thread_id", "")
    thread_parts = thread_id.split(":")
    order_id = values.get("order_id") or (thread_parts[1] if len(thread_parts) == 3 else None)
    renamed_task = any(key.startswith("branch:to:") and channel_name(key) != key for key in values)
    pending = saved.pending_writes or []
    old_collection = "collection_prompt" in values and any(
        key == "__interrupt__" and any(
            isinstance(event.value, dict) and "_message_ids" not in event.value for event in value
        )
        for _, key, value in pending
    )
    channel_values = {
        channel_name(key): channel_value(key, value, order_id)
        for key, value in values.items() if key not in REMOVED_CHANNELS
    }
    versions = {
        channel_name(key): value for key, value in checkpoint["channel_versions"].items() if key not in REMOVED_CHANNELS
    }
    for key in LEGACY_REPLY_CHANNELS:
        if isinstance(text := values.get(key), str) and text:
            version = checkpoint["channel_versions"].get(key, checkpoint["id"])
            message_id = str(uuid5(NAMESPACE_URL, f"sav:reply:{thread_id}:{version}"))
            messages = list(channel_values.get("messages", []))
            if not messages or not isinstance(messages[-1], AIMessage) or messages[-1].content != text:
                channel_values["messages"] = [*messages, AIMessage(content=text, id=message_id)]
                if key in checkpoint["channel_versions"]:
                    versions.setdefault("messages", version)
            break
    checkpoint = {
        **checkpoint,
        "channel_values": channel_values,
        "channel_versions": versions,
        "versions_seen": {
            NODE_NAMES.get(node, node): {
                channel_name(key): value for key, value in versions.items() if key not in REMOVED_CHANNELS
            }
            for node, versions in checkpoint["versions_seen"].items()
        },
    }
    # LangGraph derives task and interrupt IDs from the node name. A renamed
    # pending node recreates its interrupt from the last committed state.
    pending_writes = []
    if not renamed_task and not old_collection:
        for task_id, key, value in pending:
            if key in LEGACY_REPLY_CHANNELS and isinstance(value, str) and value:
                message_id = str(uuid5(NAMESPACE_URL, f"sav:reply:{task_id}"))
                pending_writes.append((task_id, "messages", [AIMessage(content=value, id=message_id)]))
            elif key not in REMOVED_CHANNELS:
                pending_writes.append((task_id, channel_name(key), channel_value(key, value, order_id)))
    return saved._replace(checkpoint=checkpoint, pending_writes=pending_writes)


class SupportPostgresSaver(PostgresSaver):
    def _load_checkpoint_tuple(self, value):
        return translate_checkpoint(super()._load_checkpoint_tuple(value))
