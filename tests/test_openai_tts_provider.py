"""
Unit tests for OpenAITTSProvider.

Regression guard for the production bug where audio generation always failed
with status="failed" / error_code="generation_failed": the provider imported
`emergentintegrations.llm.openai.OpenAITextToSpeech` from a private package that
is NOT vendored in this repo, so the import raised ModuleNotFoundError on every
call. The provider now calls the official `openai` SDK directly.

These tests mock the openai SDK so they run offline (no network, no real key).
"""
import sys
import types
import pytest

# `motor` (the Mongo driver) is only used by audio_service for a type hint and
# is not installed in the offline test environment. Stub it so the module
# imports without a live Mongo driver. (Production ships the real motor.)
if "motor" not in sys.modules:
    _motor = types.ModuleType("motor")
    _motor_asyncio = types.ModuleType("motor.motor_asyncio")
    _motor_asyncio.AsyncIOMotorDatabase = object
    _motor.motor_asyncio = _motor_asyncio
    sys.modules["motor"] = _motor
    sys.modules["motor.motor_asyncio"] = _motor_asyncio

from services.audio_service import OpenAITTSProvider, create_tts_provider, MockTTSProvider


@pytest.mark.asyncio
async def test_synthesize_raises_when_no_api_key(monkeypatch):
    """Without a key, synthesize must fail fast with tts_not_configured."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("EMERGENT_LLM_KEY", raising=False)
    provider = OpenAITTSProvider()
    with pytest.raises(RuntimeError, match="tts_not_configured"):
        await provider.synthesize("hello world")


@pytest.mark.asyncio
async def test_synthesize_calls_official_openai_sdk(monkeypatch):
    """synthesize must call openai.AsyncOpenAI().audio.speech.create with the
    configured model/voice/format and return the audio bytes — NOT touch the
    (missing) emergentintegrations.llm.openai module."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-dummy")
    monkeypatch.setenv("OPENAI_TTS_MODEL", "tts-1")
    monkeypatch.setenv("OPENAI_TTS_VOICE", "nova")
    monkeypatch.setenv("OPENAI_TTS_FORMAT", "mp3")

    calls = {}

    class _FakeResponse:
        content = b"ID3-FAKE-MP3-BYTES"

    class _FakeSpeech:
        async def create(self, **kwargs):
            calls.update(kwargs)
            return _FakeResponse()

    class _FakeAudio:
        speech = _FakeSpeech()

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            calls["_client_kwargs"] = kwargs
            self.audio = _FakeAudio()

    # Inject a fake `openai` module so the inline `from openai import AsyncOpenAI`
    # resolves to our stub regardless of the installed SDK.
    fake_openai = types.ModuleType("openai")
    fake_openai.AsyncOpenAI = _FakeAsyncOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    provider = OpenAITTSProvider()
    result = await provider.synthesize("Audio takeaway: a test article.", voice="default")

    # Correct args forwarded to the SDK
    assert calls["model"] == "tts-1"
    assert calls["voice"] == "nova"  # "default" resolves to configured default
    assert calls["response_format"] == "mp3"
    assert calls["input"].startswith("Audio takeaway")
    assert calls["_client_kwargs"]["api_key"] == "sk-test-dummy"

    # Correct return shape
    assert result["audio_bytes"] == b"ID3-FAKE-MP3-BYTES"
    assert result["content_type"] == "audio/mpeg"
    assert result["format"] == "mp3"
    assert result["duration_seconds"] >= 0


@pytest.mark.asyncio
async def test_synthesize_explicit_voice_overrides_default(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-dummy")
    monkeypatch.setenv("OPENAI_TTS_VOICE", "nova")

    calls = {}

    class _FakeResponse:
        content = b"x" * 100

    class _FakeSpeech:
        async def create(self, **kwargs):
            calls.update(kwargs)
            return _FakeResponse()

    class _FakeAudio:
        speech = _FakeSpeech()

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            self.audio = _FakeAudio()

    fake_openai = types.ModuleType("openai")
    fake_openai.AsyncOpenAI = _FakeAsyncOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    provider = OpenAITTSProvider()
    await provider.synthesize("text", voice="shimmer")
    assert calls["voice"] == "shimmer"


@pytest.mark.asyncio
async def test_synthesize_raises_on_empty_audio(monkeypatch):
    """An empty/None response body must raise (never store empty audio as ready)."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-dummy")

    class _FakeResponse:
        content = b""

    class _FakeSpeech:
        async def create(self, **kwargs):
            return _FakeResponse()

    class _FakeAudio:
        speech = _FakeSpeech()

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            self.audio = _FakeAudio()

    fake_openai = types.ModuleType("openai")
    fake_openai.AsyncOpenAI = _FakeAsyncOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    provider = OpenAITTSProvider()
    with pytest.raises(RuntimeError, match="tts_empty_response"):
        await provider.synthesize("text")


def test_factory_returns_openai_provider_when_configured(monkeypatch):
    monkeypatch.setenv("AUDIO_TTS_PROVIDER", "openai")
    assert isinstance(create_tts_provider(), OpenAITTSProvider)


def test_factory_defaults_to_mock(monkeypatch):
    monkeypatch.delenv("AUDIO_TTS_PROVIDER", raising=False)
    assert isinstance(create_tts_provider(), MockTTSProvider)
