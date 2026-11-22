import re

from .demo_data import DEMO_CUSTOMER

# Chat threads are named "<customer id>:<uuid>".
CHAT_THREAD = r"CLI-[0-9]{3}:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def customer_of(thread_id: str) -> str:
    """The customer of a chat thread. Other threads (Studio) belong to the demo customer."""
    match = re.match(r"(CLI-[0-9]{3}):", thread_id)
    return match[1] if match else DEMO_CUSTOMER
