"""
Dango Config Models

Pydantic models for configuration validation.
"""

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, List, Dict, Any

from pydantic import BaseModel, Field, validator


class DeduplicationStrategy(str, Enum):
    """Data deduplication strategies"""
    NONE = "none"
    LATEST_ONLY = "latest_only"
    APPEND_ONLY = "append_only"
    SCD_TYPE2 = "scd_type2"


class SourceType(str, Enum):
    """Data source types - covers all 31 dlt verified sources + CSV + REST API"""

    # Local/Custom
    CSV = "csv"
    REST_API = "rest_api"
    DLT_NATIVE = "dlt_native"  # Advanced: Direct dlt source bypass

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
    organization: Optional[str] = Field(
        None,
        description="Organization name (used in Metabase, Web UI, etc.)"
    )
    dango_version: Optional[str] = Field(
        None,
        description="Version of Dango used to create this project"
    )
    created: datetime = Field(default_factory=datetime.now)
    created_by: str

    purpose: str = Field(description="Why this project exists, what it's used for")

    stakeholders: List[Stakeholder] = Field(default_factory=list)

    sla: Optional[str] = Field(
        None,
        description="Data freshness SLA (e.g., 'Daily by 9am', 'Real-time')"
    )

    limitations: Optional[str] = Field(
        None,
        description="Known limitations, caveats, or gotchas"
    )

    getting_started: Optional[str] = Field(
        None,
        description="Quick start guide for new team members"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "name": "Shopee Analytics",
                "created_by": "Aaron Teoh <aaron@company.com>",
                "purpose": "Track daily sales performance and customer behavior",
                "stakeholders": [
                    {
                        "name": "Sarah Chen",
                        "role": "CMO - Primary dashboard user",
                        "contact": "sarah@company.com"
                    }
                ],
                "sla": "Daily by 9am SGT",
                "limitations": "Shopify data has 24h delay. Stripe doesn't include refunds.",
                "getting_started": "Run 'dango sync' to refresh data, then open http://dango.local"
            }
        }


class CSVSourceConfig(BaseModel):
    """CSV file source configuration"""
    directory: Path = Field(description="Directory containing CSV files")
    file_pattern: str = Field(
        default="*.csv",
        description="Glob pattern for CSV files"
    )
    deduplication_strategy: DeduplicationStrategy = Field(
        default=DeduplicationStrategy.LATEST_ONLY,
        description="Deduplication strategy: none, latest_only, append_only, scd_type2"
    )
    primary_key: Optional[str] = Field(
        default=None,
        description="Primary key column for deduplication"
    )
    timestamp_column: Optional[str] = Field(
        default=None,
        description="Timestamp column for latest_only/scd_type2 deduplication"
    )
    timestamp_sort: Optional[str] = Field(
        default="desc",
        description="Sort order for timestamp: 'desc' (latest first) or 'asc' (oldest first)"
    )
    notes: Optional[str] = Field(
        default=None,
        description="Optional notes about how to regenerate this CSV data (e.g., script to run, export steps)"
    )


class GoogleSheetsSourceConfig(BaseModel):
    """Google Sheets source configuration"""
    spreadsheet_url_or_id: str  # Spreadsheet ID or full URL
    range_names: List[str]  # Sheet/tab names to load (each becomes a table)
    deduplication: DeduplicationStrategy = DeduplicationStrategy.LATEST_ONLY

    @validator('range_names', pre=True)
    def ensure_list(cls, v):
        """Convert single string to list for backward compatibility"""
        if isinstance(v, str):
            return [v]
        return v


class StripeSourceConfig(BaseModel):
    """Stripe API source configuration"""
    stripe_secret_key_env: str = Field(
        default="STRIPE_API_KEY",
        description="Environment variable containing Stripe secret key"
    )
    endpoints: Optional[List[str]] = Field(
        default=None,
        description="Stripe endpoints to sync (None = all default endpoints)"
    )
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None


class ShopifySourceConfig(BaseModel):
    """Shopify API source configuration"""
    shop_url: str = Field(description="Shopify shop URL (e.g., 'myshop.myshopify.com')")
    api_key_env: str = Field(
        default="SHOPIFY_API_KEY",
        description="Environment variable containing API key"
    )
    resources: List[str] = Field(
        default=["orders", "customers", "products"],
        description="Shopify resources to sync"
    )
    start_date: Optional[datetime] = None


class FacebookAdsSourceConfig(BaseModel):
    """Facebook Ads API source configuration"""
    account_id: str = Field(description="Facebook Ads Account ID (e.g., 'act_123456789')")
    access_token_env: str = Field(
        default="FB_ACCESS_TOKEN",
        description="Environment variable containing access token"
    )
    start_date: Optional[datetime] = Field(
        default=None,
        description="Start date for data extraction (YYYY-MM-DD)"
    )


class GoogleAnalyticsSourceConfig(BaseModel):
    """Google Analytics API source configuration"""
    property_id: str = Field(description="GA4 property ID")
    credentials_env: str = Field(
        default="GOOGLE_CREDENTIALS",
        description="Environment variable containing Google service account JSON or OAuth credentials"
    )
    start_date: Optional[datetime] = Field(
        default=None,
        description="Start date for data extraction (YYYY-MM-DD)"
    )


class HubSpotSourceConfig(BaseModel):
    """HubSpot API source configuration"""
    api_key_env: str = Field(
        default="HUBSPOT_API_KEY",
        description="Environment variable containing API key"
    )
    resources: List[str] = Field(
        default=["contacts", "companies", "deals", "tickets"],
        description="HubSpot resources to sync"
    )


class SalesforceSourceConfig(BaseModel):
    """Salesforce API source configuration"""
    username_env: str = Field(
        default="SALESFORCE_USERNAME",
        description="Environment variable containing username"
    )
    password_env: str = Field(
        default="SALESFORCE_PASSWORD",
        description="Environment variable containing password"
    )
    security_token_env: str = Field(
        default="SALESFORCE_SECURITY_TOKEN",
        description="Environment variable containing security token"
    )
    is_sandbox: bool = Field(
        default=False,
        description="Whether to use sandbox environment"
    )


class GitHubSourceConfig(BaseModel):
    """GitHub API source configuration"""
    access_token_env: str = Field(
        default="GITHUB_ACCESS_TOKEN",
        description="Environment variable containing personal access token"
    )
    repos: List[str] = Field(
        description="List of repositories to sync (format: 'owner/repo')"
    )


class SlackSourceConfig(BaseModel):
    """Slack API source configuration"""
    token_env: str = Field(
        default="SLACK_TOKEN",
        description="Environment variable containing Slack bot token"
    )
    channels: Optional[List[str]] = Field(
        default=None,
        description="List of channel IDs to sync (None = all channels)"
    )
    start_date: Optional[datetime] = Field(
        default=None,
        description="Start date for message history"
    )


class RESTAPISourceConfig(BaseModel):
    """Generic REST API source configuration (for custom APIs)"""
    base_url: str = Field(description="Base URL for the API")
    endpoints: List[Dict[str, Any]] = Field(
        description="List of endpoints to sync with their configurations"
    )
    auth_type: Optional[str] = Field(
        default="bearer",
        description="Authentication type: bearer, api_key, basic, or none"
    )
    auth_token_env: Optional[str] = Field(
        default=None,
        description="Environment variable containing auth token/key"
    )
    headers: Optional[Dict[str, str]] = Field(
        default=None,
        description="Additional headers to include in requests"
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
    function_kwargs: Dict[str, Any] = Field(
        default_factory=dict,
        description="Keyword arguments to pass to source function"
    )
    pipeline_name: Optional[str] = Field(
        default=None,
        description="Custom pipeline name (defaults to source name)"
    )
    dataset_name: Optional[str] = Field(
        default=None,
        description="Custom dataset name (defaults to source name)"
    )


class DataSource(BaseModel):
    """Data source definition"""
    name: str = Field(description="Unique source name (used as table prefix)")
    type: SourceType
    enabled: bool = True

    # Type-specific configs (only one should be set based on source type)
    csv: Optional[CSVSourceConfig] = None
    rest_api: Optional[RESTAPISourceConfig] = None
    dlt_native: Optional[DltNativeConfig] = None  # Advanced: Direct dlt source

    # Marketing & Analytics
    facebook_ads: Optional[FacebookAdsSourceConfig] = None
    google_analytics: Optional[GoogleAnalyticsSourceConfig] = None
    google_sheets: Optional[GoogleSheetsSourceConfig] = None

    # Business & CRM
    hubspot: Optional[HubSpotSourceConfig] = None
    salesforce: Optional[SalesforceSourceConfig] = None

    # E-commerce & Payment
    stripe: Optional[StripeSourceConfig] = None
    shopify: Optional[ShopifySourceConfig] = None

    # Development
    github: Optional[GitHubSourceConfig] = None

    # Other
    slack: Optional[SlackSourceConfig] = None

    # Generic config for sources without specific models yet
    # (will be used for the other 21 sources until we add their specific models)
    generic_config: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Generic configuration for sources without dedicated models"
    )

    # Custom metadata
    description: Optional[str] = None
    tags: List[str] = Field(default_factory=list)

    @validator('name')
    def validate_name_format(cls, v):
        """Ensure source name uses only letters, numbers, and underscores (no hyphens)."""
        if not v or not v.replace("_", "").isalnum():
            raise ValueError(
                f"Source name '{v}' is invalid. Use only lowercase letters, numbers, and underscores (no hyphens)."
            )
        return v.lower()  # Also enforce lowercase


class SourcesConfig(BaseModel):
    """sources.yml configuration"""
    version: str = "1.0"
    sources: List[DataSource] = Field(default_factory=list)

    def get_source(self, name: str) -> Optional[DataSource]:
        """Get source by name"""
        for source in self.sources:
            if source.name == name:
                return source
        return None

    def get_enabled_sources(self) -> List[DataSource]:
        """Get all enabled sources"""
        return [s for s in self.sources if s.enabled]


class PlatformSettings(BaseModel):
    """Platform configuration settings"""
    duckdb_path: str = "./data/warehouse.duckdb"
    dbt_project_dir: str = "./dbt"
    data_dir: str = "./data"

    # Web UI port (change if you have a conflict)
    port: int = Field(
        default=8800,
        description="Port for Web UI and API (e.g., http://localhost:8800)"
    )

    # Metabase port (change if you have a conflict)
    metabase_port: int = Field(
        default=3000,
        description="Port for Metabase BI dashboard (e.g., http://localhost:3000)"
    )

    # dbt docs port (change if you have a conflict)
    dbt_docs_port: int = Field(
        default=8081,
        description="Port for dbt documentation (e.g., http://localhost:8081)"
    )

    # Auto-trigger settings
    auto_sync: bool = True
    auto_dbt: bool = True
    debounce_seconds: int = 600  # 10 minutes

    # Watch patterns
    watch_patterns: List[str] = Field(default_factory=lambda: ["*.csv"])

    # Watch directories (relative to project root)
    watch_directories: List[str] = Field(default_factory=lambda: ["data/uploads"])


class DangoConfig(BaseModel):
    """Complete Dango project configuration"""
    project: ProjectContext
    sources: SourcesConfig = Field(default_factory=SourcesConfig)

    # Platform settings
    platform: PlatformSettings = Field(default_factory=PlatformSettings)
