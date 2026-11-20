import re
from uuid import NAMESPACE_URL, UUID, uuid5


def order_thread(thread_id: str) -> tuple[str, str]:
    match = re.fullmatch(r"(CLI-[0-9]{3}):(DEMO-[0-9]{4}):([0-9a-f-]{36})", thread_id)
    if match is None or str(UUID(match[3])) != match[3]:
        raise ValueError("Identifiant de thread invalide. Utilisez le format CLI-001:DEMO-1001:<uuid>.")
    return match[1], match[2]


def operation_id(thread_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"agent-sav:{thread_id}"))
