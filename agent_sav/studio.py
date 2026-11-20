import os

from agent_sav.model import interpret_mistral
from agent_sav.shop import DEFAULT_DSN, Shop
from agent_sav.workflow import build_graph

# Agent Server supplies the checkpointer for Studio sessions.
graph = build_graph(None, Shop(os.getenv("DATABASE_URL", DEFAULT_DSN)), interpret_mistral)
