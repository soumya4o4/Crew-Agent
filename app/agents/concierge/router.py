import asyncio
import logging

from app.agents.concierge.classifiers import Intent, KeywordClassifier, LLMClassifier
from app.core.utils import now_ist

logger = logging.getLogger(__name__)
LLM_TIMEOUT_S = 8


class IntentRouter:
    """Understands a free-text request. Uses the LLM when configured, keywords otherwise or on any failure."""

    def __init__(self, llm: LLMClassifier | None = None):
        self.llm = llm
        self.keywords = KeywordClassifier()

    @property
    def uses_llm(self) -> bool:
        return self.llm is not None

    async def classify(self, text: str, history: list[dict], active: str | None, trip: dict | None = None) -> Intent:
        today = now_ist().date()
        if self.llm:
            return await self.llm.classify(text, today, history, active, trip)
        return self.keywords.classify(text, today, active)
