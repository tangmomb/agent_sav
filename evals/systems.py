"""The two systems the evaluation compares, seen as a chat: the agent, and the workflow it replaced."""

import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import psycopg
from langchain_core.messages import AIMessage, ToolMessage
from psycopg.rows import dict_row

from agent_sav import model
from agent_sav.database import initialize_database, open_checkpointer
from agent_sav.service import Support

from .scenarios import Scenario

ROOT = Path(__file__).resolve().parent.parent
BASELINE_TAG = "workflow-v1"


class Failed(Exception):
    """The system did not answer: the customer can send the message again."""


class Agent:
    name = "agent"

    def __init__(self, dsn: str, scenario: Scenario):
        self.calls = self.input_tokens = self.output_tokens = self.rejected = 0
        self.failures = set(scenario.failures)
        self.support = Support(dsn, model=self.complete)
        for number, (action, args) in enumerate(scenario.prior):
            case, refusal = self.support.cases.open(f"prior:{number}", scenario.customer, action, args)
            assert refusal is None, refusal
        self.support.run_cases()
        self.chat_id = self.support.chats.open(scenario.customer)

    def complete(self, system, messages, tools) -> AIMessage:
        self.calls += 1
        self.rejected += "n’a pas été envoyée au client" in system
        if self.calls in self.failures:
            raise TimeoutError("panne simulée")
        answer = model.complete(system, messages, tools)
        self.input_tokens += answer.usage_metadata["input_tokens"]
        self.output_tokens += answer.usage_metadata["output_tokens"]
        return answer

    def say(self, message: str, message_id: str) -> str:
        try:
            return "\n".join(self.support.chat(self.chat_id, message, message_id)["messages"])
        except Exception as error:
            raise Failed(f"{type(error).__name__}: {error}") from error

    def steps(self) -> list[str]:
        """The tools the agent called, and what the rules answered."""
        with open_checkpointer(self.support.dsn) as checkpointer:
            graph = self.support.conversation_graph(checkpointer)
            values = graph.get_state({"configurable": {"thread_id": self.chat_id}}).values
        messages = values.get("messages", [])
        refused = [f"réponse retenue ({each['problem']}) : {each['text']}" for each in values.get("taken_back", [])]
        return refused + [f"{message.name} {json.dumps(call['args'], ensure_ascii=False)} → {str(message.text)[:160]}"
                for message in messages if isinstance(message, ToolMessage)
                for other in messages if isinstance(other, AIMessage)
                for call in other.tool_calls if call["id"] == message.tool_call_id]

    @staticmethod
    def prepare(dsn: str) -> None:
        initialize_database(dsn)

    @staticmethod
    def cases(dsn: str) -> list[dict]:
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            return conn.execute("""
                SELECT kind, coalesce(item_id, order_id) AS target, size, color, route, amount_cents AS cents, status
                FROM sav.cases ORDER BY created_at
            """).fetchall()

    def close(self) -> None:
        pass


class Workflow:
    """The version tagged workflow-v1, run as it was, through its web API."""

    name = "workflow"
    source: Path | None = None
    calls = input_tokens = output_tokens = rejected = None

    def __init__(self, dsn: str, scenario: Scenario):
        self.customer, self.thread = scenario.customer, None
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.server = self.run(dsn, "web", "--port", str(port))
        for _ in range(120):
            try:
                urlopen(self.url + "/api/customers", timeout=2).close()
                return
            except (URLError, TimeoutError, ConnectionError):
                if self.server.poll() is not None:
                    raise RuntimeError("Le workflow n’a pas démarré.") from None
                time.sleep(0.25)
        self.close()
        raise RuntimeError("Le workflow ne répond pas.")

    @classmethod
    def extract(cls, folder: Path) -> None:
        archive = subprocess.run(["git", "archive", BASELINE_TAG, "agent_sav", "interface"],
                                 cwd=ROOT, capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(folder, filter="data")
        cls.source = folder

    @classmethod
    def run(cls, dsn: str, *args: str) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, "-X", "utf8", "-m", "agent_sav", *args], cwd=cls.source,
                                env={**os.environ, "DATABASE_URL": dsn, "LANGSMITH_TRACING": "false"},
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @classmethod
    def prepare(cls, dsn: str) -> None:
        if cls.run(dsn, "init").wait(timeout=60):
            raise RuntimeError("La base du workflow n’a pas pu être créée.")

    def say(self, message: str, message_id: str) -> str:
        self.thread = self.thread or f"{self.customer}:{uuid4()}"
        body = json.dumps({"thread_id": self.thread, "message": message, "message_id": message_id}).encode()
        try:
            with urlopen(Request(self.url + "/api/support", data=body, headers={"Content-Type": "application/json"}),
                         timeout=120) as response:
                answer = json.load(response)
        except (URLError, TimeoutError, ConnectionError) as error:
            raise Failed(str(error)) from error
        if answer.get("awaiting_message"):
            return answer["question"]
        self.thread = None  # its page then started another conversation
        return answer["messages"][-1]["content"]

    def steps(self) -> list[str]:
        return []

    @staticmethod
    def cases(dsn: str) -> list[dict]:
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            return conn.execute("""
                SELECT CASE action WHEN 'information' THEN 'question' ELSE action END AS kind, item_id AS target, size, color,
                       NULL AS route, NULL AS cents, NULL AS status FROM shop.sav_requests ORDER BY created_at
            """).fetchall()

    def close(self) -> None:
        self.server.terminate()
        self.server.wait(timeout=10)


SYSTEMS = {"agent": Agent, "workflow": Workflow}
