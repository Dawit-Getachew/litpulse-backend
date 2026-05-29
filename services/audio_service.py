"""
Audio Takeaway Service for LitPulse.
Handles TTS generation (mock + OpenAI), storage (local + S3-ready), script building, and caching.
PHI-Zero: only uses article metadata/AI summary, never user text.
"""
import hashlib
import os
import uuid
import struct
import math
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Audio script builder (PHI-Zero: only article metadata)
# ---------------------------------------------------------------------------

def build_audio_script(article: Dict[str, Any]) -> str:
    """Build a TTS script from article metadata only. Never includes user text."""
    title = article.get("title", "Untitled article")
    journal = article.get("journal", "")
    pub_date = article.get("pub_date", "")
    ai_summary = article.get("ai_summary", "")
    key_findings = article.get("key_findings", [])

    parts = [f"Audio takeaway: {title}."]
    if journal:
        parts.append(f"Published in {journal}")
        if pub_date:
            parts[-1] += f", {pub_date}"
        parts[-1] += "."
    if ai_summary:
        parts.append(ai_summary)
    if key_findings:
        parts.append("Key findings: " + "; ".join(key_findings) + ".")
    parts.append("AI-generated summary from the abstract. Verify details in the full paper.")
    return " ".join(parts)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Mock TTS provider (generates valid WAV — used for tests + dev)
# ---------------------------------------------------------------------------

def _generate_mock_wav(duration_seconds: float = 2.0, sample_rate: int = 16000) -> bytes:
    num_samples = int(sample_rate * duration_seconds)
    freq = 440.0
    samples = []
    for i in range(num_samples):
        t = i / sample_rate
        val = int(16000 * math.sin(2 * math.pi * freq * t))
        samples.append(struct.pack('<h', max(-32768, min(32767, val))))
    audio_data = b''.join(samples)
    data_size = len(audio_data)
    header = struct.pack('<4sI4s', b'RIFF', 36 + data_size, b'WAVE')
    fmt = struct.pack('<4sIHHIIHH', b'fmt ', 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
    data_header = struct.pack('<4sI', b'data', data_size)
    return header + fmt + data_header + audio_data


class MockTTSProvider:
    async def synthesize(self, text: str, voice: str = "default") -> Dict:
        wav = _generate_mock_wav(duration_seconds=min(len(text) / 100, 5.0))
        return {"audio_bytes": wav, "content_type": "audio/wav", "duration_seconds": round(len(text) / 100, 1), "format": "wav"}


# ---------------------------------------------------------------------------
# OpenAI TTS provider (real speech via the official openai SDK)
# ---------------------------------------------------------------------------

class OpenAITTSProvider:
    def __init__(self):
        # Prefer the real OpenAI key for the DIRECT api.openai.com call. The
        # legacy EMERGENT_LLM_KEY only worked via the (now-removed) Emergent
        # proxy and is NOT a valid OpenAI key, so it must not be used here.
        self.api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("EMERGENT_LLM_KEY")
        self.base_url = os.environ.get("OPENAI_TTS_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or None
        self.model = os.environ.get("OPENAI_TTS_MODEL", "tts-1")
        self.default_voice = os.environ.get("OPENAI_TTS_VOICE", "nova")
        self.fmt = os.environ.get("OPENAI_TTS_FORMAT", "mp3")

    async def synthesize(self, text: str, voice: str = "default") -> Dict:
        if not self.api_key:
            raise RuntimeError("tts_not_configured")

        use_voice = voice if voice != "default" else self.default_voice

        # Truncate to 4096 chars (OpenAI TTS input limit)
        truncated = text[:4096]

        # Call OpenAI's Text-to-Speech directly via the official SDK. (The
        # previous emergentintegrations.llm.openai.OpenAITextToSpeech import
        # pointed at a private package that isn't vendored here, which made
        # every generation fail with a swallowed ModuleNotFoundError.)
        from openai import AsyncOpenAI

        client_kwargs = {"api_key": self.api_key}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        client = AsyncOpenAI(**client_kwargs)

        response = await client.audio.speech.create(
            model=self.model,
            voice=use_voice,
            input=truncated,
            response_format=self.fmt,
        )
        # openai 1.x returns a binary response object; .content is the bytes.
        audio_bytes = getattr(response, "content", None)
        if audio_bytes is None and hasattr(response, "read"):
            audio_bytes = await response.read() if callable(getattr(response, "read", None)) else None
        if not audio_bytes:
            raise RuntimeError("tts_empty_response")

        content_type = "audio/mpeg" if self.fmt == "mp3" else f"audio/{self.fmt}"
        # Estimate duration (~150 words/min, ~5 chars/word)
        est_duration = round(len(truncated) / 5 / 150 * 60, 1)

        return {
            "audio_bytes": audio_bytes,
            "content_type": content_type,
            "duration_seconds": est_duration,
            "format": self.fmt,
        }


# ---------------------------------------------------------------------------
# Local storage backend (dev/test)
# ---------------------------------------------------------------------------

class LocalStorageBackend:
    def __init__(self, base_dir: str = "/app/backend/storage/audio"):
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)
        self.backend_name = "local"

    async def put_bytes(self, key: str, data: bytes, content_type: str = "audio/wav") -> str:
        ext = "mp3" if "mpeg" in content_type or "mp3" in content_type else "wav"
        # Flatten path separators for local storage
        safe_key = key.replace("/", "_")
        filename = f"{safe_key}.{ext}"
        path = os.path.join(self.base_dir, filename)
        with open(path, "wb") as f:
            f.write(data)
        return filename

    async def get_url(self, storage_key: str, base_url: str = "") -> str:
        return f"{base_url}/api/audio/files/{storage_key}"

    async def file_exists(self, storage_key: str) -> bool:
        """Check if file exists in local storage."""
        path = os.path.join(self.base_dir, storage_key)
        return os.path.exists(path)

    async def get_file_bytes(self, storage_key: str) -> Optional[bytes]:
        """Get file bytes from local storage (for migration)."""
        path = os.path.join(self.base_dir, storage_key)
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
        return None


# ---------------------------------------------------------------------------
# S3-compatible storage backend (production)
# ---------------------------------------------------------------------------

class S3StorageBackend:
    def __init__(self):
        self.bucket = os.environ.get("AUDIO_S3_BUCKET", "")
        self.region = os.environ.get("AUDIO_S3_REGION", "us-east-1")
        self.endpoint_url = os.environ.get("AUDIO_S3_ENDPOINT_URL")
        self.url_ttl = int(os.environ.get("AUDIO_URL_TTL_SECONDS", "300"))
        self._client = None
        self.backend_name = "s3"

    def _get_client(self):
        if self._client is None:
            import boto3
            kwargs = {
                "service_name": "s3",
                "region_name": self.region,
                "aws_access_key_id": os.environ.get("AUDIO_S3_ACCESS_KEY_ID"),
                "aws_secret_access_key": os.environ.get("AUDIO_S3_SECRET_ACCESS_KEY"),
            }
            if self.endpoint_url:
                kwargs["endpoint_url"] = self.endpoint_url
            self._client = boto3.client(**kwargs)
        return self._client

    async def put_bytes(self, key: str, data: bytes, content_type: str = "audio/mpeg") -> str:
        ext = "mp3" if "mpeg" in content_type or "mp3" in content_type else "wav"
        s3_key = f"audio/{key}.{ext}"
        client = self._get_client()
        client.put_object(Bucket=self.bucket, Key=s3_key, Body=data, ContentType=content_type)
        return s3_key

    async def get_url(self, storage_key: str, base_url: str = "") -> str:
        client = self._get_client()
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": storage_key},
            ExpiresIn=self.url_ttl,
        )
        return url

    async def file_exists(self, storage_key: str) -> bool:
        """Check if file exists in S3."""
        try:
            client = self._get_client()
            client.head_object(Bucket=self.bucket, Key=storage_key)
            return True
        except Exception:
            return False

    async def get_file_bytes(self, storage_key: str) -> Optional[bytes]:
        """Get file bytes from S3."""
        try:
            client = self._get_client()
            response = client.get_object(Bucket=self.bucket, Key=storage_key)
            return response["Body"].read()
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Factory: create provider + storage from env
# ---------------------------------------------------------------------------

def create_tts_provider():
    provider = os.environ.get("AUDIO_TTS_PROVIDER", "mock")
    if provider == "openai":
        return OpenAITTSProvider()
    return MockTTSProvider()


def create_storage_backend():
    backend = os.environ.get("AUDIO_STORAGE_BACKEND", "local")
    if backend == "s3" and os.environ.get("AUDIO_S3_BUCKET"):
        return S3StorageBackend()
    return LocalStorageBackend()


# ---------------------------------------------------------------------------
# Audio service (orchestrator)
# Step 16: Hybrid storage support for cutover
# ---------------------------------------------------------------------------

class AudioService:
    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db
        self.tts = create_tts_provider()
        self.storage = create_storage_backend()
        # Keep local backend for hybrid reads during migration
        self._local_storage = LocalStorageBackend()
        self._s3_storage = None

    def _get_s3_storage(self):
        """Lazy-load S3 storage for hybrid reads."""
        if self._s3_storage is None and os.environ.get("AUDIO_S3_BUCKET"):
            self._s3_storage = S3StorageBackend()
        return self._s3_storage

    async def get_audio_status(self, pmid: str) -> Optional[Dict]:
        return await self.db.article_audio_summaries.find_one(
            {"pmid": pmid}, {"_id": 0}
        )

    async def get_url_for_record(self, record: Dict, base_url: str = "") -> Optional[str]:
        """
        Get audio URL based on record's storage_backend (hybrid support).
        Allows existing local files to play even after S3 is configured.
        """
        if not record or not record.get("storage_key"):
            return None
        
        storage_backend = record.get("storage_backend", "local")  # Default to local for legacy
        
        if storage_backend == "s3":
            s3 = self._get_s3_storage()
            if s3:
                return await s3.get_url(record["storage_key"], base_url)
            else:
                # S3 not configured but record says s3 - fallback to local
                return await self._local_storage.get_url(record["storage_key"], base_url)
        else:
            # Local storage
            return await self._local_storage.get_url(record["storage_key"], base_url)

    async def generate_audio(self, pmid: str, user_id: str) -> Dict:
        """Initiate audio generation (idempotent). Returns status dict."""
        article = await self.db.articles.find_one({"pmid": pmid}, {"_id": 0})
        if not article:
            return {"status": "failed", "error_message": "Article not found"}

        script = build_audio_script(article)
        t_hash = text_hash(script)
        provider_name = os.environ.get("AUDIO_TTS_PROVIDER", "mock")
        voice = os.environ.get("OPENAI_TTS_VOICE", "default") if provider_name == "openai" else "default"
        current_storage_backend = os.environ.get("AUDIO_STORAGE_BACKEND", "local")

        # Check existing
        existing = await self.db.article_audio_summaries.find_one(
            {"pmid": pmid, "voice": voice, "text_hash": t_hash}, {"_id": 0}
        )
        if existing and existing.get("status") == "ready":
            return existing
        if existing and existing.get("status") == "pending":
            return existing

        now = datetime.now(timezone.utc).isoformat()
        audio_id = str(uuid.uuid4())

        # Upsert to pending
        await self.db.article_audio_summaries.update_one(
            {"pmid": pmid, "voice": voice, "text_hash": t_hash},
            {"$set": {
                "status": "pending",
                "transcript": script,
                "last_requested_at": now,
                "updated_at": now,
            }, "$setOnInsert": {
                "audio_id": audio_id,
                "pmid": pmid,
                "voice": voice,
                "text_hash": t_hash,
                "provider": provider_name,
                "storage_key": None,
                "storage_backend": current_storage_backend,  # Step 16: Track storage backend
                "file_size_bytes": None,
                "duration_seconds": None,
                "audio_format": None,
                "audio_content_type": None,
                "error_code": None,
                "error_message": None,
                "created_at": now,
            }},
            upsert=True,
        )

        # Run generation
        try:
            result = await self.tts.synthesize(script, voice)
            storage_key = await self.storage.put_bytes(
                f"{pmid}/{t_hash[:8]}/{voice}", result["audio_bytes"], result["content_type"]
            )
            file_size = len(result["audio_bytes"])
            
            await self.db.article_audio_summaries.update_one(
                {"pmid": pmid, "voice": voice, "text_hash": t_hash},
                {"$set": {
                    "status": "ready",
                    "storage_key": storage_key,
                    "storage_backend": current_storage_backend,  # Step 16: Track storage backend
                    "file_size_bytes": file_size,
                    "duration_seconds": result["duration_seconds"],
                    "audio_format": result.get("format", "wav"),
                    "audio_content_type": result.get("content_type", "audio/wav"),
                    "error_code": None,
                    "error_message": None,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }},
            )
            # Record usage event with pmid for audio history
            await self.db.user_usage_events.insert_one({
                "event_id": str(uuid.uuid4()),
                "user_id": user_id,
                "event_type": "audio_generate",
                "pmid": pmid,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            # Track for analytics dashboard
            from utils.event_tracker import track_event
            await track_event("audio_generated", user_id, {"pmid": pmid})
        except RuntimeError as e:
            error_code = str(e) if str(e) in ("tts_not_configured",) else "generation_failed"
            logger.error("Audio generation failed for pmid=%s: %s", pmid, error_code)
            from utils.event_tracker import track_event as _track_fail
            await _track_fail("audio_generation_failed", user_id, {"pmid": pmid, "error": error_code})
            await self.db.article_audio_summaries.update_one(
                {"pmid": pmid, "voice": voice, "text_hash": t_hash},
                {"$set": {
                    "status": "failed",
                    "error_code": error_code,
                    "error_message": "TTS provider not configured" if error_code == "tts_not_configured" else "Generation failed",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }},
            )
        except Exception as e:
            # Log the real exception (type + message + traceback) so failures
            # are diagnosable. Store a short, non-secret detail on the record.
            logger.error(
                "Audio generation failed for pmid=%s: %s: %s",
                pmid, type(e).__name__, str(e), exc_info=True,
            )
            await self.db.article_audio_summaries.update_one(
                {"pmid": pmid, "voice": voice, "text_hash": t_hash},
                {"$set": {
                    "status": "failed",
                    "error_code": "generation_failed",
                    "error_message": f"{type(e).__name__}: {str(e)[:300]}",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }},
            )

        return await self.get_audio_status(pmid) or {"status": "failed"}

    async def generate_tts_audio(self, script: str, user_id: str) -> Optional[Dict]:
        """
        Generate TTS audio from raw text (used by combined audio summaries).
        Returns {audio_url, storage_key, duration_seconds} or None on failure.
        Reuses the same TTS provider + storage backend as article audio.
        """
        t_hash = text_hash(script)
        voice = os.environ.get("OPENAI_TTS_VOICE", "default")
        storage_key_prefix = f"combined/{user_id[:8]}/{t_hash[:8]}"

        try:
            result = await self.tts.synthesize(script, voice)
            storage_key = await self.storage.put_bytes(
                storage_key_prefix, result["audio_bytes"], result["content_type"]
            )
            audio_url = await self.storage.get_url(storage_key)
            # Record usage event
            await self.db.user_usage_events.insert_one({
                "event_id": str(uuid.uuid4()),
                "user_id": user_id,
                "event_type": "audio_generate",
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            return {
                "audio_url": audio_url,
                "storage_key": storage_key,
                "duration_seconds": result.get("duration_seconds"),
            }
        except Exception as e:
            logger.error("Combined TTS generation failed for user=%s: %s", user_id, type(e).__name__)
            return None

    async def get_bytes_for_record(self, record: dict):
        """Fetch raw audio bytes for a ready record (used for ZIP download).
        Returns bytes or None if unavailable.
        """
        if not record or not record.get("storage_key"):
            return None
        storage_backend = record.get("storage_backend", "local")
        try:
            if storage_backend == "s3":
                s3 = self._get_s3_storage()
                if s3:
                    return await s3.get_file_bytes(record["storage_key"])
                return await self._local_storage.get_file_bytes(record["storage_key"])
            else:
                return await self._local_storage.get_file_bytes(record["storage_key"])
        except Exception:
            return None

    async def get_playlist_items(self, pmids: list, base_url: str = "") -> list:
        items = []
        for pmid in pmids:
            article = await self.db.articles.find_one({"pmid": pmid}, {"_id": 0, "pmid": 1, "title": 1})
            audio = await self.db.article_audio_summaries.find_one(
                {"pmid": pmid, "status": "ready"}, {"_id": 0}
            )
            audio_url = None
            if audio and audio.get("storage_key"):
                # Step 16: Use hybrid URL getter
                audio_url = await self.get_url_for_record(audio, base_url)
            items.append({
                "pmid": pmid,
                "title": article.get("title", "Unknown") if article else "Unknown",
                "status": audio.get("status", "missing") if audio else "missing",
                "audio_url": audio_url,
                "transcript": audio.get("transcript") if audio else None,
                "duration_seconds": audio.get("duration_seconds") if audio else None,
                "audio_format": audio.get("audio_format") if audio else None,
            })
        return items
