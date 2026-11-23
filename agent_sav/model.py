"""Reads a customer message: what they want, and the details the message gives.

With MISTRAL_API_KEY set, a language model reads the message. Without it, simple rules do.
"""

import json
import os
import re
import unicodedata
from typing import Literal, TypeVar

from langsmith import traceable
from mistralai.client import Mistral
from mistralai.extra.struct_chat import ParsedChatCompletionResponse
from mistralai.extra.utils.response_format import response_format_from_pydantic_model
from pydantic import BaseModel, ConfigDict

MODEL = "zai-glm-5-3-flash"


class Found(BaseModel):
    """What the latest message says. None means the message does not say.

    No defaults: with optional fields, the model sometimes leaves out a value it did find.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["information", "return", "exchange", "cancel"] | None
    item_id: str | None
    size: Literal["S", "M", "L", "XL"] | None
    color: str | None
    question: str | None


def understand(message: str, request: dict, items: list[dict], history: list[dict]) -> dict:
    """Returns the fields the message gives: action, item_id, size, color, question."""
    read = understand_mistral if os.getenv("MISTRAL_API_KEY") else understand_rules
    found = read(message, request, items, history)
    if found.item_id not in {item["item_id"] for item in items}:
        found.item_id = None
    # A first message has nothing to give up yet, and a bare no only refuses the summary.
    # « Annuler », which the summary suggests, always gives up.
    if history and re.fullmatch(r"[^a-z]*annule[rz]?[^a-z]*", normalize(message)):
        found.action = "cancel"
    elif found.action == "cancel" and (not history or re.fullmatch(r"\W*non\W*", normalize(message))):
        found.action = None
    return found.model_dump()


def understand_mistral(message: str, request: dict, items: list[dict], history: list[dict]) -> Found:
    payload = {
        "user_message": message,
        "context": {
            "known_request": request,
            "delivered_items": [
                {key: item[key] for key in ("item_id", "order_id", "ordered_on", "name", "color", "size")}
                for item in items
            ],
            "recent_messages": history,
        },
    }
    instruction = (
        "Tu aides le SAV d’une boutique de vêtements. Lis le dernier message du client (user_message) et "
        "remplis les cinq champs : la valeur que ce message donne ou corrige, sinon null. Les valeurs de known_request "
        "sont déjà enregistrées : ne les recopie pas. Utilise recent_messages seulement pour comprendre les réponses "
        "courtes (« le deuxième », « en M »). "
        "Exemples, pour un échange en taille M déjà connu : « non, plutôt en L » → size L et le reste null ; "
        "« oui mais en noir » → color Noir et le reste null ; « non, c’est l’autre article » → item_id de cet article "
        "et le reste null ; « non » → tout à null. "
        "action : information pour une question à la boutique (suivi, délais, conditions…), return pour renvoyer "
        "un article, exchange pour l’échanger contre une autre taille ou un autre coloris (un article trop petit "
        "ou trop grand appelle un échange), cancel si le client renonce à la demande en cours dans cette conversation "
        "(« annuler », « laisse tomber », « finalement je garde tout »), sans en formuler une autre ; null si le message "
        "ne le dit pas. « Je veux annuler ma commande » concerne un achat, pas la demande en cours : action information "
        "et question sur l’annulation de la commande. "
        "question : pour une demande d’information, la question du client reformulée en une phrase ; "
        "null s’il annonce seulement qu’il a une question. "
        "item_id : copie exactement l’item_id de delivered_items si le message désigne un seul article. "
        "Si le client parle d’une commande (« ma dernière commande »), retrouve-la avec ordered_on ; si elle contient "
        "plusieurs articles et que le message ne précise pas lequel, item_id est null. "
        "size et color : la taille et le coloris voulus pour un échange, jamais ceux de l’article acheté. "
        "Mets null pour tout ce que le message ne dit pas."
    )
    return parse_response([
        {"role": "system", "content": instruction},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ], Found, structured_input={"system_message": instruction, **payload})


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(letter for letter in text if not unicodedata.combining(letter)).casefold()


YES = (r"(?:oui|ouais|yes|ok|d['’]accord|c['’]est (?:bon|ca|correct|exact)|tout est (?:bon|correct)|parfait|exact|"
       r"(?:je )?(?:confirme|valide)(?: (?:le|la|ma|mon|cette|cet|ce) (?:demande|retour|echange))?)")


def confirms(message: str) -> bool:
    """Whether the message is only a yes. « Oui, mais en L » is not."""
    polite = r"(?:merci|svp|s['’]il vous plait|pour moi)"
    return bool(re.fullmatch(rf"\W*{YES}(?:\W+(?:{YES}|{polite}))*\W*", normalize(message)))


def understand_rules(message: str, request: dict, items: list[dict], history: list[dict]) -> Found:
    text = normalize(message)
    found = Found(**dict.fromkeys(Found.model_fields))
    gives_up = re.search(r"\b(?:laisse[rz]? tomber|j['’]abandonne|abandonner|annule[rz]?|plus besoin)\b", text)
    if history and gives_up and not re.search(r"\b(?:commande|colis|livraison|comment)\b|\?$", text):
        found.action = "cancel"
        return found
    if re.search(r"\b(?:question|renseignement|information|suivi|ou en est|delai|quand|comment)\b|\?$", text):
        found.action = "information"
    if re.search(r"\b(?:retour\w*|retourner|renvo\w*|rembours\w*)\b", text) and not re.search(r"\bcomment\b", text):
        found.action = "return"
    if re.search(r"\b(?:echang\w*|autre taille|trop (?:petit|grand)\w*)\b", text):
        found.action = "exchange"

    if (found.action or request["action"]) == "information":
        just_announced = re.fullmatch(r"\W*(?:bonjour\W*)?(?:j'ai|une) (?:une )?question\W*", text)
        found.question = None if just_announced else message.strip()
        return found

    if size := re.search(r"(?:^|\b(?:taille|en|du|un|une)\s+)(xl|s|m|l)\b(?!['’])", text):
        found.size = size[1].upper()
    colors = {normalize(color): color for color in ("Noir", "Blanc", *(item["color"] for item in items))}
    found.color = next((color for key, color in colors.items() if re.search(rf"\b{re.escape(key)}\b", text)), None)
    if found.action is None and request["action"] is None and (found.size or found.color):
        found.action = "exchange"

    if match := re.search(r"\bitem-[\w-]+", text):
        found.item_id = match[0].upper()
    elif items:
        last_question = next((entry["content"] for entry in reversed(history) if entry["role"] == "sav"), "")
        listed = bool(re.search(r"^1\. ", last_question, re.MULTILINE))
        number = re.fullmatch(r"\W*(?:le |la |l'|article |numero )?(\d+)\W*", text)
        ordinals = ("premier|premiere", "deuxieme|second|seconde", "troisieme", "quatrieme", "cinquieme")
        ordinal = next((n for n, words in enumerate(ordinals, 1) if re.search(rf"\b(?:{words})\b", text)), None)
        if listed and (number or ordinal):
            index = int(number[1]) if number else ordinal
            found.item_id = items[index - 1]["item_id"] if 1 <= index <= len(items) else None
        else:
            words = set(re.findall(r"[a-z]{4,}", text))
            scores = [len(words & set(re.findall(r"[a-z]{4,}", normalize(item["name"])))) for item in items]
            if max(scores) and scores.count(max(scores)) == 1:
                found.item_id = items[scores.index(max(scores))]["item_id"]
    return found


Response = TypeVar("Response", bound=BaseModel)


def trace_completion_inputs(inputs: dict) -> dict:
    inputs = inputs.copy()
    schema = inputs.pop("schema", Found)
    details = dict(inputs.pop("structured_input", None) or {})
    if details:
        messages = list(inputs["messages"])
        if (system_message := details.pop("system_message", None)) is not None:
            messages[0] = {**messages[0], "content": system_message}
        user_message = messages[-1].copy()
        user_message.pop("content")
        content = details.pop("user_message", "")
        messages[-1] = {"content": content, "context": details.pop("context", {}), **user_message}
        inputs["messages"] = messages
    return {**inputs, "response_format": response_format_from_pydantic_model(schema)}


def trace_completion(result: ParsedChatCompletionResponse) -> dict:
    output = result.model_dump(mode="json", exclude_none=True, by_alias=True)
    for choice, traced_choice in zip(result.choices or [], output.get("choices", [])):
        parsed = choice.message.parsed if choice.message else None
        if isinstance(parsed, BaseModel):
            traced_choice["message"].pop("content", None)
            traced_choice["message"]["parsed"] = parsed.model_dump(mode="json", exclude_unset=True)
    if result.usage is not None:
        output["usage_metadata"] = {
            "input_tokens": result.usage.prompt_tokens,
            "output_tokens": result.usage.completion_tokens,
            "total_tokens": result.usage.total_tokens,
        }
    return output


@traceable(
    name="Z.ai GLM 5.3 Flash",
    run_type="llm",
    metadata={"ls_provider": "mistralai", "ls_model_name": MODEL},
    process_inputs=trace_completion_inputs,
    process_outputs=trace_completion,
)
def classify_with_mistral(
    messages: list[dict[str, str]], schema: type[Response], *, structured_input: dict | None = None,
) -> ParsedChatCompletionResponse[Response]:
    with Mistral(api_key=os.environ["MISTRAL_API_KEY"], timeout_ms=30000) as client:
        return client.chat.parse(
            model=MODEL, messages=messages, response_format=schema, max_tokens=1536,
            reasoning_effort="high",  # measured: "low" misses articles, the default can use every token reasoning
        )


def parse_response(
    messages: list[dict[str, str]], schema: type[Response], *, structured_input: dict | None = None,
) -> Response:
    for _ in range(2):  # The model now and then answers outside the format: ask once more.
        result = classify_with_mistral(messages, schema=schema, structured_input=structured_input)
        response = result.choices[0].message.parsed if result.choices else None
        if isinstance(response, schema):
            return response
    raise ValueError("La réponse du modèle ne correspond pas au format demandé.")
