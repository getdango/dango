"""dango/ingestion/sources/registry.py

Metadata registry for all 33 supported data sources (27 dlt verified + CSV + Local Files + dlt_native + REST API + PostgreSQL + Filesystem).
"""

from enum import Enum
from typing import Any, cast


class AuthType(str, Enum):
    """Authentication types for data sources"""

    NONE = "none"  # No auth needed (e.g., CSV)
    API_KEY = "api_key"  # Simple API key
    OAUTH = "oauth"  # OAuth 2.0 flow
    BASIC = "basic"  # Basic HTTP auth (username/password)
    SERVICE_ACCOUNT = "service_account"  # Service account credentials (e.g., Google)


# ============================================================================
# SOURCE SELECTION CRITERIA
# ============================================================================
#
# This registry contains 33 sources in five categories:
#   1. dlt verified sources (27): All connectors vendored in dlt_sources/. Each
#      uses its own dlt verified source package (e.g., facebook_ads, hubspot).
#   2. CSV (1): Custom CSVLoader — not dlt. For local structured CSV files.
#      Hidden from wizard — superseded by local_files.
#   3. Local Files (1): Unified local file source (CSV, JSON, JSONL, Parquet).
#      Extends CSVLoader with multi-format support. Primary wizard entry for local data.
#   4. dlt_native (1): Passthrough escape hatch. User provides custom dlt pipeline
#      code. No wizard UI — advanced users only.
#   5. dlt core built-ins (2): filesystem + rest_api. Built into dlt, no vendoring.
#      filesystem: cloud storage (S3/GCS/Azure). Hidden from wizard — use local_files for local.
#      rest_api: connect any REST API via declarative config.
#   6. PostgreSQL (1): Dedicated wizard entry backed by dlt's built-in
#      sql_database source. Structured params for the most common DB use case.
#
# Excluded dlt verified sources:
#   - sql_database (generic): Too complex for wizard UI (arbitrary table selectors,
#     multiple DB dialects). Use dlt_native with dlt's sql_database for advanced
#     multi-database setups.
#   - Shopify: wizard_enabled=False pending P5-006 investigation — dlt's shopify_dlt
#     connector may be incompatible with Shopify's Jan 2026 API deprecation.

# ============================================================================
# SOURCE REGISTRY
# ============================================================================

SOURCE_REGISTRY: dict[str, dict[str, Any]] = {
    # ========================================
    # LOCAL / CUSTOM
    # ========================================
    "csv": {
        "display_name": "CSV Files",
        "category": "Local & Custom",
        "description": "Load data from local CSV files with incremental loading support",
        "auth_type": AuthType.NONE,
        "dlt_package": None,  # Custom implementation, not dlt
        "dlt_function": None,
        "wizard_enabled": False,  # Hidden — use "local_files" instead
        "required_params": [
            {
                "name": "directory",
                "type": "path",
                "prompt": "Directory containing CSV files",
                "default": "data/uploads",
                "help": "Default: data/uploads (already in .gitignore). Press Enter to use default.",
            },
            {
                "name": "file_pattern",
                "type": "string",
                "prompt": "File pattern",
                "default": "*.csv",
                "help": "Glob pattern for CSV files (e.g., '*.csv', 'data_*.csv')",
            },
        ],
        "optional_params": [
            {
                "name": "notes",
                "type": "text",
                "prompt": "Notes on how to refresh this data",
                "default": None,
                "help": "How do you regenerate/update this CSV? (e.g., 'Run python generate_orders.py' or 'Export from Salesforce > Reports')",
            },
        ],
        "setup_guide": [
            "1. Place your CSV files in a directory",
            "2. Ensure CSV files have headers (first row = column names)",
            "3. Files should have consistent schema across updates",
            "4. Dango will auto-detect column types and load incrementally",
        ],
        "cost_warning": None,
        "popularity": 10,  # 1-10, used for sorting
        "capabilities": {
            "performance_metrics": False,
            "date_range": False,
            "incremental": True,
            "custom_queries": False,
        },
    },
    "local_files": {
        "display_name": "File Import (CSV, JSON, Parquet)",
        "category": "Local & Custom",
        "description": "Load CSV, JSON, JSONL, or Parquet files from a directory. All matching files are combined into a single raw table. On re-sync, new/modified files are loaded, deleted files are removed.",
        "auth_type": AuthType.NONE,
        "dlt_package": None,  # Custom implementation (extends CSVLoader)
        "dlt_function": None,
        "wizard_enabled": True,
        "required_params": [
            {
                "name": "directory",
                "type": "path",
                "prompt": "Directory containing data files",
                "default": "data/uploads",
                "help": "Default: data/uploads (already in .gitignore). Press Enter to use default.",
            },
            {
                "name": "file_pattern",
                "type": "string",
                "prompt": "File pattern",
                "default": "*",
                "help": "Glob pattern for files (e.g., '*' for all supported formats, '*.json', 'data_*.csv')",
            },
        ],
        "optional_params": [
            {
                "name": "notes",
                "type": "text",
                "prompt": "Notes on how to refresh this data",
                "default": None,
                "help": "How do you regenerate/update this data? (e.g., 'Run python generate_orders.py' or 'Export from app')",
            },
        ],
        "setup_guide": [
            "1. Place your data files in a directory",
            "2. Supported formats: CSV, JSON, JSONL, Parquet",
            "3. Files should have consistent schema across updates",
            "4. Dango will auto-detect column types and load incrementally",
        ],
        "cost_warning": None,
        "popularity": 10,
        "capabilities": {
            "performance_metrics": False,
            "date_range": False,
            "incremental": True,
            "custom_queries": False,
        },
    },
    "dlt_native": {
        "display_name": "dlt Native Source (Advanced)",
        "category": "Local & Custom",
        "description": "Use any dlt verified source or custom source not in Dango's registry. Advanced users only.",
        "auth_type": AuthType.NONE,  # Auth handled by source itself
        "dlt_package": None,  # User specifies
        "dlt_function": None,  # User specifies
        "wizard_enabled": True,  # Registry bypass implementation complete
        "required_params": [
            {
                "name": "source_module",
                "type": "string",
                "prompt": "Source module name",
                "help": "Module name: from custom_sources/ or dlt package (e.g., 'my_source', 'google_ads')",
            },
            {
                "name": "source_function",
                "type": "string",
                "prompt": "Source function name",
                "help": "Function to call (e.g., 'my_source_func', 'google_ads')",
            },
        ],
        "optional_params": [],
        "setup_guide": [
            "ADVANCED FEATURE - For developers familiar with dlt",
            "",
            "1. File-based approach (recommended):",
            "   - Manually edit .dango/sources.yml",
            "   - Add source with type: dlt_native",
            "   - Configure source_module, source_function, function_kwargs",
            "   - See docs/ADVANCED_USAGE.md for examples",
            "",
            "2. Custom sources:",
            "   - Place Python files in custom_sources/ directory",
            "   - Define dlt source functions",
            "   - Configure in sources.yml",
            "",
            "3. Using dlt verified sources:",
            "   - Install dlt source package",
            "   - Configure credentials in .dlt/secrets.toml",
            "   - Add to sources.yml",
            "",
            "Documentation: docs/ADVANCED_USAGE.md, docs/REGISTRY_BYPASS.md",
        ],
        "docs_url": "https://dlthub.com/docs/build-a-pipeline-tutorial",
        "cost_warning": "⚠️  ADVANCED FEATURE - Manual configuration required",
        "popularity": 3,  # Low - for advanced users only
        "capabilities": {
            "performance_metrics": False,
            "date_range": False,
            "incremental": False,
            "custom_queries": True,
        },
    },
    "filesystem": {
        "display_name": "Files & Cloud Storage (Parquet, JSON, Excel)",
        "category": "Local & Custom",
        "description": "Load Parquet, JSON, JSONL, Excel, or CSV files from local disk or cloud storage (S3, GCS, Azure)",
        "auth_type": AuthType.NONE,
        "dlt_package": "filesystem",  # Built-in dlt core source
        "dlt_function": "filesystem",
        "wizard_enabled": False,  # Hidden — use "local_files" for local, filesystem still works for cloud
        "required_params": [
            {
                "name": "bucket_url",
                "type": "path",
                "prompt": "File path or cloud storage URL",
                "help": "Local path (e.g., 'data/exports/') or cloud URL (e.g., 's3://my-bucket/data/', 'gs://my-bucket/data/')",
            },
            {
                "name": "file_glob",
                "type": "string",
                "prompt": "File pattern (glob)",
                "default": "**/*.parquet",
                "help": "Glob pattern to match files (e.g., '*.parquet', '**/*.json', 'reports_*.xlsx')",
            },
        ],
        "optional_params": [],
        "setup_guide": [
            "LOCAL FILES:",
            "1. Place your files in a project directory (e.g., data/exports/)",
            "2. Set bucket_url to the directory path (e.g., 'data/exports/')",
            "3. Set file_glob to match your files (e.g., '*.parquet', '**/*.json')",
            "",
            "CLOUD STORAGE (S3):",
            "1. Set bucket_url to your S3 path (e.g., 's3://my-bucket/data/')",
            "2. Add credentials to .dlt/secrets.toml:",
            "   [sources.filesystem.credentials]",
            "   aws_access_key_id = 'your-key'",
            "   aws_secret_access_key = 'your-secret'",
            "   region_name = 'us-east-1'",
            "",
            "CLOUD STORAGE (GCS):",
            "1. Set bucket_url to your GCS path (e.g., 'gs://my-bucket/data/')",
            "2. Add credentials to .dlt/secrets.toml:",
            "   [sources.filesystem.credentials]",
            "   project_id = 'your-project'",
            "   private_key = '...'",
            "   client_email = '...'",
            "",
            "CLOUD STORAGE (Azure Blob):",
            "1. Set bucket_url to your Azure path (e.g., 'az://my-container/data/')",
            "2. Add credentials to .dlt/secrets.toml:",
            "   [sources.filesystem.credentials]",
            "   connection_string = 'DefaultEndpointsProtocol=https;AccountName=...'",
            "",
            "SUPPORTED FORMATS: Parquet, JSON (array or JSONL), Excel (.xlsx), CSV",
            "dlt handles schema inference automatically for all formats.",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/filesystem",
        "cost_warning": None,
        "popularity": 7,
        "capabilities": {
            "performance_metrics": False,
            "date_range": False,
            "incremental": False,
            "custom_queries": False,
        },
    },
    "rest_api": {
        "display_name": "REST API (Generic)",
        "category": "Local & Custom",
        "description": "Connect to any REST API with configurable authentication",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "rest_api",  # Built-in dlt source
        "dlt_function": "rest_api_source",
        "required_params": [
            {
                "name": "base_url",
                "type": "string",
                "prompt": "API base URL",
                "help": "Base URL for the API (e.g., 'https://api.example.com')",
            },
            {
                "name": "endpoints",
                "type": "json",
                "prompt": "Endpoint configurations",
                "help": "JSON array of endpoint configs (see docs for structure)",
            },
        ],
        "optional_params": [
            {
                "name": "auth_type",
                "type": "choice",
                "prompt": "Authentication type",
                "choices": ["bearer", "api_key", "basic", "oauth2_client_credentials", "none"],
                "default": "bearer",
            },
            {
                "name": "auth_token_env",
                "type": "string",
                "prompt": "Auth token environment variable",
                "default": "API_TOKEN",
                "help": "Name of .env variable containing auth token",
            },
        ],
        "setup_guide": [
            "1. Identify the API endpoints you want to sync",
            "2. Get API documentation for authentication method",
            "3. Obtain API keys/tokens from provider",
            "4. Configure endpoint paths, params, and pagination",
            "5. See REST API guide for detailed config examples",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api",
        "cost_warning": "Check API provider's rate limits and pricing",
        "wizard_enabled": True,
        "popularity": 8,
        "capabilities": {
            "performance_metrics": False,
            "date_range": False,
            # rest_api is a generic source — whether it's incremental depends
            # entirely on per-endpoint user configuration. Not incremental by
            # default.
            "incremental": False,
            "custom_queries": True,
        },
    },
    # ========================================
    # MARKETING & ANALYTICS
    # ========================================
    "google_sheets": {
        "display_name": "Google Sheets",
        "category": "Marketing & Analytics",
        "description": "Load data from Google Sheets (one or more tabs)",
        "auth_type": AuthType.OAUTH,
        "dlt_package": "google_sheets",
        "dlt_function": "google_spreadsheet",
        "required_params": [
            {
                "name": "spreadsheet_url_or_id",
                "type": "string",
                "prompt": "Spreadsheet ID or URL",
                "help": "Found in URL: docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit",
            },
            {
                "name": "range_names",
                "type": "sheet_selector",  # Special type: wizard fetches sheets and shows multi-select
                "prompt": "Select sheets/tabs to load",
                "help": "Each selected sheet becomes a table in the database",
            },
        ],
        # Transform string to list for backward compatibility with old configs
        "param_transforms": {
            "range_names": "list",  # Convert single string "Sheet1" to ["Sheet1"]
        },
        "setup_guide": [
            "1. OAuth setup runs automatically during 'dango source add'",
            "2. OR manually run: dango oauth google_sheets",
            "3. Follow the browser OAuth flow to authenticate",
            "4. Get spreadsheet ID from URL: docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit",
            "5. Credentials are permanent (refresh token stored in .dlt/secrets.toml)",
            "6. To add/remove sheets later: edit .dango/sources.yml and run 'dango sync'",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/google_sheets",
        "cost_warning": "Subject to Google API quota limits",
        "wizard_enabled": True,  # OAuth implementation complete
        "popularity": 10,
        "capabilities": {
            "performance_metrics": False,
            "date_range": False,
            "incremental": False,
            "custom_queries": False,
        },
    },
    "facebook_ads": {
        "display_name": "Facebook Ads",
        "category": "Marketing & Analytics",
        "description": "Load ad campaigns, ads, creatives, leads, and daily performance metrics from Facebook Ads",
        "auth_type": AuthType.OAUTH,
        "dlt_package": "facebook_ads",
        "dlt_function": "facebook_ads_combined",
        "pip_dependencies": [{"pip": "facebook-business", "import": "facebook_business"}],
        "required_params": [
            {
                "name": "account_id",
                "type": "string",
                "prompt": "Facebook Ads Account ID (numeric, e.g., 123456789)",
                "help": "Find in Facebook Ads Manager URL",
            },
            {
                "name": "access_token_env",
                "type": "secret",
                "env_var": "FB_ACCESS_TOKEN",
                "prompt": "Access Token (use 'dango oauth facebook_ads' to generate)",
                "help": "Long-lived User Access Token (60 days). Generate via 'dango oauth facebook_ads' or manually at https://developers.facebook.com/tools/accesstoken. Requires 'ads_read' permission.",
            },
        ],
        "optional_params": [
            {
                "name": "initial_load_past_days",
                "type": "integer",
                "default": 30,
                "help": "Number of past days of performance metrics to load on first sync",
            },
        ],
        "setup_guide": [
            "1. OAuth setup runs automatically during 'dango source add'",
            "2. OR manually run: dango oauth facebook_ads",
            "3. Follow the prompts to exchange short-lived token for long-lived token",
            "4. IMPORTANT: Access token expires in 60 days — set reminder to re-authenticate",
            "5. Loads entity data (campaigns, ads, etc.) AND daily performance metrics",
            "6. Performance metrics: reach, impressions, clicks, spend, CTR, CPC, CPM",
            "7. First sync loads 30 days of insights; subsequent syncs are incremental",
            "8. Facebook retains insights data for 37 months",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads",
        "available_resources": [
            "campaigns",
            "ads",
            "ad_sets",
            "ad_creatives",
            "leads",
            "facebook_insights",
        ],
        "default_resources": [
            "campaigns",
            "ads",
            "ad_sets",
            "ad_creatives",
            "leads",
            "facebook_insights",
        ],
        "default_config": {
            "attribution_window_days_lag": 28,  # Facebook attribution window; source reads this directly
        },
        "cost_warning": "Rate limited: 200 calls/hour per user, 4800/day per app",
        "wizard_enabled": True,  # OAuth implementation complete
        "popularity": 9,
        "capabilities": {
            "performance_metrics": True,
            "date_range": False,
            # Mixed: 5/6 resources (campaigns, ads, ad_sets, ad_creatives,
            # leads) use replace. Only facebook_insights uses
            # merge+incremental(date_start). Marked True because insights —
            # the primary analytics payload — are incremental.
            "incremental": True,
            "custom_queries": False,
        },
    },
    "google_analytics": {
        "display_name": "Google Analytics (GA4)",
        "category": "Marketing & Analytics",
        "description": "Load website analytics data from Google Analytics 4",
        "auth_type": AuthType.OAUTH,
        "dlt_package": "google_analytics",
        "dlt_function": "google_analytics",
        "pip_dependencies": [
            {"pip": "google-analytics-data", "import": "google.analytics.data_v1beta"}
        ],
        "required_params": [
            {
                "name": "property_id",
                "type": "string",
                "prompt": "GA4 Property ID",
                "help": "Find in GA4 Admin > Property Settings",
            },
        ],
        "optional_params": [
            {
                "name": "start_date",
                "type": "string",
                "prompt": "Start date (YYYY-MM-DD or relative like '90daysAgo')",
                "default": "90daysAgo",
                "help": "GA4 accepts relative dates (e.g., '90daysAgo', '30daysAgo') or absolute dates (YYYY-MM-DD). Defaults to 90daysAgo for first sync.",
            },
        ],
        # 6 themed queries covering 90-95% of analytics use cases.
        # GA4 Data API limits: max 9 dimensions per query.
        # WARNING: Changing dimensions after first sync requires full refresh
        # (dlt incremental state tracks dimension combinations).
        "default_config": {
            "lookback_days": 7,  # GA4 has 24-72h processing delay; 7 days catches corrections
            "queries": [
                {
                    # Traffic acquisition — how users arrive
                    "resource_name": "traffic",
                    "dimensions": [
                        "date",
                        "sessionSource",
                        "sessionMedium",
                        "sessionCampaignName",
                        "sessionDefaultChannelGroup",
                        "deviceCategory",
                        "operatingSystem",
                        "browser",
                    ],
                    "metrics": [
                        "sessions",
                        "engagedSessions",
                        "totalUsers",
                        "newUsers",
                        "averageSessionDuration",
                        "bounceRate",
                    ],
                },
                {
                    # Page performance — what users view
                    "resource_name": "pages",
                    "dimensions": [
                        "date",
                        "pagePath",
                        "pageTitle",
                        "sessionSource",
                        "sessionMedium",
                        "deviceCategory",
                    ],
                    "metrics": [
                        "screenPageViews",
                        "totalUsers",
                        "userEngagementDuration",
                        "sessions",
                        "bounceRate",
                    ],
                },
                {
                    # Landing pages — first touchpoints
                    "resource_name": "landing_pages",
                    "dimensions": [
                        "date",
                        "landingPage",
                        "sessionSource",
                        "sessionMedium",
                        "sessionCampaignName",
                        "sessionDefaultChannelGroup",
                        "deviceCategory",
                    ],
                    "metrics": [
                        "sessions",
                        "totalUsers",
                        "engagedSessions",
                        "bounceRate",
                        "averageSessionDuration",
                    ],
                },
                {
                    # Geographic — where users are
                    "resource_name": "geo",
                    "dimensions": [
                        "date",
                        "country",
                        "city",
                        "language",
                        "deviceCategory",
                    ],
                    "metrics": [
                        "sessions",
                        "totalUsers",
                        "engagedSessions",
                        "bounceRate",
                    ],
                },
                {
                    # Events — what users do (custom events + standard events)
                    "resource_name": "events",
                    "dimensions": [
                        "date",
                        "eventName",
                        "sessionSource",
                        "sessionMedium",
                        "deviceCategory",
                    ],
                    "metrics": [
                        "eventCount",
                        "totalUsers",
                        "eventCountPerUser",
                        "sessions",
                    ],
                },
                {
                    # Conversions — business outcomes
                    "resource_name": "conversions",
                    "dimensions": [
                        "date",
                        "sessionSource",
                        "sessionMedium",
                        "sessionCampaignName",
                        "sessionDefaultChannelGroup",
                        "deviceCategory",
                    ],
                    "metrics": [
                        "conversions",
                        "totalRevenue",
                        "totalUsers",
                        "sessions",
                        "engagedSessions",
                    ],
                },
            ],
        },
        "setup_guide": [
            "1. OAuth setup runs automatically during 'dango source add'",
            "2. OR manually run: dango oauth google_analytics",
            "3. Follow the browser OAuth flow to authenticate",
            "4. Get GA4 Property ID from Admin > Property Settings",
            "5. Default queries load 6 tables: traffic, pages, landing_pages, geo, events, conversions",
            "6. Edit .dlt/config.toml to customize dimensions/metrics",
            "7. WARNING: Changing dimensions after first sync requires 'dango sync --full-refresh'",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/google_analytics",
        "cost_warning": "Subject to Google API quota limits. Data is aggregated (not event-level).",
        "wizard_enabled": True,  # OAuth implementation complete
        "popularity": 9,
        "capabilities": {
            "performance_metrics": True,
            "date_range": True,
            "incremental": True,
            "custom_queries": True,
        },
    },
    # ========================================
    # BUSINESS & CRM
    # ========================================
    "hubspot": {
        "display_name": "HubSpot",
        "category": "Business & CRM",
        "description": "Load contacts, companies, deals, and tickets from HubSpot CRM",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "hubspot",
        "dlt_function": "hubspot",
        "wizard_enabled": True,
        "required_params": [
            {
                "name": "api_key_env",
                "type": "secret",
                "env_var": "HUBSPOT_API_KEY",
                "prompt": "HubSpot API Key",
                "help": "Generate in HubSpot Settings > Integrations > Private Apps",
            },
        ],
        "optional_params": [
            {
                "name": "resources",
                "type": "multiselect",
                "prompt": "Resources to sync",
                "choices": [
                    "contacts",
                    "companies",
                    "deals",
                    "tickets",
                    "products",
                    "quotes",
                    "owners",
                    "properties",
                    "pipelines_deals",
                    "pipelines_tickets",
                ],
                "default": [
                    "contacts",
                    "companies",
                    "deals",
                    "tickets",
                    "products",
                    "quotes",
                    "owners",
                    "properties",
                    "pipelines_deals",
                    "pipelines_tickets",
                ],
            },
        ],
        "setup_guide": [
            "1. Log in to HubSpot",
            "2. Go to Settings > Integrations > Private Apps",
            "   Direct URL: https://app.hubspot.com/private-apps/<hub_id>",
            "3. Create new private app — add scopes: crm.objects.contacts.read, crm.objects.companies.read, crm.objects.deals.read",
            "4. Copy the access token to .env as HUBSPOT_API_KEY",
        ],
        "available_resources": [
            "contacts",
            "companies",
            "deals",
            "tickets",
            "products",
            "quotes",
            "owners",
            "properties",
            "pipelines_deals",
            "pipelines_tickets",
        ],
        "default_resources": [
            "contacts",
            "companies",
            "deals",
            "tickets",
            "products",
            "quotes",
            "owners",
            "properties",
            "pipelines_deals",
            "pipelines_tickets",
        ],
        "first_sync_note": "First sync loads all historical data. Large accounts (>100k contacts) may take 15-30 minutes.",
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/hubspot",
        "cost_warning": "Subject to HubSpot API limits (varies by plan)",
        "popularity": 9,
        "capabilities": {
            "performance_metrics": False,
            "date_range": False,
            "incremental": True,
            "custom_queries": False,
        },
    },
    "salesforce": {
        "display_name": "Salesforce",
        "category": "Business & CRM",
        "description": "Load data from Salesforce CRM (accounts, contacts, opportunities, etc.)",
        "auth_type": AuthType.SERVICE_ACCOUNT,
        "dlt_package": "salesforce",
        "dlt_function": "salesforce_source",
        "pip_dependencies": [{"pip": "simple-salesforce", "import": "simple_salesforce"}],
        "wizard_enabled": True,
        "required_params": [],
        "optional_params": [],
        "available_resources": [
            "account",
            "contact",
            "lead",
            "opportunity",
            "campaign",
            "task",
            "event",
            "sf_user",
            "user_role",
            "product_2",
            "opportunity_line_item",
            "opportunity_contact_role",
            "campaign_member",
            "pricebook_2",
            "pricebook_entry",
        ],
        "default_resources": [
            "account",
            "contact",
            "lead",
            "opportunity",
            "campaign",
            "task",
            "event",
            "sf_user",
            "user_role",
            "product_2",
            "opportunity_line_item",
            "opportunity_contact_role",
            "campaign_member",
            "pricebook_2",
            "pricebook_entry",
        ],
        "secrets_toml_template": '[sources.{source_name}.credentials]\nuser_name = ""\npassword = ""\nsecurity_token = ""\n',
        "setup_guide": [
            "1. Log in to Salesforce → Setup → search 'Reset My Security Token'",
            "2. Click 'Reset Security Token' — it will be emailed to you",
            "3. Fill in credentials in .dlt/secrets.toml (template added automatically)",
            "4. Use your Salesforce login email, password, and the security token",
        ],
        "first_sync_note": "First sync loads all Salesforce objects. Large orgs may take 30+ minutes.",
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/salesforce",
        "cost_warning": "Salesforce API limits depend on edition (check your limits)",
        "popularity": 8,
        "capabilities": {
            "performance_metrics": False,
            "date_range": False,
            # Mixed: 7/15 resources use merge+incremental (account, opportunity,
            # opportunity_line_item, opportunity_contact_role, campaign_member,
            # task, event); 8/15 use replace. Marked True because the
            # highest-volume CRM objects (accounts, opportunities) are
            # incremental.
            "incremental": True,
            "custom_queries": False,
        },
    },
    # ========================================
    # E-COMMERCE & PAYMENT
    # ========================================
    "stripe": {
        "display_name": "Stripe",
        "category": "E-commerce & Payment",
        "description": "Load payment data from Stripe (charges, customers, subscriptions, etc.)",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "stripe_analytics",
        "dlt_function": "stripe_source",
        "pip_dependencies": [{"pip": "stripe", "import": "stripe"}],
        "wizard_enabled": True,  # Fully tested for v0.0.1
        "required_params": [
            {
                "name": "stripe_secret_key_env",
                "type": "secret",
                "env_var": "STRIPE_API_KEY",
                "prompt": "Stripe API Key (starts with sk_)",
                "help": "Find in Stripe Dashboard > Developers > API Keys",
            },
        ],
        "optional_params": [
            {
                "name": "endpoints",
                "type": "multiselect",
                "prompt": "Endpoints to sync (Space to select/deselect, Enter to continue)",
                "choices": [
                    "Charge",  # API uses capitalized, dlt normalizes to lowercase table names
                    "Customer",
                    "Subscription",
                    "Invoice",
                    "Product",
                    "Price",
                    "PaymentIntent",
                ],
                "default": [
                    "Charge",
                    "Customer",
                    "Subscription",
                    "Invoice",
                    "Product",
                    "Price",
                ],
            },
            {
                "name": "start_date",
                "type": "date",
                "prompt": "Start date (YYYY-MM-DD)",
                "default": "90daysAgo",
                "help": "How far back to load on first sync. Default: 90 days.",
            },
        ],
        "setup_guide": [
            "1. Go to https://dashboard.stripe.com/apikeys",
            "   (Use /test/apikeys for test mode during development)",
            "2. Click 'Reveal test key' to see the Secret key",
            "3. Copy the key (starts with sk_test_ or sk_live_)",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/stripe_analytics",
        "cost_warning": "No additional cost (included with Stripe account)",
        "popularity": 10,
        "capabilities": {
            "performance_metrics": False,
            "date_range": True,
            # stripe_source uses write_disposition="replace" for all resources
            # (full refresh every sync). Correct for mutable objects (charges
            # can be refunded, subscriptions updated).
            "incremental": False,
            "custom_queries": False,
        },
    },
    "shopify": {
        "display_name": "Shopify",
        "category": "E-commerce & Payment",
        "description": "Load e-commerce data from Shopify (orders, customers, products, etc.)",
        "auth_type": AuthType.OAUTH,
        "dlt_package": "shopify_dlt",  # Note: source name is shopify_dlt
        "dlt_function": "shopify_source",
        "required_params": [],
        "optional_params": [
            {
                "name": "resources",
                "type": "multiselect",
                "prompt": "Resources to sync",
                "choices": ["orders", "customers", "products"],
                "default": ["orders", "customers", "products"],
            },
            {
                "name": "start_date",
                "type": "date",
                "prompt": "Start date (YYYY-MM-DD)",
                "default": None,
            },
        ],
        "setup_guide": [
            "1. OAuth setup runs automatically during 'dango source add' — follow the prompts",
            "2. OR manually run: dango oauth shopify",
            "3. Either way: create a custom app in Shopify Admin > Apps > Develop apps",
            "4. Configure Admin API scopes (read permissions needed)",
            "5. Install app and reveal Admin API access token",
            "6. Enter shop URL (e.g., mystore.myshopify.com) and access token when prompted",
            "7. Credentials are permanent (stored in .dlt/secrets.toml)",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/shopify",
        "cost_warning": "Included with Shopify plan",
        "wizard_enabled": False,  # Disabled: OAuth 2.0 rewrite needed
        "popularity": 9,
        "capabilities": {
            "performance_metrics": False,
            "date_range": True,
            "incremental": True,
            "custom_queries": False,
        },
    },
    # ========================================
    # DEVELOPMENT
    # ========================================
    "github": {
        "display_name": "GitHub",
        "category": "Development",
        "description": "Load issues and pull requests with reactions and comments from GitHub",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "github",
        "dlt_function": "github_reactions",
        "wizard_enabled": True,
        "required_params": [
            {
                "name": "access_token_env",
                "type": "secret",
                "env_var": "GITHUB_ACCESS_TOKEN",
                "prompt": "GitHub Personal Access Token",
                "help": "Generate at https://github.com/settings/tokens. Required scopes: repo, read:org, read:user",
            },
            {
                "name": "owner",
                "type": "string",
                "prompt": "Repository owner (e.g., getdango)",
                "help": "GitHub username or organization that owns the repository",
            },
            {
                "name": "name",
                "type": "string",
                "prompt": "Repository name (e.g., dango)",
                "help": "Name of the repository to load data from",
            },
        ],
        "optional_params": [],
        "setup_guide": [
            "1. Go to https://github.com/settings/tokens",
            "2. Click 'Generate new token (classic)' (NOT fine-grained)",
            "3. Select scopes: repo, read:org, read:user",
            "4. Generate token (will start with ghp_) and copy",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/github",
        "cost_warning": "Rate limited: 5000 requests/hour (authenticated)",
        "popularity": 8,
        "capabilities": {
            "performance_metrics": False,
            "date_range": False,
            "incremental": False,
            "custom_queries": False,
        },
    },
    # ========================================
    # OTHER
    # ========================================
    "slack": {
        "display_name": "Slack",
        "category": "Communication",
        "description": "Load messages, channels, and user data from Slack",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "slack",
        "dlt_function": "slack_source",
        "wizard_enabled": True,
        "required_params": [
            {
                "name": "access_token_env",
                "type": "secret",
                "env_var": "SLACK_ACCESS_TOKEN",
                "prompt": "Slack Bot User OAuth Token (starts with xoxb-)",
                "help": "Bot User OAuth Token (starts with 'xoxb-'). Create at https://api.slack.com/apps > Your App > OAuth & Permissions. Required scopes: channels:history, channels:read, users:read. Must invite bot to channels you want to sync.",
            },
        ],
        "optional_params": [
            {
                "name": "selected_channels",
                "type": "list",
                "prompt": "Channel IDs to sync (empty = all channels)",
                "default": None,
                "help": "Find channel ID by right-clicking channel > View channel details",
            },
            {
                "name": "start_date",
                "type": "date",
                "prompt": "Start date for messages (YYYY-MM-DD)",
                "default": "90daysAgo",
                "help": "How far back to load messages on first sync. Default: 90 days.",
            },
        ],
        "setup_guide": [
            "1. Go to https://api.slack.com/apps → Create New App → From scratch",
            "2. Under OAuth & Permissions, add Bot Token Scopes:",
            "   channels:history, channels:read, users:read",
            "3. Click 'Install to Workspace' and authorize",
            "4. Copy 'Bot User OAuth Token' (starts with xoxb-)",
            "5. Invite the bot to channels: /invite @YourBotName",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/slack",
        "cost_warning": "Subject to Slack API rate limits",
        "popularity": 7,
        "capabilities": {
            "performance_metrics": False,
            "date_range": True,
            "incremental": True,
            "custom_queries": False,
        },
    },
    "zendesk": {
        "display_name": "Zendesk",
        "category": "Business & CRM",
        "description": "Load support tickets, users, and chat data from Zendesk Support, Talk, and Chat",
        "auth_type": AuthType.BASIC,
        "dlt_package": "zendesk",
        "dlt_function": "zendesk_support",
        "wizard_enabled": True,
        "required_params": [
            {
                "name": "subdomain",
                "type": "string",
                "prompt": "Zendesk subdomain (e.g., 'mycompany' from mycompany.zendesk.com)",
                "help": "Find this in your Zendesk URL: https://<subdomain>.zendesk.com",
            },
        ],
        "optional_params": [
            {
                "name": "start_date",
                "type": "date",
                "prompt": "Start date (YYYY-MM-DD)",
                "default": "90daysAgo",
                "help": "How far back to load tickets on first sync. Default: 90 days.",
            },
        ],
        "available_resources": [
            "tickets",
            "ticket_fields",
            "ticket_events",
            "ticket_metric_events",
        ],
        "default_resources": ["tickets", "ticket_fields", "ticket_events", "ticket_metric_events"],
        "secrets_toml_template": '[sources.{source_name}.credentials]\nsubdomain = "{subdomain}"\nemail = ""\ntoken = ""\n',
        "setup_guide": [
            "1. Log in to Zendesk as admin",
            "2. Go to Admin Center > Apps and Integrations > APIs > Zendesk API",
            "   Direct URL: https://<subdomain>.zendesk.com/admin/apps-integrations/apis/zendesk-api/settings",
            "3. Enable Token Access and click 'Add API token'",
            "4. Fill in credentials in .dlt/secrets.toml (template added automatically)",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/zendesk",
        "cost_warning": "Subject to Zendesk API rate limits",
        "popularity": 7,
        "capabilities": {
            "performance_metrics": False,
            "date_range": True,
            "incremental": True,
            "custom_queries": False,
        },
    },
    # Additional verified sources (skeleton metadata - to be expanded)
    "google_ads": {
        "display_name": "Google Ads",
        "category": "Marketing & Analytics",
        "description": "Load daily performance metrics from Google Ads via GAQL queries",
        "auth_type": AuthType.OAUTH,
        "dlt_package": "google_ads",
        "dlt_function": "google_ads",
        "pip_dependencies": [{"pip": "google-ads", "import": "google.ads"}],
        "required_params": [],  # OAuth handles credentials; developer_token and customer_id are collected during auth
        "optional_params": [
            {
                "name": "start_date",
                "type": "date",
                "prompt": "Start date (YYYY-MM-DD)",
                "default": "90daysAgo",
                "help": "How far back to load on first sync. Default: 90 days.",
            },
        ],
        # Default GAQL queries — each becomes a table. Comprehensive coverage
        # with all compatible fields per resource. Note: some metrics/segments
        # can't be combined in GAQL; these queries have been validated.
        # See https://developers.google.com/google-ads/api/fields/v17/overview
        "default_config": {
            "lookback_days": 90,  # Google Ads conversion attribution window
            "queries": [
                {
                    "resource_name": "campaign_stats",
                    "query": (
                        "SELECT "
                        "segments.date, "
                        "campaign.id, "
                        "campaign.name, "
                        "campaign.status, "
                        "campaign.advertising_channel_type, "
                        "campaign.bidding_strategy_type, "
                        "metrics.impressions, "
                        "metrics.clicks, "
                        "metrics.cost_micros, "
                        "metrics.conversions, "
                        "metrics.conversions_value, "
                        "metrics.ctr, "
                        "metrics.average_cpc, "
                        "metrics.average_cpm, "
                        "metrics.search_impression_share, "
                        "metrics.search_rank_lost_impression_share "
                        "FROM campaign "
                        "WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'"
                    ),
                },
                {
                    "resource_name": "ad_group_stats",
                    "query": (
                        "SELECT "
                        "segments.date, "
                        "campaign.id, "
                        "campaign.name, "
                        "ad_group.id, "
                        "ad_group.name, "
                        "ad_group.status, "
                        "ad_group.type, "
                        "metrics.impressions, "
                        "metrics.clicks, "
                        "metrics.cost_micros, "
                        "metrics.conversions, "
                        "metrics.conversions_value, "
                        "metrics.ctr, "
                        "metrics.average_cpc "
                        "FROM ad_group "
                        "WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'"
                    ),
                },
                {
                    "resource_name": "keyword_stats",
                    "query": (
                        "SELECT "
                        "segments.date, "
                        "campaign.id, "
                        "campaign.name, "
                        "ad_group.id, "
                        "ad_group.name, "
                        "ad_group_criterion.keyword.text, "
                        "ad_group_criterion.keyword.match_type, "
                        "ad_group_criterion.quality_info.quality_score, "
                        "metrics.impressions, "
                        "metrics.clicks, "
                        "metrics.cost_micros, "
                        "metrics.conversions, "
                        "metrics.ctr, "
                        "metrics.average_cpc, "
                        "metrics.search_impression_share "
                        "FROM keyword_view "
                        "WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'"
                    ),
                },
                {
                    "resource_name": "ad_stats",
                    "query": (
                        "SELECT "
                        "segments.date, "
                        "campaign.id, "
                        "campaign.name, "
                        "ad_group.id, "
                        "ad_group.name, "
                        "ad_group_ad.ad.id, "
                        "ad_group_ad.ad.name, "
                        "ad_group_ad.ad.type, "
                        "ad_group_ad.status, "
                        "metrics.impressions, "
                        "metrics.clicks, "
                        "metrics.cost_micros, "
                        "metrics.conversions, "
                        "metrics.conversions_value, "
                        "metrics.ctr "
                        "FROM ad_group_ad "
                        "WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'"
                    ),
                },
                {
                    "resource_name": "search_term_stats",
                    "query": (
                        "SELECT "
                        "segments.date, "
                        "campaign.id, "
                        "campaign.name, "
                        "ad_group.id, "
                        "ad_group.name, "
                        "search_term_view.search_term, "
                        "search_term_view.status, "
                        "metrics.impressions, "
                        "metrics.clicks, "
                        "metrics.cost_micros, "
                        "metrics.conversions, "
                        "metrics.ctr, "
                        "metrics.average_cpc "
                        "FROM search_term_view "
                        "WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'"
                    ),
                },
                {
                    "resource_name": "geographic_stats",
                    "query": (
                        "SELECT "
                        "segments.date, "
                        "campaign.id, "
                        "campaign.name, "
                        "geographic_view.country_criterion_id, "
                        "geographic_view.location_type, "
                        "metrics.impressions, "
                        "metrics.clicks, "
                        "metrics.cost_micros, "
                        "metrics.conversions, "
                        "metrics.conversions_value "
                        "FROM geographic_view "
                        "WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'"
                    ),
                },
            ],
        },
        "setup_guide": [
            "1. OAuth setup runs automatically during 'dango source add'",
            "2. OR manually run: dango oauth google_ads",
            "3. Follow the browser OAuth flow to authenticate",
            "4. Enter Developer Token from Google Ads API Center",
            "5. Enter Customer ID (find in Google Ads account URL, no hyphens)",
            "6. Default queries load 6 tables: campaign, ad_group, keyword, ad,"
            " search_term, geographic stats",
            "7. Edit .dlt/config.toml to customize GAQL queries",
            "8. Use Google Ads Query Builder to validate field compatibility",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/google_ads",
        "cost_warning": "Subject to Google Ads API rate limits",
        "wizard_enabled": True,  # OAuth implementation complete
        "popularity": 7,
        "capabilities": {
            "performance_metrics": True,
            "date_range": True,
            # Uses append + delete-before-insert lookback (90-day window).
            # First sync loads full history; subsequent syncs re-load the
            # lookback window only. Older data is preserved.
            "incremental": True,
            "custom_queries": True,
        },
    },
    "matomo": {
        "display_name": "Matomo Analytics",
        "category": "Marketing & Analytics",
        "description": "Load reports and raw visits data from Matomo",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "matomo",
        "dlt_function": "matomo_reports",
        "wizard_enabled": False,  # Disabled: token passed via GET param (security risk)
        "required_params": [
            {
                "name": "url",
                "type": "string",
                "prompt": "Matomo instance URL (e.g., https://analytics.example.com)",
                "help": "URL of your Matomo installation",
            },
            {
                "name": "api_token_env",
                "type": "secret",
                "env_var": "MATOMO_API_TOKEN",
                "prompt": "Matomo API Token",
                "help": "Generate at Settings > Platform > API",
            },
            {
                "name": "site_id",
                "type": "integer",
                "prompt": "Site ID to track",
                "help": "Found in Matomo dashboard (usually a number like '1')",
            },
        ],
        "optional_params": [],
        "default_config": {
            "queries": [
                {
                    "resource_name": "visits_summary",
                    "methods": ["VisitsSummary.get"],
                    "date": "today",
                    "period": "month",
                },
                {
                    "resource_name": "referrers",
                    "methods": ["Referrers.getAll"],
                    "date": "today",
                    "period": "month",
                },
                {
                    "resource_name": "pages",
                    "methods": ["Actions.getPageUrls"],
                    "date": "today",
                    "period": "month",
                },
            ],
        },
        "setup_guide": [
            "1. Log in to Matomo",
            "2. Go to Settings > Platform > API",
            "3. Create API token",
            "4. Add to .env as MATOMO_API_TOKEN",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/matomo",
        "popularity": 5,
        "capabilities": {
            "performance_metrics": True,
            "date_range": False,
            "incremental": True,
            "custom_queries": True,
        },
    },
    "mux": {
        "display_name": "Mux",
        "category": "Marketing & Analytics",
        "description": "Load video analytics data from Mux",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "mux",
        "dlt_function": "mux_source",
        "wizard_enabled": True,
        "required_params": [],
        "optional_params": [
            {
                "name": "start_date",
                "type": "string",
                "prompt": "Start date for video views (YYYY-MM-DD)",
                "help": "How far back to load views. Defaults to 30 days ago.",
            },
        ],
        "setup_guide": [
            "1. Log in to Mux Dashboard",
            "2. Go to Settings > Access Tokens",
            "3. Create new token with read permissions",
            "4. Add to .dlt/secrets.toml:",
            "   [sources.mux]",
            "   mux_api_access_token = 'your_token_id'",
            "   mux_api_secret_key = 'your_secret_key'",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/mux",
        "popularity": 4,
        "capabilities": {
            "performance_metrics": True,
            "date_range": True,
            # assets use merge (idempotent upsert by PK) but no
            # dlt.sources.incremental() cursor — effectively full refresh
            # because all assets are re-fetched each run.
            "incremental": False,
            "custom_queries": False,
        },
    },
    "airtable": {
        "display_name": "Airtable",
        "category": "Marketing & Analytics",
        "description": "Load tables from Airtable bases",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "airtable",
        "dlt_function": "airtable_source",
        "pip_dependencies": [{"pip": "pyairtable", "import": "pyairtable"}],
        "wizard_enabled": True,
        "required_params": [
            {
                "name": "base_id",
                "type": "string",
                "prompt": "Airtable Base ID (starts with 'app')",
                "help": "Find in URL: airtable.com/BASE_ID/... - See https://support.airtable.com/docs/finding-airtable-ids",
            },
            {
                "name": "access_token_env",
                "type": "secret",
                "env_var": "AIRTABLE_ACCESS_TOKEN",
                "prompt": "Airtable Personal Access Token",
                "help": "Personal Access Token (starts with 'pat'). Create at https://airtable.com/create/tokens. Required scopes: data.records:read, schema.bases:read. Must add specific bases to token access.",
            },
        ],
        "optional_params": [
            {
                "name": "table_names",
                "type": "list",
                "prompt": "Table names or IDs to load (empty = all tables)",
                "default": None,
                "help": "Comma-separated list of table names or IDs (IDs start with 'tbl')",
            },
        ],
        "setup_guide": [
            "1. Go to https://airtable.com/create/tokens",
            "2. Create a new personal access token",
            "3. Grant scopes: data.records:read, schema.bases:read",
            "4. Add bases you want to access",
            "5. Copy token to .env as AIRTABLE_ACCESS_TOKEN",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/airtable",
        "popularity": 7,
        "capabilities": {
            "performance_metrics": False,
            "date_range": False,
            "incremental": False,
            "custom_queries": False,
        },
    },
    "pipedrive": {
        "display_name": "Pipedrive",
        "category": "Business & CRM",
        "description": "Load deals, contacts, and activities from Pipedrive CRM",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "pipedrive",
        "dlt_function": "pipedrive_source",
        "wizard_enabled": True,
        "required_params": [
            {
                "name": "pipedrive_api_key_env",
                "type": "secret",
                "env_var": "PIPEDRIVE_API_KEY",
                "prompt": "Pipedrive API Token",
                "help": "Find at Settings > Personal > API - See https://pipedrive.readme.io/docs/how-to-find-the-api-token",
            },
        ],
        "optional_params": [
            {
                "name": "since_timestamp",
                "type": "date",
                "prompt": "Start date for incremental loading (YYYY-MM-DD HH:MM:SS)",
                "default": "1970-01-01 00:00:00",
                "help": "Load data updated since this timestamp",
            },
            {
                "name": "resources",
                "type": "multiselect",
                "prompt": "Resources to sync",
                "choices": [
                    "activities",
                    "deals",
                    "deals_flow",
                    "deals_participants",
                    "files",
                    "filters",
                    "leads",
                    "notes",
                    "organizations",
                    "persons",
                    "pipelines",
                    "products",
                    "projects",
                    "stages",
                    "tasks",
                    "users",
                ],
                "default": [
                    "activities",
                    "deals",
                    "deals_flow",
                    "deals_participants",
                    "files",
                    "filters",
                    "leads",
                    "notes",
                    "organizations",
                    "persons",
                    "pipelines",
                    "products",
                    "projects",
                    "stages",
                    "tasks",
                    "users",
                ],
            },
        ],
        "setup_guide": [
            "1. Log in to Pipedrive",
            "2. Go to Settings > Personal preferences > API",
            "3. Copy your Personal API token",
            "4. Add to .env as PIPEDRIVE_API_KEY",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/pipedrive",
        "popularity": 7,
        "capabilities": {
            "performance_metrics": False,
            "date_range": False,
            "incremental": True,
            "custom_queries": False,
        },
    },
    "freshdesk": {
        "display_name": "Freshdesk",
        "category": "Business & CRM",
        "description": "Load support tickets, agents, and companies from Freshdesk",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "freshdesk",
        "dlt_function": "freshdesk_source",
        "wizard_enabled": True,
        "required_params": [
            {
                "name": "domain",
                "type": "string",
                "prompt": "Freshdesk domain (e.g., 'yourcompany' from yourcompany.freshdesk.com)",
                "help": "Your Freshdesk subdomain",
            },
            {
                "name": "api_secret_key_env",
                "type": "secret",
                "env_var": "FRESHDESK_API_KEY",
                "prompt": "Freshdesk API Key",
                "help": "Find at Profile Settings > View API key",
            },
        ],
        "optional_params": [
            {
                "name": "endpoints",
                "type": "multiselect",
                "prompt": "Resources to sync",
                "choices": [
                    "tickets",
                    "agents",
                    "companies",
                    "contacts",
                    "groups",
                    "roles",
                    "skills",
                ],
                "default": [
                    "tickets",
                    "agents",
                    "companies",
                    "contacts",
                    "groups",
                    "roles",
                ],
            },
            {
                "name": "per_page",
                "type": "number",
                "prompt": "Results per page (max 100)",
                "default": 100,
                "help": "Number of records to fetch per API call",
            },
        ],
        "setup_guide": [
            "1. Log in to Freshdesk",
            "2. Go to Profile Settings > View API key",
            "3. Copy your API key",
            "4. Add to .env as FRESHDESK_API_KEY",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/freshdesk",
        "popularity": 6,
        "capabilities": {
            "performance_metrics": False,
            "date_range": False,
            "incremental": True,
            "custom_queries": False,
        },
    },
    "jira": {
        "display_name": "Jira",
        "category": "Business & CRM",
        "description": "Load issues, users, workflows, and projects from Jira",
        "auth_type": AuthType.BASIC,
        "dlt_package": "jira",
        "dlt_function": "jira",
        "wizard_enabled": False,  # Disabled: wrong endpoint in dlt source
        "required_params": [
            {
                "name": "subdomain",
                "type": "string",
                "prompt": "Jira subdomain (e.g., 'mycompany' from mycompany.atlassian.net)",
                "help": "Your Jira Cloud subdomain",
            },
            {
                "name": "email_env",
                "type": "secret",
                "env_var": "JIRA_EMAIL",
                "prompt": "Jira account email",
                "help": "Email used to log into Jira",
            },
            {
                "name": "api_token_env",
                "type": "secret",
                "env_var": "JIRA_API_TOKEN",
                "prompt": "Jira API Token",
                "help": "Generate at https://id.atlassian.com/manage-profile/security/api-tokens",
            },
        ],
        "optional_params": [
            {
                "name": "page_size",
                "type": "number",
                "prompt": "Page size (results per request)",
                "default": 50,
                "help": "Number of results to fetch per API call",
            },
            {
                "name": "resources",
                "type": "multiselect",
                "prompt": "Resources to sync",
                "choices": ["issues", "projects", "users", "workflows"],
                "default": ["issues", "projects"],
            },
        ],
        "setup_guide": [
            "1. Go to https://id.atlassian.com/manage-profile/security/api-tokens",
            "2. Click 'Create API token'",
            "3. Give it a label and create",
            "4. Copy token to .env as JIRA_API_TOKEN",
            "5. Add your Jira email to .env as JIRA_EMAIL",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/jira",
        "popularity": 8,
        "capabilities": {
            "performance_metrics": False,
            "date_range": False,
            "incremental": False,
            "custom_queries": False,
        },
    },
    "workable": {
        "display_name": "Workable",
        "category": "Business & CRM",
        "description": "Load candidates, jobs, and events from Workable ATS",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "workable",
        "dlt_function": "workable_source",
        "wizard_enabled": True,
        "required_params": [
            {
                "name": "access_token_env",
                "type": "secret",
                "env_var": "WORKABLE_ACCESS_TOKEN",
                "prompt": "Workable API Access Token",
                "help": "Generate at Integrations > API",
            },
            {
                "name": "subdomain",
                "type": "string",
                "prompt": "Workable subdomain (e.g., 'yourcompany' from yourcompany.workable.com)",
                "help": "Your Workable subdomain",
            },
        ],
        "optional_params": [
            {
                "name": "start_date",
                "type": "date",
                "prompt": "Start date for data loading (YYYY-MM-DD)",
                "default": "2000-01-01",
                "help": "Load data created after this date",
            },
            {
                "name": "load_details",
                "type": "boolean",
                "prompt": "Load detailed data (activities, etc.)?",
                "default": False,
                "help": "Load additional details for jobs and candidates (slower)",
            },
        ],
        "setup_guide": [
            "1. Log in to Workable",
            "2. Go to Settings > Integrations > API",
            "3. Click 'Generate new token'",
            "4. Copy token to .env as WORKABLE_ACCESS_TOKEN",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/workable",
        "popularity": 5,
        "capabilities": {
            "performance_metrics": False,
            "date_range": True,
            # Mixed: 7/8 resources use replace, but candidates uses
            # merge+incremental(updated_at). Marked True because candidates
            # is the highest-volume resource and benefits from incremental.
            "incremental": True,
            "custom_queries": False,
        },
    },
    "asana": {
        "display_name": "Asana",
        "category": "Business & CRM",
        "description": "Load tasks, projects, and workspaces from Asana",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "asana_dlt",  # Note: source name is asana_dlt
        "dlt_function": "asana_source",
        "wizard_enabled": False,  # Disabled: Asana SDK removed from dlt source
        "required_params": [],
        "optional_params": [
            {
                "name": "resources",
                "type": "multiselect",
                "prompt": "Resources to sync",
                "choices": [
                    "workspaces",
                    "projects",
                    "sections",
                    "tags",
                    "tasks",
                    "stories",
                    "teams",
                    "users",
                ],
                "default": ["workspaces", "projects", "tasks"],
            },
        ],
        "setup_guide": [
            "1. Create a Personal Access Token at https://app.asana.com/0/developer-console",
            "2. Note: The Asana SDK has been removed from the dlt source.",
            "   Credentials must be added to .dlt/secrets.toml:",
            "   [sources.asana_dlt]",
            "   access_token = 'your_token_here'",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/asana",
        "popularity": 7,
        "capabilities": {
            "performance_metrics": False,
            "date_range": False,
            # asana_source uses write_disposition="replace" for 6/8 resources
            # (workspaces, projects, sections, tags, users, teams). Only tasks
            # uses merge+incremental(modified_at); stories uses append (no
            # cursor). Not truly incremental overall.
            "incremental": False,
            "custom_queries": False,
        },
    },
    "notion": {
        "display_name": "Notion",
        "category": "Files & Storage",
        "description": "Load pages and databases from Notion",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "notion",
        "dlt_function": "notion_databases",
        "wizard_enabled": True,
        "required_params": [
            {
                "name": "api_key_env",
                "type": "secret",
                "env_var": "NOTION_API_KEY",
                "prompt": "Notion Integration Token (starts with 'secret_')",
                "help": "Create integration at https://www.notion.so/my-integrations",
            },
        ],
        "optional_params": [
            {
                "name": "database_ids",
                "type": "json",
                "prompt": "Database IDs to sync (JSON array, empty = all)",
                "default": None,
                "help": 'Format: [{"id": "db_id", "use_name": "my_db"}] - Leave empty to sync all databases',
            },
        ],
        "setup_guide": [
            "1. Go to https://www.notion.so/my-integrations",
            "2. Create new integration and copy Internal Integration Token",
            "3. Share databases/pages with your integration",
            "4. Add token to .env as NOTION_API_KEY",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/notion",
        "popularity": 7,
        "capabilities": {
            "performance_metrics": False,
            "date_range": False,
            "incremental": False,
            "custom_queries": False,
        },
    },
    "inbox": {
        "display_name": "Email Inbox (IMAP)",
        "category": "Files & Storage",
        "description": "Read messages and attachments from email inbox via IMAP",
        "auth_type": AuthType.BASIC,
        "dlt_package": "inbox",
        "dlt_function": "inbox_source",
        "wizard_enabled": True,
        "required_params": [
            {
                "name": "host",
                "type": "string",
                "prompt": "IMAP server host (e.g., imap.gmail.com)",
                "help": "IMAP server address for your email provider",
            },
            {
                "name": "email_account_env",
                "type": "secret",
                "env_var": "EMAIL_ACCOUNT",
                "prompt": "Email address",
                "help": "Your email address",
            },
            {
                "name": "password_env",
                "type": "secret",
                "env_var": "EMAIL_PASSWORD",
                "prompt": "Email password or app password",
                "help": "For Gmail, use an app password (not your regular password)",
            },
        ],
        "optional_params": [
            {
                "name": "folder",
                "type": "string",
                "prompt": "Folder to read (default: INBOX)",
                "default": "INBOX",
                "help": "Email folder/label to sync",
            },
        ],
        "setup_guide": [
            "1. Enable IMAP in your email provider settings",
            "2. For Gmail: Create app password at myaccount.google.com/apppasswords",
            "3. Add credentials to .env (EMAIL_ACCOUNT, EMAIL_PASSWORD)",
            "4. Find IMAP server (Gmail: imap.gmail.com, Outlook: outlook.office365.com)",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/inbox",
        "popularity": 5,
        "capabilities": {
            "performance_metrics": False,
            "date_range": False,
            "incremental": True,
            "custom_queries": False,
        },
    },
    "mongodb": {
        "display_name": "MongoDB",
        "category": "Databases",
        "description": "Load collections from MongoDB databases with incremental support",
        "auth_type": AuthType.BASIC,
        "dlt_package": "mongodb",
        "dlt_function": "mongodb",
        "pip_dependencies": [{"pip": "pymongo", "import": "pymongo"}],
        "wizard_enabled": True,
        "required_params": [
            {
                "name": "connection_url_env",
                "type": "secret",
                "env_var": "MONGODB_CONNECTION_URL",
                "prompt": "MongoDB connection URL",
                "help": "Format: mongodb://username:password@host:port/database or mongodb+srv://...",
            },
        ],
        "optional_params": [
            {
                "name": "database",
                "type": "string",
                "prompt": "Database name (empty = default database from connection URL)",
                "default": None,
                "help": "Specific database to load from",
            },
            {
                "name": "collection_names",
                "type": "list",
                "prompt": "Collection names to sync (comma-separated, empty = all)",
                "default": None,
                "help": "Leave empty to sync all collections in database",
            },
            {
                "name": "parallel",
                "type": "boolean",
                "prompt": "Enable parallel loading?",
                "default": False,
                "help": "Load collections in parallel (faster but more resource-intensive)",
            },
        ],
        "setup_guide": [
            "1. Ensure MongoDB is accessible from your network",
            "2. Create read-only user (recommended): db.createUser({user: 'dango', pwd: '...', roles: ['read']})",
            "3. Get connection URL (check MongoDB Atlas or your hosting provider)",
            "4. Add to .env as MONGODB_CONNECTION_URL",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/mongodb",
        "popularity": 8,
        "capabilities": {
            "performance_metrics": False,
            "date_range": False,
            # mongodb() accepts an optional incremental parameter (defaults to
            # None). Without explicit configuration, it does a full load — not
            # incremental by default. Users can enable incremental via
            # dlt_native config with the incremental parameter.
            "incremental": False,
            "incremental_available": True,
            "custom_queries": False,
        },
    },
    "postgres": {
        "display_name": "PostgreSQL",
        "category": "Databases",
        "description": "Load tables from PostgreSQL databases with schema filtering",
        "auth_type": AuthType.BASIC,
        "dlt_package": "sql_database",
        "dlt_function": "sql_database",
        "pip_dependencies": [
            {"pip": "sqlalchemy", "import": "sqlalchemy"},
            {"pip": "psycopg2-binary", "import": "psycopg2"},
        ],
        "wizard_enabled": True,
        "required_params": [
            {
                "name": "credentials_env",
                "type": "secret",
                "env_var": "POSTGRES_CREDENTIALS",
                "prompt": "PostgreSQL connection URL",
                "help": "Format: postgresql://username:password@host:port/database",
            },
        ],
        "optional_params": [
            {
                "name": "schema",
                "type": "string",
                "prompt": "Schema name (empty = public)",
                "default": "public",
                "help": "PostgreSQL schema to load tables from",
            },
            {
                "name": "table_names",
                "type": "list",
                "prompt": "Tables to sync (comma-separated, empty = all)",
                "default": None,
                "help": "Leave empty to sync all tables in the schema",
            },
        ],
        "setup_guide": [
            "1. Ensure PostgreSQL is accessible from your network",
            "2. Create a read-only user (recommended):",
            "   CREATE USER dango WITH PASSWORD 'your_password';",
            "   GRANT CONNECT ON DATABASE mydb TO dango;",
            "   GRANT USAGE ON SCHEMA public TO dango;",
            "   GRANT SELECT ON ALL TABLES IN SCHEMA public TO dango;",
            "3. Build connection URL: postgresql://dango:your_password@host:5432/mydb",
            "4. Install driver: pip install sqlalchemy psycopg2-binary",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/sql_database",
        "popularity": 8,
        "capabilities": {
            "performance_metrics": False,
            "date_range": False,
            # sql_database() accepts an optional incremental parameter per
            # table (defaults to None). Without explicit configuration, it
            # loads all rows on every run — not incremental by default.
            # Users can enable per-table incremental via dlt_native config.
            "incremental": False,
            "incremental_available": True,
            "custom_queries": False,
        },
    },
    "kafka": {
        "display_name": "Apache Kafka",
        "category": "Streaming",
        "description": "Extract messages from Kafka topics",
        "auth_type": AuthType.NONE,
        "dlt_package": "kafka",
        "dlt_function": "kafka_consumer",
        "pip_dependencies": [{"pip": "confluent-kafka", "import": "confluent_kafka"}],
        "wizard_enabled": True,
        "required_params": [
            {
                "name": "topics",
                "type": "list",
                "prompt": "Kafka topics to consume (comma-separated)",
                "help": "List of topic names to extract messages from",
            },
            {
                "name": "credentials_env",
                "type": "secret",
                "env_var": "KAFKA_CREDENTIALS",
                "prompt": "Kafka connection credentials (JSON config)",
                "help": "JSON with bootstrap.servers, group.id, and optional security settings",
            },
        ],
        "optional_params": [
            {
                "name": "batch_size",
                "type": "number",
                "prompt": "Batch size (messages per request)",
                "default": 3000,
                "help": "Number of messages to read at once",
            },
            {
                "name": "batch_timeout",
                "type": "number",
                "prompt": "Batch timeout (seconds)",
                "default": 3,
                "help": "Maximum time to wait for a batch",
            },
            {
                "name": "start_from",
                "type": "date",
                "prompt": "Start timestamp (YYYY-MM-DD HH:MM:SS, empty = beginning)",
                "default": None,
                "help": "Read messages from this timestamp onwards",
            },
        ],
        "setup_guide": [
            "1. Get Kafka broker addresses (bootstrap.servers)",
            "2. Create consumer group ID",
            "3. If using auth: get SASL credentials or SSL certificates",
            "4. Create JSON config with connection details",
            "5. Add to .env as KAFKA_CREDENTIALS",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/kafka",
        "popularity": 7,
        "capabilities": {
            "performance_metrics": False,
            "date_range": False,
            "incremental": True,
            "custom_queries": False,
        },
    },
    "kinesis": {
        "display_name": "Amazon Kinesis",
        "category": "Streaming",
        "description": "Read messages from Kinesis streams",
        "auth_type": AuthType.SERVICE_ACCOUNT,
        "dlt_package": "kinesis",
        "dlt_function": "kinesis_stream",
        "wizard_enabled": True,
        "required_params": [
            {
                "name": "stream_name",
                "type": "string",
                "prompt": "Kinesis stream name",
                "help": "Name of the Kinesis stream to read from",
            },
            {
                "name": "credentials_env",
                "type": "secret",
                "env_var": "AWS_CREDENTIALS",
                "prompt": "AWS credentials (JSON with aws_access_key_id, aws_secret_access_key, region_name)",
                "help": "AWS credentials with Kinesis read permissions",
            },
        ],
        "optional_params": [
            {
                "name": "initial_at_timestamp",
                "type": "date",
                "prompt": "Start timestamp (YYYY-MM-DD HH:MM:SS, 0 = beginning)",
                "default": "0",
                "help": "Timestamp to start reading from (0 for earliest, empty for latest)",
            },
            {
                "name": "chunk_size",
                "type": "number",
                "prompt": "Chunk size (records per request)",
                "default": 1000,
                "help": "Number of records to fetch per API call",
            },
            {
                "name": "parse_json",
                "type": "boolean",
                "prompt": "Parse messages as JSON?",
                "default": True,
                "help": "If True, parses message data as JSON objects",
            },
        ],
        "setup_guide": [
            "1. Create IAM user with Kinesis read permissions",
            "2. Get AWS access key ID and secret access key",
            '3. Create JSON: {"aws_access_key_id": "...", "aws_secret_access_key": "...", "region_name": "us-east-1"}',
            "4. Add to .env as AWS_CREDENTIALS",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/kinesis",
        "popularity": 6,
        "capabilities": {
            "performance_metrics": False,
            "date_range": False,
            "incremental": True,
            "custom_queries": False,
        },
    },
    "chess": {
        "display_name": "Chess.com",
        "category": "Other",
        "description": "Load player profiles and games from Chess.com API",
        "auth_type": AuthType.NONE,
        "dlt_package": "chess",
        "dlt_function": "source",
        "wizard_enabled": True,
        "required_params": [
            {
                "name": "players",
                "type": "list",
                "prompt": "Player usernames to track (comma-separated)",
                "help": "Chess.com usernames to load profiles and games for",
            },
        ],
        "optional_params": [],
        "setup_guide": [
            "1. No authentication required",
            "2. Chess.com API is public and free",
            "3. Just provide player usernames to track",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/chess",
        "popularity": 3,
        "capabilities": {
            "performance_metrics": False,
            "date_range": False,
            "incremental": False,
            "custom_queries": False,
        },
    },
    "strapi": {
        "display_name": "Strapi",
        "category": "Other",
        "description": "Load content from Strapi headless CMS",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "strapi",
        "dlt_function": "strapi_source",
        "wizard_enabled": False,  # Disabled: untested, requires Docker Strapi instance
        "required_params": [
            {
                "name": "domain",
                "type": "string",
                "prompt": "Strapi instance URL (e.g., https://cms.example.com)",
                "help": "Base URL of your Strapi installation",
            },
            {
                "name": "api_secret_key_env",
                "type": "secret",
                "env_var": "STRAPI_API_SECRET_KEY",
                "prompt": "Strapi API Token",
                "help": "Create at Settings > API Tokens",
            },
            {
                "name": "endpoints",
                "type": "list",
                "prompt": "Content type endpoints (comma-separated, e.g., posts,articles)",
                "help": "Strapi collection names to load data from",
            },
        ],
        "optional_params": [],
        "setup_guide": [
            "1. Log in to Strapi admin",
            "2. Go to Settings > API Tokens",
            "3. Create new token with read permissions",
            "4. Copy token to .env as STRAPI_API_SECRET_KEY",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/strapi",
        "popularity": 5,
        "capabilities": {
            "performance_metrics": False,
            "date_range": False,
            "incremental": False,
            "custom_queries": False,
        },
    },
    "personio": {
        "display_name": "Personio",
        "category": "Other",
        "description": "Fetch employees, absences, and attendances from Personio HR",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "personio",
        "dlt_function": "personio_source",
        "wizard_enabled": False,  # Disabled: enterprise-only API
        "required_params": [
            {
                "name": "client_id_env",
                "type": "secret",
                "env_var": "PERSONIO_CLIENT_ID",
                "prompt": "Personio API Client ID",
                "help": "From Personio Settings > Integrations > API credentials",
            },
            {
                "name": "client_secret_env",
                "type": "secret",
                "env_var": "PERSONIO_CLIENT_SECRET",
                "prompt": "Personio API Client Secret",
                "help": "Secret paired with client ID",
            },
        ],
        "optional_params": [],
        "setup_guide": [
            "1. Log in to Personio",
            "2. Go to Settings > Integrations > API credentials",
            "3. Generate new credentials",
            "4. Copy Client ID and Secret to .env",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/personio",
        "popularity": 4,
        "capabilities": {
            "performance_metrics": False,
            "date_range": False,
            "incremental": True,
            "custom_queries": False,
        },
    },
}


# ============================================================================
# CATEGORY MAPPINGS
# ============================================================================

CATEGORIES = {
    "Local & Custom": ["local_files", "csv", "filesystem", "rest_api"],
    "Marketing & Analytics": [
        "facebook_ads",
        "google_ads",
        "google_analytics",
        "google_sheets",
        "matomo",
        "mux",
        "airtable",
    ],
    "Business & CRM": [
        "hubspot",
        "salesforce",
        "pipedrive",
        "freshdesk",
        "zendesk",
        "jira",
        "workable",
        "asana",
    ],
    "E-commerce & Payment": ["stripe", "shopify"],
    "Files & Storage": ["notion", "inbox"],
    "Databases": ["mongodb", "postgres"],  # postgres uses built-in sql_database
    "Streaming": ["kafka", "kinesis"],
    "Development": ["github"],
    "Communication": ["slack"],
    "Other": ["chess", "strapi", "personio"],  # scrapy not available
}


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================


def get_source_metadata(source_type: str) -> dict[str, Any] | None:
    """Get metadata for a specific source type"""
    return SOURCE_REGISTRY.get(source_type)


def get_sources_by_category(category: str) -> list[str]:
    """Get all source types in a category"""
    return CATEGORIES.get(category, [])


def get_all_categories() -> list[str]:
    """Get list of all categories"""
    return list(CATEGORIES.keys())


def get_popular_sources(limit: int = 10) -> list[str]:
    """Get most popular sources (sorted by popularity score)"""
    sources_with_popularity = [
        (source_type, metadata.get("popularity", 0))
        for source_type, metadata in SOURCE_REGISTRY.items()
    ]
    sorted_sources = sorted(sources_with_popularity, key=lambda x: x[1], reverse=True)
    return [source_type for source_type, _ in sorted_sources[:limit]]


def is_source_implemented(source_type: str) -> bool:
    """Check if a source has full metadata in registry"""
    return source_type in SOURCE_REGISTRY


def get_source_capabilities(source_type: str) -> dict[str, bool] | None:
    """Get capability flags for a specific source type."""
    metadata = SOURCE_REGISTRY.get(source_type)
    if metadata is None:
        return None
    return cast(dict[str, bool] | None, metadata.get("capabilities"))
