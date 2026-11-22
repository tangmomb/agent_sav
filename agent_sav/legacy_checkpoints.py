from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.postgres import PostgresSaver


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
    "message_client": "customer_message",
    "candidats": "candidates",
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


def channel_value(name: str, value):
    if name == "action" and isinstance(value, str):
        return ACTION_VALUES.get(value, value)
    if name == "status" and isinstance(value, str):
        return STATUS_VALUES.get(value, value)
    if name == "collection_prompt" and isinstance(value, dict):
        return {
            STATE_KEYS.get(key, key): channel_value(key, item)
            for key, item in value.items()
        }
    return value


def translate_checkpoint(saved: CheckpointTuple) -> CheckpointTuple:
    checkpoint = saved.checkpoint
    values = checkpoint["channel_values"]
    renamed_task = any(key.startswith("branch:to:") and channel_name(key) != key for key in values)
    checkpoint = {
        **checkpoint,
        "channel_values": {channel_name(key): channel_value(key, value) for key, value in values.items()},
        "channel_versions": {channel_name(key): value for key, value in checkpoint["channel_versions"].items()},
        "versions_seen": {
            NODE_NAMES.get(node, node): {channel_name(key): value for key, value in versions.items()}
            for node, versions in checkpoint["versions_seen"].items()
        },
    }
    # LangGraph derives task and interrupt IDs from the node name. A renamed
    # pending node recreates its interrupt from the last committed state.
    pending_writes = [] if renamed_task else [
        (task_id, channel_name(key), channel_value(key, value))
        for task_id, key, value in saved.pending_writes or []
    ]
    return saved._replace(checkpoint=checkpoint, pending_writes=pending_writes)


class SupportPostgresSaver(PostgresSaver):
    def _load_checkpoint_tuple(self, value):
        return translate_checkpoint(super()._load_checkpoint_tuple(value))
