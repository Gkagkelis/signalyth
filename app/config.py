from pathlib import Path
import os
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[1]
RUNNING_ON_VERCEL = bool(os.environ.get("VERCEL"))
DEFAULT_DATA_DIR = Path("/tmp/signalyth-data") if RUNNING_ON_VERCEL else (BASE_DIR / "data")
DEFAULT_RUNTIME_CONFIG_DIR = Path("/tmp/signalyth-config") if RUNNING_ON_VERCEL else (BASE_DIR / "config")
# Keep user credentials outside the versioned app folder so upgrades/relocations
# do not lose them. SIGNALYTH_HOME can override this location when needed.
USER_DATA_DIR = Path(os.environ.get("SIGNALYTH_HOME", "")).expanduser() if os.environ.get("SIGNALYTH_HOME") else ((Path("/tmp") / ".signalyth") if RUNNING_ON_VERCEL else (Path.home() / ".signalyth"))
PERSISTENT_SECRETS_FILE = USER_DATA_DIR / "secrets.env"
LEGACY_SECRETS_FILE = BASE_DIR / "config" / "secrets.env"

class Settings(BaseSettings):
    apify_token: str = ""
    signalyth_data_dir: str = str(DEFAULT_DATA_DIR)
    signalyth_runtime_config_dir: str = str(DEFAULT_RUNTIME_CONFIG_DIR)
    signalyth_cloud_storage: bool = RUNNING_ON_VERCEL
    signalyth_execution_backend: str = "celery" if RUNNING_ON_VERCEL else "thread"
    # Serverless invocations are hard-killed at vercel.json maxDuration. The pipeline
    # must therefore stop at a SOFT deadline, persist its durable state and requeue a
    # continuation instead of dying mid-phase with an eternally "running" status.
    # 0 disables the soft deadline (local/long-lived workers).
    signalyth_worker_soft_deadline_seconds: int = 240 if RUNNING_ON_VERCEL else 0
    # A run whose status is "running" but whose status.json has not been updated for
    # this long is treated as orphaned by a killed worker and may be resumed.
    signalyth_stale_running_after_seconds: int = 600
    # New collection sources are not started when less than this many seconds
    # remain before the soft deadline, so a typical source (subruns + persist)
    # finishes before Vercel's hard kill and the run continues in a new worker.
    signalyth_collection_deadline_margin_seconds: int = 110
    # Number of collection sources allowed to run at the same time. Sources are
    # independent in fixed per-source plans; they share ONE atomic budget guard
    # and one locked status writer. Automatic elastic rebalancing always runs
    # sequentially regardless of this value. 1 = classic sequential behavior.
    signalyth_collection_parallel_sources: int = 3 if RUNNING_ON_VERCEL else 1
    signalyth_dry_run: bool = True
    signalyth_max_parallel_runs: int = 2
    openai_api_key: str = ""
    signalyth_ai_enabled: bool = False
    signalyth_ai_bulk_model: str = "gpt-5.6-luna"
    signalyth_ai_reasoning_model: str = "gpt-5.6-terra"
    signalyth_ai_batch_size: int = 12
    # Number of concurrent OpenAI batch requests. All parallel batches draw from
    # ONE shared, lock-protected budget (atomic reserve/settle), so parallelism
    # can never jointly exceed the hard AI cost guard. 1 = sequential.
    signalyth_ai_parallel_requests: int = 3
    signalyth_ai_max_text_chars: int = 6000
    signalyth_ai_max_output_tokens: int = 7000
    signalyth_ai_max_cost_usd: float = 3.0
    signalyth_ai_bulk_input_usd_per_mtok: float = 0.20
    signalyth_ai_bulk_output_usd_per_mtok: float = 1.20
    signalyth_ai_reasoning_input_usd_per_mtok: float = 2.00
    signalyth_ai_reasoning_output_usd_per_mtok: float = 12.00
    signalyth_allow_remote_secret_setup: bool = False
    model_config = SettingsConfigDict(
        env_file=(str(BASE_DIR / ".env"), str(LEGACY_SECRETS_FILE), str(PERSISTENT_SECRETS_FILE)),
        env_ignore_empty=True,
        extra="ignore",
    )

settings = Settings()
