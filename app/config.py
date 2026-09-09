"""Configuration, read once from the environment.

Secrets may be supplied either directly (``X``) or via a file path (``X_FILE``),
so docker-compose ``secrets:`` mounts work without putting values in the env.
"""
import os
from dataclasses import dataclass, field


def _read(name: str, default: str = "") -> str:
    path = os.environ.get(f"{name}_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return default
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


@dataclass
class Settings:
    # --- TigerBay (Nimble API) -------------------------------------------
    tigerbay_base_url: str = field(default_factory=lambda: _read("TIGERBAY_BASE_URL").rstrip("/"))
    tigerbay_token_url: str = field(default_factory=lambda: _read("TIGERBAY_OAUTH_TOKEN_URL"))
    tigerbay_client_id: str = field(default_factory=lambda: _read("TIGERBAY_CLIENT_ID"))
    tigerbay_client_secret: str = field(default_factory=lambda: _read("TIGERBAY_CLIENT_SECRET"))
    tigerbay_timeout: float = field(default_factory=lambda: _float("TIGERBAY_TIMEOUT", 30.0))

    # --- HubSpot -----------------------------------------------------------
    hubspot_token: str = field(default_factory=lambda: _read("HUBSPOT_ACCESS_TOKEN"))
    hubspot_base_url: str = field(default_factory=lambda: _read("HUBSPOT_BASE_URL", "https://api.hubapi.com").rstrip("/"))
    hubspot_timeout: float = field(default_factory=lambda: _float("HUBSPOT_TIMEOUT", 30.0))
    # Requests per 10s window we allow ourselves (private app burst limit is 100-190).
    hubspot_rate_per_10s: int = field(default_factory=lambda: _int("HUBSPOT_RATE_PER_10S", 80))
    # "flag" => set tigerbay_archived=true on the HubSpot record; "delete" => archive the record.
    hubspot_archive_action: str = field(default_factory=lambda: _read("HUBSPOT_ARCHIVE_ACTION", "flag").lower())
    # Lifecycle stage applied ONLY when a contact is first created (blank = leave HubSpot default).
    hubspot_customer_lifecycle: str = field(default_factory=lambda: _read("HUBSPOT_CUSTOMER_LIFECYCLE", "lead"))
    hubspot_staff_lifecycle: str = field(default_factory=lambda: _read("HUBSPOT_STAFF_LIFECYCLE", "lead"))
    # Sync the parent agency as a HubSpot company and associate staff contacts to it.
    sync_agent_companies: bool = field(default_factory=lambda: _bool("SYNC_AGENT_COMPANIES", False))
    # Never create a HubSpot contact without an email address (name-only contacts are
    # unusable for marketing and prone to duplication). Existing records still update.
    require_email_for_create: bool = field(default_factory=lambda: _bool("REQUIRE_EMAIL_FOR_CREATE", True))
    # Dry-run: compute diffs and log them, never write to HubSpot.
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", False))
    # Safety net: non-production TigerBay tenants hold ANONYMISED data (hashed names,
    # @tigerbay.co.uk emails). Writing that into the live HubSpot would destroy real
    # contact data, so writes are forced to dry-run unless this is explicitly set.
    allow_writes_from_nonprod_tigerbay: bool = field(
        default_factory=lambda: _bool("ALLOW_WRITES_FROM_NONPROD_TIGERBAY", False))

    def tigerbay_is_nonprod(self) -> bool:
        host = self.tigerbay_base_url.lower()
        return any(tag in host for tag in ("preproduction", "preprod", "preview", "candidate", "staging", "test"))

    def effective_dry_run(self) -> bool:
        if self.dry_run:
            return True
        return self.tigerbay_is_nonprod() and not self.allow_writes_from_nonprod_tigerbay

    # --- Inbound webhook auth (fail closed: at least one must be set) ------
    webhook_basic_user: str = field(default_factory=lambda: _read("WEBHOOK_BASIC_USER"))
    webhook_basic_password: str = field(default_factory=lambda: _read("WEBHOOK_BASIC_PASSWORD"))
    webhook_header_name: str = field(default_factory=lambda: _read("WEBHOOK_HEADER_NAME", "X-Webhook-Token"))
    webhook_header_value: str = field(default_factory=lambda: _read("WEBHOOK_HEADER_VALUE"))

    # --- Admin API auth -----------------------------------------------------
    admin_token: str = field(default_factory=lambda: _read("ADMIN_TOKEN"))

    # --- Runtime -------------------------------------------------------------
    db_path: str = field(default_factory=lambda: _read("DB_PATH", "./data/beachsync.db"))
    worker_enabled: bool = field(default_factory=lambda: _bool("WORKER_ENABLED", True))
    worker_poll_seconds: float = field(default_factory=lambda: _float("WORKER_POLL_SECONDS", 2.0))
    max_attempts: int = field(default_factory=lambda: _int("MAX_ATTEMPTS", 8))
    log_level: str = field(default_factory=lambda: _read("LOG_LEVEL", "INFO").upper())

    def webhook_auth_configured(self) -> bool:
        return bool((self.webhook_basic_user and self.webhook_basic_password) or self.webhook_header_value)


settings = Settings()
