"""dango/config/models.py

Pydantic models for configuration validation.
"""

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class DeduplicationStrategy(str, Enum):
    """Data deduplication strategies"""

    NONE = "none"
    LATEST_ONLY = "latest_only"
    APPEND_ONLY = "append_only"
    SCD_TYPE2 = "scd_type2"


class SourceType(str, Enum):
    """Data source types — 35 source types (27 dlt verified + CSV + Local Files + REST API + dlt_native + Filesystem + PostgreSQL + sql_database + Scrapy)"""

    # Local/Custom
    CSV = "csv"
    LOCAL_FILES = "local_files"  # Unified local file source (CSV, JSON, JSONL, Parquet)
    REST_API = "rest_api"
    DLT_NATIVE = "dlt_native"  # Advanced: Direct dlt source bypass
    FILESYSTEM = "filesystem"  # dlt core built-in file source

    # Marketing & Analytics (7)
    FACEBOOK_ADS = "facebook_ads"
    GOOGLE_ADS = "google_ads"
    GOOGLE_ANALYTICS = "google_analytics"
    GOOGLE_SHEETS = "google_sheets"
    MATOMO = "matomo"
    MUX = "mux"
    AIRTABLE = "airtable"

    # Business & CRM (8)
    HUBSPOT = "hubspot"
    SALESFORCE = "salesforce"
    PIPEDRIVE = "pipedrive"
    FRESHDESK = "freshdesk"
    ZENDESK = "zendesk"
    ASANA = "asana"
    JIRA = "jira"
    WORKABLE = "workable"

    # E-commerce & Payment (2)
    SHOPIFY = "shopify"
    STRIPE = "stripe"

    # Files & Storage (3) - Airtable/Sheets already in Marketing
    NOTION = "notion"
    INBOX = "inbox"

    # Databases (3)
    MONGODB = "mongodb"
    POSTGRESQL = "postgres"
    SQL_DATABASE = "sql_database"  # Generic for 24 SQL databases via dlt

    # Streaming (2)
    APACHE_KAFKA = "kafka"
    AMAZON_KINESIS = "kinesis"

    # Development (1)
    GITHUB = "github"

    # Other (5)
    SLACK = "slack"
    CHESS = "chess"
    SCRAPY = "scrapy"
    STRAPI = "strapi"
    PERSONIO = "personio"


class Stakeholder(BaseModel):
    """Project stakeholder"""

    name: str
    role: str
    contact: str


class ProjectContext(BaseModel):
    """Project-level context and metadata"""

    name: str
    organization: str | None = Field(
        None, description="Organization name (used in Metabase, Web UI, etc.)"
    )
    dango_version: str | None = Field(
        None, description="Version of Dango used to create this project"
    )
    created: datetime = Field(default_factory=datetime.now)
    created_by: str

    purpose: str = Field(description="Why this project exists, what it's used for")

    stakeholders: list[Stakeholder] = Field(default_factory=list)

    sla: str | None = Field(
        None, description="Data freshness SLA (e.g., 'Daily by 9am', 'Real-time')"
    )

    limitations: str | None = Field(None, description="Known limitations, caveats, or gotchas")

    getting_started: str | None = Field(None, description="Quick start guide for new team members")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Shopee Analytics",
                "created_by": "Aaron Teoh <aaron@company.com>",
                "purpose": "Track daily sales performance and customer behavior",
                "stakeholders": [
                    {
                        "name": "Sarah Chen",
                        "role": "CMO - Primary dashboard user",
                        "contact": "sarah@company.com",
                    }
                ],
                "sla": "Daily by 9am SGT",
                "limitations": "Shopify data has 24h delay. Stripe doesn't include refunds.",
                "getting_started": "Run 'dango sync' to refresh data, then open http://dango.local",
            }
        }
    )


class CSVSourceConfig(BaseModel):
    """CSV file source configuration"""

    directory: Path = Field(description="Directory containing CSV files")
    file_pattern: str = Field(default="*.csv", description="Glob pattern for CSV files")
    deduplication_strategy: DeduplicationStrategy = Field(
        default=DeduplicationStrategy.LATEST_ONLY,
        description="Deduplication strategy: none, latest_only, append_only, scd_type2",
    )
    primary_key: str | None = Field(
        default=None, description="Primary key column for deduplication"
    )
    timestamp_column: str | None = Field(
        default=None, description="Timestamp column for latest_only/scd_type2 deduplication"
    )
    timestamp_sort: str | None = Field(
        default="desc",
        description="Sort order for timestamp: 'desc' (latest first) or 'asc' (oldest first)",
    )
    notes: str | None = Field(
        default=None,
        description="Optional notes about how to regenerate this CSV data (e.g., script to run, export steps)",
    )


class LocalFilesSourceConfig(CSVSourceConfig):
    """Local files source — extends CSV with multi-format support (CSV, JSON, JSONL, Parquet)."""

    # Override default: match all supported formats instead of just *.csv
    file_pattern: str = Field(default="*", description="Glob pattern for files")


class GoogleSheetsSourceConfig(BaseModel):
    """Google Sheets source configuration"""

    spreadsheet_url_or_id: str  # Spreadsheet ID or full URL
    range_names: list[str]  # Sheet/tab names to load (each becomes a table)
    deduplication: DeduplicationStrategy = DeduplicationStrategy.LATEST_ONLY

    @field_validator("range_names", mode="before")
    @classmethod
    def ensure_list(cls, v: Any) -> Any:
        """Convert single string to list for backward compatibility"""
        if isinstance(v, str):
            return [v]
        return v


class StripeSourceConfig(BaseModel):
    """Stripe API source configuration"""

    stripe_secret_key_env: str = Field(
        default="STRIPE_API_KEY", description="Environment variable containing Stripe secret key"
    )
    endpoints: list[str] | None = Field(
        default=None, description="Stripe endpoints to sync (None = all default endpoints)"
    )
    start_date: datetime | None = None
    end_date: datetime | None = None


class FacebookAdsSourceConfig(BaseModel):
    """Facebook Ads API source configuration"""

    account_id: str = Field(description="Facebook Ads Account ID (e.g., 'act_123456789')")
    access_token_env: str = Field(
        default="FB_ACCESS_TOKEN", description="Environment variable containing access token"
    )
    initial_load_past_days: int = Field(
        default=30, description="Days of historical performance metrics to load on first sync"
    )
    start_date: datetime | None = Field(
        default=None, description="Start date for data extraction (YYYY-MM-DD)"
    )
    resources: list[str] | None = Field(
        default=None, description="Facebook Ads resources to sync (None = all)"
    )


class GoogleAnalyticsSourceConfig(BaseModel):
    """Google Analytics API source configuration"""

    property_id: str = Field(description="GA4 property ID")
    credentials_env: str = Field(
        default="GOOGLE_CREDENTIALS",
        description="Environment variable containing Google service account JSON or OAuth credentials",
    )
    start_date: str | None = Field(
        default=None,
        description="Start date for data extraction (YYYY-MM-DD or relative like '90daysAgo')",
    )


class HubSpotSourceConfig(BaseModel):
    """HubSpot API source configuration"""

    api_key_env: str = Field(
        default="HUBSPOT_API_KEY", description="Environment variable containing API key"
    )
    resources: list[str] = Field(
        default=["contacts", "companies", "deals", "tickets"],
        description="HubSpot resources to sync",
    )


class SalesforceSourceConfig(BaseModel):
    """Salesforce API source configuration"""

    username_env: str = Field(
        default="",
        description="(Deprecated) Environment variable containing username. Use OAuth 2.0 via secrets.toml instead.",
    )
    password_env: str = Field(
        default="",
        description="(Deprecated) Environment variable containing password. Use OAuth 2.0 via secrets.toml instead.",
    )
    security_token_env: str = Field(
        default="",
        description="(Deprecated) Environment variable containing security token. Use OAuth 2.0 via secrets.toml instead.",
    )
    resources: list[str] | None = Field(
        default=None, description="Salesforce resources to sync (None = all)"
    )


class GitHubSourceConfig(BaseModel):
    """GitHub API source configuration"""

    access_token_env: str = Field(
        default="GITHUB_ACCESS_TOKEN",
        description="Environment variable containing personal access token",
    )
    owner: str = Field(description="GitHub username or organization that owns the repository")
    name: str = Field(description="Repository name to load data from")


class SlackSourceConfig(BaseModel):
    """Slack API source configuration"""

    model_config = ConfigDict(populate_by_name=True)

    access_token_env: str = Field(
        default="SLACK_ACCESS_TOKEN",
        description="Environment variable containing Slack bot token",
        validation_alias=AliasChoices("access_token_env", "token_env"),
    )
    selected_channels: list[str] | None = Field(
        default=None,
        description="List of channel IDs to sync (None = all channels)",
        validation_alias=AliasChoices("selected_channels", "channels"),
    )
    start_date: datetime | None = Field(default=None, description="Start date for message history")


class RESTAPISourceConfig(BaseModel):
    """Generic REST API source configuration (for custom APIs)"""

    base_url: str = Field(description="Base URL for the API")
    endpoints: list[dict[str, Any]] = Field(
        description="List of endpoints to sync with their configurations"
    )
    auth_type: str | None = Field(
        default="bearer", description="Authentication type: bearer, api_key, basic, or none"
    )
    auth_token_env: str | None = Field(
        default=None, description="Environment variable containing auth token/key"
    )
    api_key_name: str | None = Field(
        default=None, description="Header or query parameter name for API key auth"
    )
    api_key_location: str | None = Field(
        default=None, description="Where to send the API key: 'header' or 'query'"
    )
    basic_username_env: str | None = Field(
        default=None, description="Environment variable for HTTP Basic username"
    )
    basic_password_env: str | None = Field(
        default=None, description="Environment variable for HTTP Basic password"
    )
    access_token_url: str | None = Field(
        default=None, description="OAuth2 client credentials token endpoint URL"
    )
    client_id_env: str | None = Field(
        default=None, description="Environment variable for OAuth2 client ID"
    )
    client_secret_env: str | None = Field(
        default=None, description="Environment variable for OAuth2 client secret"
    )
    headers: dict[str, str] | None = Field(
        default=None, description="Additional headers to include in requests"
    )


class DltNativeConfig(BaseModel):
    """
    Advanced: Direct dlt source configuration (registry bypass)

    For dlt sources not in Dango's registry, or for advanced users who want
    full control over dlt source configuration.

    Users can:
    1. Place custom dlt source files in custom_sources/ directory
    2. Configure source parameters directly in sources.yml
    3. Use any dlt verified source or custom source

    Example sources.yml:
        sources:
          - name: my_custom_source
            type: dlt_native
            dlt_native:
              source_module: "my_source"  # custom_sources/my_source.py
              source_function: "my_source_func"
              function_kwargs:
                api_key_env: "MY_API_KEY"
                endpoint: "https://api.example.com"
    """

    source_module: str = Field(
        description="Python module name (from custom_sources/ directory or dlt package name)"
    )
    source_function: str = Field(
        description="Function name to call for source (e.g., 'google_ads', 'my_custom_source')"
    )
    function_kwargs: dict[str, Any] = Field(
        default_factory=dict, description="Keyword arguments to pass to source function"
    )
    pipeline_name: str | None = Field(
        default=None, description="Custom pipeline name (defaults to source name)"
    )
    dataset_name: str | None = Field(
        default=None, description="Custom dataset name (defaults to source name)"
    )


class DataSource(BaseModel):
    """Data source definition"""

    name: str = Field(description="Unique source name (used as table prefix)")
    type: SourceType
    enabled: bool = True

    # Type-specific configs (only one should be set based on source type)
    csv: CSVSourceConfig | None = None
    local_files: LocalFilesSourceConfig | None = None
    rest_api: RESTAPISourceConfig | None = None
    dlt_native: DltNativeConfig | None = None  # Advanced: Direct dlt source

    # Marketing & Analytics
    facebook_ads: FacebookAdsSourceConfig | None = None
    google_analytics: GoogleAnalyticsSourceConfig | None = None
    google_sheets: GoogleSheetsSourceConfig | None = None

    # Business & CRM
    hubspot: HubSpotSourceConfig | None = None
    salesforce: SalesforceSourceConfig | None = None

    # E-commerce & Payment
    stripe: StripeSourceConfig | None = None

    # Development
    github: GitHubSourceConfig | None = None

    # Other
    slack: SlackSourceConfig | None = None

    # Generic config for sources without specific models yet
    # (will be used for the other 21 sources until we add their specific models)
    generic_config: dict[str, Any] | None = Field(
        default=None, description="Generic configuration for sources without dedicated models"
    )

    # Custom metadata
    description: str | None = None
    tags: list[str] = Field(default_factory=list)

    # Lookback window: on each incremental sync, re-load this many days of data
    # to pick up late-arriving records.  Ignored during full refresh.
    lookback_days: int | None = None

    @field_validator("name")
    @classmethod
    def validate_name_format(cls, v: str) -> str:
        """Ensure source name uses only letters, numbers, and underscores (no hyphens)."""
        if not v or not v.replace("_", "").isalnum():
            raise ValueError(
                f"Source name '{v}' is invalid. Use only lowercase letters, numbers, and underscores (no hyphens)."
            )
        return v.lower()  # Also enforce lowercase


class SourcesConfig(BaseModel):
    """sources.yml configuration"""

    version: str = "1.0"
    sources: list[DataSource] = Field(default_factory=list)

    def get_source(self, name: str) -> DataSource | None:
        """Get source by name"""
        for source in self.sources:
            if source.name == name:
                return source
        return None

    def get_enabled_sources(self) -> list[DataSource]:
        """Get all enabled sources"""
        return [s for s in self.sources if s.enabled]


class PlatformSettings(BaseModel):
    """Platform configuration settings"""

    duckdb_path: str = "./data/warehouse.duckdb"
    dbt_project_dir: str = "./dbt"
    data_dir: str = "./data"

    # Web UI port (change if you have a conflict)
    port: int = Field(
        default=8800, description="Port for Web UI and API (e.g., http://localhost:8800)"
    )

    # Metabase port (change if you have a conflict)
    metabase_port: int = Field(
        default=3000, description="Port for Metabase BI dashboard (e.g., http://localhost:3000)"
    )

    # dbt docs port (change if you have a conflict)
    dbt_docs_port: int = Field(
        default=8081, description="Port for dbt documentation (e.g., http://localhost:8081)"
    )

    # Marimo notebooks port (change if you have a conflict)
    marimo_port: int = Field(
        default=7805, description="Port for Marimo notebooks (e.g., http://localhost:7805)"
    )

    # Auto-trigger settings
    auto_sync: bool = True
    auto_dbt: bool = True
    debounce_seconds: int = 600  # 10 minutes

    # Watch patterns
    watch_patterns: list[str] = Field(
        default_factory=lambda: ["*.csv", "*.json", "*.jsonl", "*.ndjson", "*.parquet"]
    )

    # Watch directories (relative to project root)
    watch_directories: list[str] = Field(default_factory=lambda: ["data/uploads"])


class RateLimitGroupConfig(BaseModel):
    """Rate limit settings for a single route group."""

    requests: int = Field(description="Max requests allowed in the window")
    window_seconds: int = Field(default=60, description="Sliding window duration in seconds")


class RateLimitConfig(BaseModel):
    """Rate limiting configuration."""

    enabled: bool = Field(default=True, description="Enable rate limiting")
    login: RateLimitGroupConfig = Field(default_factory=lambda: RateLimitGroupConfig(requests=10))
    api: RateLimitGroupConfig = Field(default_factory=lambda: RateLimitGroupConfig(requests=200))
    trusted_proxies: list[str] = Field(
        default_factory=list,
        description="IPs of trusted reverse proxies for X-Forwarded-For extraction",
    )


class AccountLockoutConfig(BaseModel):
    """Account lockout configuration."""

    max_attempts: int = Field(default=5, description="Failed attempts before lockout")
    lockout_minutes: int = Field(default=15, description="Lockout duration in minutes")


class OAuthProviderConfig(BaseModel):
    """Credentials for a single OAuth login provider."""

    client_id: str = Field(description="OAuth client ID")
    client_secret: str = Field(description="OAuth client secret")


class AuthConfig(BaseModel):
    """Authentication configuration."""

    enabled: bool = Field(default=True, description="Enable authentication")
    idle_timeout_minutes: int = Field(default=1440, description="Session idle timeout in minutes")
    session_max_days: int = Field(default=365, description="Maximum session lifetime in days")
    require_2fa: bool = Field(default=False, description="Require all users to set up 2FA")
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    lockout: AccountLockoutConfig = Field(default_factory=AccountLockoutConfig)
    oauth_providers: dict[str, OAuthProviderConfig] = Field(
        default_factory=dict,
        description="OAuth login providers keyed by name (google, github)",
    )


class SpacesConfig(BaseModel):
    """DigitalOcean Spaces backup configuration."""

    bucket: str = Field(description="Spaces bucket name")
    region: str | None = Field(
        default=None, description="Spaces region (defaults to droplet region)"
    )
    access_key_env: str = Field(
        default="SPACES_ACCESS_KEY",
        description="Environment variable name for Spaces access key",
    )
    secret_key_env: str = Field(
        default="SPACES_SECRET_KEY",
        description="Environment variable name for Spaces secret key",
    )


class DbtOverrides(BaseModel):
    """Cloud-specific dbt configuration overrides."""

    threads: int | None = Field(
        default=None, description="Override dbt threads (default: number of vCPUs)"
    )
    memory_limit: str | None = Field(
        default=None, description="Override DuckDB memory limit (default: 25% of RAM)"
    )


class CloudConfig(BaseModel):
    """Cloud deployment configuration stored in .dango/cloud.yml."""

    droplet_id: int | None = Field(
        default=None, description="DigitalOcean droplet ID (set after provisioning)"
    )
    droplet_ip: str | None = Field(
        default=None, description="Droplet public IP address (set after provisioning)"
    )
    firewall_id: str | None = Field(
        default=None, description="DigitalOcean firewall ID (set after provisioning)"
    )
    region: str = Field(default="nyc1", description="DigitalOcean region")
    size: str = Field(default="s-2vcpu-4gb", description="Droplet size slug")
    domain: str | None = Field(default=None, description="Custom domain name")
    spaces: SpacesConfig | None = Field(default=None, description="Spaces backup configuration")
    ssh_key_path: str = Field(default=".dango/cloud_key", description="Path to SSH private key")
    ssh_key_id: int | None = Field(
        default=None, description="DigitalOcean SSH key ID (set after provisioning)"
    )
    dbt_overrides: DbtOverrides | None = Field(
        default=None, description="Cloud dbt configuration overrides"
    )


class DangoConfig(BaseModel):
    """Complete Dango project configuration"""

    project: ProjectContext
    sources: SourcesConfig = Field(default_factory=SourcesConfig)

    # Platform settings
    platform: PlatformSettings = Field(default_factory=PlatformSettings)

    # Authentication settings
    auth: AuthConfig = Field(default_factory=AuthConfig)
