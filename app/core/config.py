"""环境配置；生产模式拒绝本地演示依赖和弱认证。"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEVELOPMENT_JWT_SECRET = "local-development-only-change-before-production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True,
                                     hide_input_in_errors=True)

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite+aiosqlite:///./lesson_assistant.db"
    task_queue_mode: Literal["inline", "arq"] = "inline"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: str = DEVELOPMENT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = Field(default=480, ge=1, le=10080)
    jwt_issuer: str = "lesson-assistant"
    ai_provider: str = Field(default="mock", validation_alias=AliasChoices("AI_PROVIDER", "LESSON_AI_PROVIDER", "llm_provider", "ai_provider"))
    llm_base_url: str = Field(default="", validation_alias=AliasChoices("LLM_BASE_URL", "LESSON_AI_LLM_BASE_URL", "llm_base_url"))
    llm_api_key: str = Field(default="", validation_alias=AliasChoices("LLM_API_KEY", "LESSON_AI_LLM_API_KEY", "llm_api_key"))
    llm_model: str = Field(default="", validation_alias=AliasChoices("LLM_MODEL", "LESSON_AI_LLM_MODEL", "llm_model"))
    llm_one_click_model: str = Field(default="", validation_alias=AliasChoices("LLM_ONE_CLICK_MODEL", "LESSON_AI_LLM_ONE_CLICK_MODEL", "llm_one_click_model"))
    llm_structured_model: str = Field(default="", validation_alias=AliasChoices("LLM_STRUCTURED_MODEL", "LESSON_AI_LLM_STRUCTURED_MODEL", "llm_structured_model"))
    llm_module_operation_model: str = Field(default="", validation_alias=AliasChoices("LLM_MODULE_OPERATION_MODEL", "LESSON_AI_LLM_MODULE_OPERATION_MODEL", "llm_module_operation_model"))
    llm_review_model: str = Field(default="", validation_alias=AliasChoices("LLM_REVIEW_MODEL", "LESSON_AI_LLM_REVIEW_MODEL", "llm_review_model"))
    llm_fallback_model: str = ""
    llm_fallback_base_url: str = ""
    llm_fallback_api_key: str = ""
    llm_timeout_seconds: float = Field(default=120, gt=0, le=240)
    llm_max_tokens: int = Field(default=16000, ge=100, le=100000)
    storage_backend: Literal["local", "cos"] = "local"
    storage_root: Path = Field(default=Path("var/storage"), validation_alias=AliasChoices("STORAGE_ROOT", "STORAGE_PATH", "storage_root", "storage_path"))
    cos_bucket: str = ""
    cos_region: str = ""
    cos_secret_id: str = ""
    cos_secret_key: str = ""
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    internal_review_enabled: bool = False
    frontend_dist: Path = Path("frontend/dist")
    upload_max_bytes: int = Field(default=20 * 1024 * 1024, ge=1024)
    max_parallel_uploads: int = Field(default=4, ge=1, le=32)
    request_body_timeout_seconds: float = Field(default=60, ge=1, le=300)
    run_timeout_seconds: int = Field(default=300, ge=5, le=3600)
    quality_retry_limit: int = Field(default=1, ge=0, le=3)
    worker_lease_seconds: int = Field(default=30, ge=2, le=300)
    worker_heartbeat_seconds: float = Field(default=10, ge=0.1, le=120)
    worker_recovery_limit: int = Field(default=3, ge=0, le=10)
    dispatch_max_attempts: int = Field(default=6, ge=1, le=100)
    dispatch_retry_base_seconds: float = Field(default=2, gt=0, le=120)
    dispatch_retry_max_seconds: float = Field(default=60, gt=0, le=3600)
    dispatch_timeout_seconds: float = Field(default=5, gt=0, le=60)
    dispatch_deadline_seconds: int = Field(default=300, ge=1, le=86400)
    queue_backlog_seconds: int = Field(default=60, ge=1, le=86400)
    knowledge_search_max_chunks: int = Field(default=10000, ge=100, le=100000)
    registration_enabled: bool = True
    langfuse_enabled: bool = False
    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr = SecretStr("")
    langfuse_base_url: str = ""
    langfuse_project: str = ""
    obs_deployment_id: str = "lesson-assistant-local"
    obs_release: str = ""
    obs_capture_mode: Literal["metadata", "full"] = "metadata"
    obs_max_content_bytes: int = Field(default=65536, ge=1024, le=1048576)
    obs_trace_sample_rate: float = Field(default=1, ge=0, le=1)
    obs_export_queue_size: int = Field(default=512, ge=1, le=10000)
    obs_max_event_bytes: int = Field(default=16384, ge=1024, le=65536)
    obs_export_timeout_seconds: float = Field(default=2, gt=0, le=10)
    obs_shutdown_timeout_seconds: float = Field(default=5, ge=0, le=30)
    obs_id_hash_key: SecretStr = SecretStr("")

    @property
    def llm_provider(self) -> str:
        return self.ai_provider

    @property
    def storage_path(self) -> Path:
        return self.storage_root

    @model_validator(mode="after")
    def production_requirements(self):
        if self.dispatch_retry_max_seconds < self.dispatch_retry_base_seconds:
            raise ValueError("DISPATCH_RETRY_MAX_SECONDS must be at least DISPATCH_RETRY_BASE_SECONDS")
        if self.worker_heartbeat_seconds >= self.worker_lease_seconds:
            raise ValueError("WORKER_HEARTBEAT_SECONDS must be shorter than WORKER_LEASE_SECONDS")
        if self.jwt_algorithm != "HS256":
            raise ValueError("JWT_ALGORITHM must be HS256")
        if self.ai_provider not in {"mock", "openai", "lesson_plan_ai"}:
            raise ValueError("AI_PROVIDER must be mock, openai or lesson_plan_ai")
        if self.environment == "production":
            violations = []
            if not self.database_url.startswith("postgresql+asyncpg://"):
                violations.append("DATABASE_URL must use PostgreSQL asyncpg")
            if self.task_queue_mode != "arq":
                violations.append("TASK_QUEUE_MODE must be arq")
            if self.ai_provider == "mock" or not self.llm_base_url or not self.llm_api_key:
                violations.append("real LLM provider, base URL and API key are required")
            if not (self.llm_model or self.llm_one_click_model):
                violations.append("LLM_MODEL or LLM_ONE_CLICK_MODEL is required")
            for route in ("structured", "module_operation", "review"):
                if not (self.llm_model or getattr(self, f"llm_{route}_model")):
                    violations.append(f"LLM_MODEL or LLM_{route.upper()}_MODEL is required")
            if len(self.jwt_secret) < 32 or self.jwt_secret == DEVELOPMENT_JWT_SECRET:
                violations.append("JWT_SECRET must be a strong non-default secret of at least 32 characters")
            if "*" in self.cors_origins:
                violations.append("CORS_ORIGINS cannot contain wildcard in production")
            if violations:
                raise ValueError("Invalid production configuration: " + "; ".join(violations))
        if self.storage_backend == "cos" and not all((self.cos_bucket, self.cos_region, self.cos_secret_id, self.cos_secret_key)):
            raise ValueError("COS storage requires bucket, region and credentials")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
