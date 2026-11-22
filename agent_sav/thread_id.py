import re
from uuid import NAMESPACE_URL, UUID, uuid5


def customer_thread(thread_id: str) -> tuple[str, str | None]:
    match = re.fullmatch(r"(CLI-[0-9]{3}):(?:(DEMO-[0-9]{4}):)?([0-9a-f-]{36})", thread_id)
    if match is None or str(UUID(match[3])) != match[3]:
        raise ValueError("Identifiant de thread SAV invalide.")
    return match[1], match[2]


def order_thread(thread_id: str) -> tuple[str, str]:
    customer_id, order_id = customer_thread(thread_id)
    if order_id is None:
        raise ValueError("Cet identifiant de thread ne contient pas de commande.")
    return customer_id, order_id


def operation_id(thread_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"agent-sav:{thread_id}"))
