from dataclasses import dataclass
import os
from pathlib import Path
from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parents[2] / ".env")


@dataclass(frozen=True)
class Settings:
    mode: str = os.getenv("SWARVAAH_MODE", "demo")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./swarvaah.db")
    admin_api_key: str = os.getenv("ADMIN_API_KEY", "change-this-before-exposing-the-api")
    call_mode: str = os.getenv("CALL_MODE", "simulation")
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
    exotel_account_sid: str = os.getenv("EXOTEL_ACCOUNT_SID", "")
    exotel_api_key: str = os.getenv("EXOTEL_API_KEY", "")
    exotel_api_token: str = os.getenv("EXOTEL_API_TOKEN", "")
    exotel_caller_id: str = os.getenv("EXOTEL_CALLER_ID", "")
    exotel_stream_url: str = os.getenv("EXOTEL_STREAM_URL", "")
    exotel_callback_secret: str = os.getenv("EXOTEL_CALLBACK_SECRET", "")
    sarvam_api_key: str = os.getenv("SARVAM_API_KEY", "")
    max_global_concurrent: int = int(os.getenv("MAX_GLOBAL_CONCURRENT", "20"))
    max_dials_per_minute: int = int(os.getenv("MAX_DIALS_PER_MINUTE", "10"))
    live_dial_enabled: bool = os.getenv("LIVE_DIAL_ENABLED", "false").lower() == "true"
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    sqs_queue_url: str = os.getenv("SQS_QUEUE_URL", "")


settings = Settings()

if settings.mode != "demo":
    if settings.admin_api_key == "change-this-before-exposing-the-api":
        raise RuntimeError("ADMIN_API_KEY must be set outside demo mode")
    if settings.database_url.startswith("sqlite"):
        raise RuntimeError("Non-demo use requires PostgreSQL")
    if settings.call_mode == "exotel" and not all(
        [settings.exotel_account_sid, settings.exotel_api_key, settings.exotel_api_token,
         settings.exotel_caller_id, settings.exotel_stream_url, settings.exotel_callback_secret,
         settings.sarvam_api_key]
    ):
        raise RuntimeError("Exotel or Sarvam configuration is incomplete")
    if settings.call_mode == "exotel" and not (
        settings.exotel_stream_url.startswith("wss://") and settings.public_base_url.startswith("https://")
    ):
        raise RuntimeError("Live calls require public HTTPS and WSS endpoints")
    if settings.call_mode == "exotel" and not settings.live_dial_enabled:
        raise RuntimeError("Set LIVE_DIAL_ENABLED=true only after provider and consent checks")
elif settings.call_mode == "exotel":
    raise RuntimeError("Live dialing cannot run in demo mode")
