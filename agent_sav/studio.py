import os

from agent_sav.case_graph import build_case_graph
from agent_sav.cases import Cases
from agent_sav.conversation import build_conversation_graph
from agent_sav.shop import DEFAULT_DSN, Shop

DSN = os.getenv("DATABASE_URL", DEFAULT_DSN)

# Agent Server supplies the checkpointer for Studio sessions.
conversation = build_conversation_graph(None, Shop(DSN), Cases(DSN))
case = build_case_graph(None, Cases(DSN))
