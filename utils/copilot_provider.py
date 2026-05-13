"""
Copilot LLM Provider Abstraction.
Mock provider for tests; real provider via emergentintegrations or direct OpenAI.
"""
import os
import json
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class MockCopilotProvider:
    """Deterministic responses for tests — no external calls."""

    async def generate(self, prompt: str, context: str = "") -> str:
        return json.dumps({
            "title": "Mock Evidence Brief",
            "one_line_takeaway": "This is a mock response for testing.",
            "evidence_brief": {
                "summary": "Mock summary of the article findings.",
                "key_findings": ["Mock finding 1", "Mock finding 2"],
                "study_design": "Mock RCT",
                "population": "Mock population",
                "intervention_exposure": "Mock intervention",
                "outcomes": ["Mock outcome"],
                "limitations": ["Mock limitation"],
                "clinical_bottom_line": "Mock clinical bottom line."
            },
            "answer": "This is a mock answer to your question based on the article.",
            "confidence": "medium",
            "what_to_check_in_full_text": ["Methods section", "Table 2"],
            "comparison_title": "Mock Comparison",
            "table": {
                "columns": ["Study", "Design", "Population", "Intervention", "Key Outcome", "Limitations"],
                "rows": [["Study 1", "RCT", "N=100", "Drug A", "Positive", "Small sample"]]
            },
            "synthesis": "Mock synthesis of compared studies.",
            "draft_post": "Literature discussion only — no patient identifiers.\n\nMock draft post discussing the selected articles.",
            "suggested_questions": ["What are the limitations?", "How does this compare to prior work?"],
            "citations": []
        })


class OpenAIDirectCopilotProvider:
    """Direct OpenAI provider using Emergent proxy.
    
    Uses OPENAI_API_KEY or EMERGENT_LLM_KEY with Emergent proxy URL.
    """
    NO_TEMPERATURE_MODELS = {"gpt-5-mini", "gpt-5.2-mini"}
    EMERGENT_PROXY_URL = "https://integrations.emergentagent.com"

    def __init__(self):
        self.api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("EMERGENT_LLM_KEY")
        self.model = os.environ.get("COPILOT_MODEL", "gpt-5.2")
        if not self.api_key:
            logger.warning("OPENAI_API_KEY/EMERGENT_LLM_KEY not configured for OpenAIDirectCopilotProvider")

    async def generate(self, prompt: str, context: str = "") -> str:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=self.api_key, base_url=self.EMERGENT_PROXY_URL)
        messages = []
        if context:
            messages.append({"role": "system", "content": context})
        else:
            messages.append({"role": "system", "content": "You are a medical literature assistant."})
        messages.append({"role": "user", "content": prompt})

        kwargs = {"model": self.model, "messages": messages}
        if self.model not in self.NO_TEMPERATURE_MODELS:
            kwargs["temperature"] = 0.3

        response = await client.chat.completions.create(**kwargs)
        return response.choices[0].message.content


class RealCopilotProvider:
    """Uses emergentintegrations LlmChat for real LLM calls.
    
    Supports google/gemini, openai, and anthropic providers.
    """

    def __init__(self):
        self.api_key = os.environ.get("EMERGENT_LLM_KEY") or os.environ.get("OPENAI_API_KEY")
        provider_name = os.environ.get("COPILOT_PROVIDER", "google")
        model = os.environ.get("COPILOT_MODEL", "gemini-2.5-pro")

        # Map provider names to emergentintegrations provider keys
        provider_map = {
            "google": "gemini",
            "gemini": "gemini",
            "openai": "openai",
            "anthropic": "anthropic",
            "claude": "anthropic",
        }
        self._provider = provider_map.get(provider_name, "gemini")
        self._model = model

    async def generate(self, prompt: str, context: str = "") -> str:
        import uuid as _uuid
        from emergentintegrations.llm.chat import LlmChat, UserMessage

        chat = LlmChat(
            api_key=self.api_key,
            session_id=f"copilot_{_uuid.uuid4().hex[:8]}",
            system_message=context or "You are a medical literature assistant.",
        ).with_model(self._provider, self._model)

        message = UserMessage(text=prompt)
        response = await chat.send_message(message)
        return response


def create_copilot_provider():
    provider = os.environ.get("COPILOT_PROVIDER", "mock")
    if provider == "openai_direct":
        return OpenAIDirectCopilotProvider()
    if provider in ("claude", "openai", "google", "gemini", "anthropic"):
        return RealCopilotProvider()
    return MockCopilotProvider()
