"""Whether the customer agrees to a summary. A rule decides, not the model."""

import re
import unicodedata

YES = re.compile(r"\W*(?:oui|ouais?|ouep|yes|yep|ok|d'accord|c'est (?:bon|ca|correct|exact)|tout est (?:bon|correct)|parfait|exact|"
                 r"(?:je )?(?:confirme|valide)|vas[- ]y|allez[- ]y)\b")
DOUBT = re.compile(r"\?|\b(?:mais|non|pas|plutot|sauf|attend\w*|finalement|annul\w*|enfin|aussi|avant|si)\b")
FILLER = set("oui ouai ouais ouep confirme valide accord correct exact parfait tout cette celui bien merci "
             "beaucoup pour vous plait demande c'est allez vas bonne".split())


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.replace("’", "'"))
    return "".join(letter for letter in text if not unicodedata.combining(letter)).casefold()


def confirms(message: str, summary: str = "") -> bool:
    """A yes that adds nothing to the summary. « Oui, mais en L » or « oui, en noir » is a change, not a yes."""
    text = normalize(message)
    if not YES.match(text) or DOUBT.search(text.replace("s'il vous plait", "")):
        return False
    if re.search(r"\b(?:s|m|l|xl|\d+)\b(?!')", text):  # a size or an amount: let the agent read it
        return False
    words = set(re.findall(r"[a-z]{4,}", text))
    return words <= FILLER | set(re.findall(r"[a-z]{4,}", normalize(summary)))
