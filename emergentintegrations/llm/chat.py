"""Stub for emergentintegrations.llm.chat.

Real package is emergent.sh-internal and not on PyPI. These no-op classes let
digest_agents.py import successfully. Any attempt to actually run digest
generation will raise NotImplementedError so the failure is obvious rather
than silent.
"""


class UserMessage:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class LlmChat:
    def __init__(self, *args, **kwargs):
        pass

    def with_model(self, *args, **kwargs):
        return self

    def with_system_message(self, *args, **kwargs):
        return self

    async def send_message(self, *args, **kwargs):
        raise NotImplementedError(
            "emergentintegrations is stubbed locally — AI digest generation "
            "is disabled in this environment. Restore the real package or "
            "wire LiteLLM/OpenAI directly to enable digests."
        )
