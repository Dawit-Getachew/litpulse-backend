"""
AI Provider Abstraction for Summaries and Deep Dives.

Supports separate models for:
  - Summaries (fast, cheap): SUMMARY_PROVIDER / SUMMARY_MODEL
  - Deep Dives (strong, thorough): DEEPDIVE_PROVIDER / DEEPDIVE_MODEL

Supports:
  - "openai_direct": Direct OpenAI SDK using OPENAI_API_KEY
  - "openai": OpenAI via emergentintegrations using EMERGENT_LLM_KEY
  - "gemini"/"google": Gemini via emergentintegrations
  - "anthropic"/"claude": Anthropic via emergentintegrations
  - "mock": Returns deterministic mock responses
"""
import os
import uuid
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class MockAIProvider:
    """Returns deterministic mock responses."""
    async def generate(self, prompt: str, system: str = "") -> str:
        return json.dumps({
            "summary": "Mock summary for testing.",
            "key_findings": ["Mock finding 1", "Mock finding 2"],
            "clinical_bottom_line": "Mock clinical bottom line.",
        })


class OpenAIDirectProvider:
    """Direct OpenAI API using Emergent proxy - fallback when emergentintegrations isn't suitable."""
    NO_TEMPERATURE_MODELS = {"gpt-5-mini", "gpt-5.2-mini"}
    EMERGENT_PROXY_URL = "https://integrations.emergentagent.com"
    
    def __init__(self, model: str):
        self.api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("EMERGENT_LLM_KEY")
        self.model = model
        if not self.api_key:
            logger.warning("OPENAI_API_KEY/EMERGENT_LLM_KEY not configured for OpenAIDirectProvider")

    async def generate(self, prompt: str, system: str = "") -> str:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=self.api_key, base_url=self.EMERGENT_PROXY_URL)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        else:
            messages.append({"role": "system", "content": "You are a medical literature assistant."})
        messages.append({"role": "user", "content": prompt})
        
        # gpt-5-mini does not support custom temperature
        kwargs = {"model": self.model, "messages": messages}
        if self.model not in self.NO_TEMPERATURE_MODELS:
            kwargs["temperature"] = 0.3
        
        response = await client.chat.completions.create(**kwargs)
        return response.choices[0].message.content


class GeminiProvider:
    """Google Gemini via emergentintegrations."""
    def __init__(self, model: str):
        self.api_key = os.environ.get("EMERGENT_LLM_KEY")
        self.model = model

    async def generate(self, prompt: str, system: str = "") -> str:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
        chat = LlmChat(
            api_key=self.api_key,
            session_id=f"ai_{uuid.uuid4().hex[:8]}",
            system_message=system or "You are a medical literature assistant.",
        ).with_model("gemini", self.model)
        response = await chat.send_message(UserMessage(text=prompt))
        return response


class OpenAIProvider:
    """OpenAI via emergentintegrations."""
    def __init__(self, model: str):
        self.api_key = os.environ.get("EMERGENT_LLM_KEY")
        self.model = model

    async def generate(self, prompt: str, system: str = "") -> str:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
        chat = LlmChat(
            api_key=self.api_key,
            session_id=f"ai_{uuid.uuid4().hex[:8]}",
            system_message=system or "You are a medical literature assistant.",
        ).with_model("openai", self.model)
        response = await chat.send_message(UserMessage(text=prompt))
        return response


class AnthropicProvider:
    """Anthropic via emergentintegrations."""
    def __init__(self, model: str):
        self.api_key = os.environ.get("EMERGENT_LLM_KEY")
        self.model = model

    async def generate(self, prompt: str, system: str = "") -> str:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
        chat = LlmChat(
            api_key=self.api_key,
            session_id=f"ai_{uuid.uuid4().hex[:8]}",
            system_message=system or "You are a medical literature assistant.",
        ).with_model("anthropic", self.model)
        response = await chat.send_message(UserMessage(text=prompt))
        return response


def _create_provider(provider_name: str, model: str):
    """Factory: create provider instance from name + model."""
    if provider_name == "mock":
        return MockAIProvider()
    if provider_name == "openai_direct":
        return OpenAIDirectProvider(model)
    if provider_name in ("google", "gemini"):
        return GeminiProvider(model)
    if provider_name == "openai":
        return OpenAIProvider(model)
    if provider_name in ("anthropic", "claude"):
        return AnthropicProvider(model)
    logger.warning("AI_PROVIDERS: unknown provider '%s', using mock", provider_name)
    return MockAIProvider()


def create_summary_provider():
    """Create the AI provider for article summaries (fast model)."""
    provider = os.environ.get("SUMMARY_PROVIDER", "mock")
    model = os.environ.get("SUMMARY_MODEL", "gemini-2.5-flash")
    return _create_provider(provider, model)


def create_deepdive_provider():
    """Create the AI provider for Deep Dives (strong model)."""
    provider = os.environ.get("DEEPDIVE_PROVIDER", "mock")
    model = os.environ.get("DEEPDIVE_MODEL", "gemini-2.5-pro")
    return _create_provider(provider, model)


# Singleton instances (created on first import after env is loaded)
_summary_provider = None
_deepdive_provider = None


def get_summary_provider():
    global _summary_provider
    if _summary_provider is None:
        _summary_provider = create_summary_provider()
    return _summary_provider


def get_deepdive_provider():
    global _deepdive_provider
    if _deepdive_provider is None:
        _deepdive_provider = create_deepdive_provider()
    return _deepdive_provider
