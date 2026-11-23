"""Checks an answer against what the chat really holds, before the customer reads it. No model here.

Instructions alone were not enough: the model sometimes announced a return nobody had recorded, or made up a discount.
"""

import re
from itertools import combinations

from langchain_core.messages import AnyMessage, HumanMessage, ToolMessage

from .confirmation import normalize

AMOUNT = re.compile(r"(\d+(?:[.,]\d{1,2})?)\s*(?:€|euros?\b)")
PERCENT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:%|pour ?cent)")
# « Votre retour est enregistré », « j’ai transmis votre demande »...
CLAIM = re.compile(
    r"\b(?:ton|ta|votre)\s+(?:\w+\s+){0,3}?(?:retour|echange|remboursement|demande|annulation|dossier)\b[^.!?\n]{0,70}?"
    r"\b(?:est|a ete|a bien ete)\b[^.!?\n]{0,25}?\b(?:accepte|enregistre|transmis|valide|annule|envoye|confirme)e?\b"
    r"|\bj'ai (?:bien )?(?:transmis|enregistre|annule|lance|valide)\b")
NOT_A_CLAIM = re.compile(r"\b(?:pas|jamais|aucune?|si|lorsque|quand|apres|avant|peut|pourra|deja)\b|\bn'")


def amounts(text: str) -> set[int]:
    return {round(float(value.replace(",", ".")) * 100) for value in AMOUNT.findall(text)}


def percents(text: str) -> set[str]:
    return {value.replace(",", ".") for value in PERCENT.findall(text)}


def sources(messages: list[AnyMessage]) -> str:
    """What the answer may rely on: what the customer said, what the tools returned, and the summaries."""
    return "\n".join(
        str(message.text) + "\n" + ((message.artifact or {}).get("recap", "") if isinstance(message, ToolMessage) else "")
        for message in messages if isinstance(message, (HumanMessage, ToolMessage)))


def ungrounded(answer: str, messages: list[AnyMessage]) -> str | None:
    """Why this answer must not be sent, or None."""
    known = sources(messages)
    figures = amounts(known)
    sums = {sum(group) for size in (2, 3) for group in combinations(sorted(figures)[:40], size)}
    if invented := amounts(answer) - figures - sums - {0}:
        return "montant absent des outils : " + ", ".join(f"{cents / 100:.2f} €" for cents in sorted(invented))
    if invented := percents(answer) - percents(known):
        return "pourcentage absent des outils : " + ", ".join(sorted(invented))
    acted = any(isinstance(message, ToolMessage) and (message.name == "list_cases" or (message.artifact or {}).get("done"))
                for message in messages)
    if not acted:
        for claim in CLAIM.finditer(normalize(answer)):
            if not NOT_A_CLAIM.search(claim[0]):
                return f"annonce une action que rien n’a enregistrée : « {claim[0]} »"
    return None
