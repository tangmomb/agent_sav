"""Tells the shop's operator that requests are waiting, through an MCP contact server.

Grouped and spaced out: the contact server has quotas. Only case numbers and kinds leave the shop, never what
a customer wrote.
"""

import asyncio
import os
from datetime import timedelta

import psycopg
from mcp import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from .cases import KINDS, Cases

EVERY = timedelta(minutes=30)
TIMEOUT = 20


class NotSent(Exception):
    pass


async def call_contact_server(url: str, token: str, arguments: dict) -> None:
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
        async with Client(streamable_http_client(url, http_client=http), read_timeout_seconds=TIMEOUT) as client:
            result = await client.call_tool("send_telegram", arguments)
            if result.is_error:
                raise NotSent("Le serveur de contact a refusé le message.")


def alert_operator(cases: Cases) -> bool:
    """Sends one message for the cases the operator has not heard of yet. Returns whether one was sent.

    The cases are noted once the message is out: after a crash in between, it is sent again rather than lost.
    """
    url, token = os.getenv("CONTACT_MCP_URL"), os.getenv("CONTACT_MCP_TOKEN")
    if not url or not token:
        return False
    with psycopg.connect(cases.dsn) as lock:
        if not lock.execute("SELECT pg_try_advisory_xact_lock(hashtextextended('sav_alerts', 0))").fetchone()[0]:
            return False
        waiting, last = cases.to_alert()
        if not waiting or (last and cases.clock() - last < EVERY):
            return False
        number = len(waiting)
        subject = f"Agent SAV : {number} demande{'s' if number > 1 else ''} à traiter"
        message = "\n".join(f"{case['id']} · {KINDS[case['kind']]}" for case in waiting[:20])
        arguments = {"subject": subject, "message": message, "urgency": "normal"}
        asyncio.run(asyncio.wait_for(call_contact_server(url, token, arguments), TIMEOUT * 2))
        cases.alerted([case["id"] for case in waiting])
        return True
