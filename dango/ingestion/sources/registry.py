"""
Dango Source Registry

Metadata registry for all 33 supported data sources (31 dlt verified + CSV + REST API).

This registry is used by:
- DltPipelineRunner: To determine how to load each source type
- Source Wizard: To show categorized source selection and collect params
- CLI: To display source information and setup guides
"""

from typing import Dict, List, Any, Optional
from enum import Enum


class AuthType(str, Enum):
    """Authentication types for data sources"""
    NONE = "none"  # No auth needed (e.g., CSV)
    API_KEY = "api_key"  # Simple API key
    OAUTH = "oauth"  # OAuth 2.0 flow
    BASIC = "basic"  # Basic HTTP auth (username/password)
    SERVICE_ACCOUNT = "service_account"  # Service account credentials (e.g., Google)


# ============================================================================
# SOURCE REGISTRY - Top 10 Sources (Fully Implemented)
# ============================================================================

SOURCE_REGISTRY: Dict[str, Dict[str, Any]] = {
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
        "supported_in_v0": True,  # Fully tested for v0.0.1
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
    },

    "dlt_native": {
        "display_name": "dlt Native Source (Advanced)",
        "category": "Local & Custom",
        "description": "Use any dlt verified source or custom source not in Dango's registry. Advanced users only.",
        "auth_type": AuthType.NONE,  # Auth handled by source itself
        "dlt_package": None,  # User specifies
        "dlt_function": None,  # User specifies
        "supported_in_v0": True,  # Registry bypass implementation complete
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
    },

    "rest_api": {
        "display_name": "REST API (Generic)",
        "category": "Local & Custom",
        "description": "Connect to any custom REST API (e.g., Shopee, Lazada, internal APIs)",
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
                "choices": ["bearer", "api_key", "basic", "none"],
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
        "popularity": 8,
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
            "2. OR manually run: dango auth google_sheets",
            "3. Follow the browser OAuth flow to authenticate",
            "4. Get spreadsheet ID from URL: docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit",
            "5. Credentials are permanent (refresh token stored in .dlt/secrets.toml)",
            "6. To add/remove sheets later: edit .dango/sources.yml and run 'dango sync'",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/google_sheets",
        "cost_warning": "Subject to Google API quota limits",
        "supported_in_v0": True,  # OAuth implementation complete
        "popularity": 10,
    },

    "facebook_ads": {
        "display_name": "Facebook Ads",
        "category": "Marketing & Analytics",
        "description": "Load ad campaigns, insights, and performance metrics from Facebook Ads",
        "auth_type": AuthType.OAUTH,
        "dlt_package": "facebook_ads",
        "dlt_function": "facebook_ads_source",
        "pip_dependencies": [{"pip": "facebook-business", "import": "facebook_business"}],
        "required_params": [
            {
                "name": "account_id",
                "type": "string",
                "prompt": "Facebook Ads Account ID (e.g., act_123456789)",
                "help": "Find in Facebook Ads Manager URL",
            },
            {
                "name": "access_token_env",
                "type": "secret",
                "env_var": "FB_ACCESS_TOKEN",
                "prompt": "Access Token (use 'dango auth facebook_ads' to generate)",
                "help": "Long-lived User Access Token (60 days). Generate via 'dango auth facebook_ads' or manually at https://developers.facebook.com/tools/accesstoken. Requires 'ads_read' permission.",
            },
        ],
        "optional_params": [
            {
                "name": "start_date",
                "type": "date",
                "prompt": "Start date (YYYY-MM-DD)",
                "default": None,
            },
        ],
        "setup_guide": [
            "1. OAuth setup runs automatically during 'dango source add'",
            "2. OR manually run: dango auth facebook_ads",
            "3. Follow the prompts to exchange short-lived token for long-lived token",
            "4. IMPORTANT: Access token expires in 60 days",
            "5. Set a reminder to re-authenticate before expiry",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads",
        "cost_warning": "Rate limited: 200 calls/hour per user, 4800/day per app",
        "supported_in_v0": True,  # OAuth implementation complete
        "popularity": 9,
    },

    "google_analytics": {
        "display_name": "Google Analytics (GA4)",
        "category": "Marketing & Analytics",
        "description": "Load website analytics data from Google Analytics 4",
        "auth_type": AuthType.OAUTH,
        "dlt_package": "google_analytics",
        "dlt_function": "google_analytics",
        "pip_dependencies": [{"pip": "google-analytics-data", "import": "google.analytics.data_v1beta"}],
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
                "type": "date",
                "prompt": "Start date (YYYY-MM-DD)",
                "default": None,
            },
        ],
        # Default queries based on industry best practices (Calibrate Analytics)
        # GA4 Data API provides aggregated data only - each query becomes a table
        "default_config": {
            "queries": [
                {
                    "resource_name": "traffic",
                    "dimensions": ["date", "sessionSource", "sessionMedium", "sessionCampaignName", "deviceCategory"],
                    "metrics": ["sessions", "engagedSessions", "totalUsers", "newUsers", "averageSessionDuration", "bounceRate"]
                },
                {
                    "resource_name": "pages",
                    "dimensions": ["date", "pagePath", "pageTitle"],
                    "metrics": ["screenPageViews", "totalUsers", "userEngagementDuration", "sessions"]
                },
                {
                    "resource_name": "landing_pages",
                    "dimensions": ["date", "landingPage", "sessionSource", "sessionMedium", "deviceCategory"],
                    "metrics": ["sessions", "totalUsers", "engagedSessions", "bounceRate"]
                },
                {
                    "resource_name": "geo",
                    "dimensions": ["date", "country", "city"],
                    "metrics": ["sessions", "totalUsers", "engagedSessions"]
                }
            ]
        },
        "setup_guide": [
            "1. OAuth setup runs automatically during 'dango source add'",
            "2. OR manually run: dango auth google_analytics",
            "3. Follow the browser OAuth flow to authenticate",
            "4. Get GA4 Property ID from Admin > Property Settings",
            "5. Default queries load 4 tables: traffic, pages, landing_pages, geo",
            "6. Edit .dlt/config.toml to customize dimensions/metrics",
            "7. Schema evolves automatically - can add queries anytime",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/google_analytics",
        "cost_warning": "Subject to Google API quota limits. Data is aggregated (not event-level).",
        "supported_in_v0": True,  # OAuth implementation complete
        "popularity": 9,
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
                "choices": ["contacts", "companies", "deals", "tickets", "products", "quotes"],
                "default": ["contacts", "companies", "deals", "tickets"],
            },
        ],
        "setup_guide": [
            "1. Log in to HubSpot",
            "2. Go to Settings > Integrations > Private Apps",
            "3. Create new private app with required scopes",
            "4. Copy API key to .env as HUBSPOT_API_KEY",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/hubspot",
        "cost_warning": "Subject to HubSpot API limits (varies by plan)",
        "popularity": 9,
    },

    "salesforce": {
        "display_name": "Salesforce",
        "category": "Business & CRM",
        "description": "Load data from Salesforce CRM (accounts, contacts, opportunities, etc.)",
        "auth_type": AuthType.BASIC,
        "dlt_package": "salesforce",
        "dlt_function": "salesforce_source",
        "required_params": [
            {
                "name": "username_env",
                "type": "secret",
                "env_var": "SALESFORCE_USERNAME",
                "prompt": "Salesforce username",
            },
            {
                "name": "password_env",
                "type": "secret",
                "env_var": "SALESFORCE_PASSWORD",
                "prompt": "Salesforce password",
            },
            {
                "name": "security_token_env",
                "type": "secret",
                "env_var": "SALESFORCE_SECURITY_TOKEN",
                "prompt": "Salesforce security token",
                "help": "Reset at Setup > My Personal Information > Reset Security Token",
            },
        ],
        "optional_params": [
            {
                "name": "is_sandbox",
                "type": "boolean",
                "prompt": "Use sandbox environment?",
                "default": False,
            },
        ],
        "setup_guide": [
            "1. Get Salesforce username and password",
            "2. Reset security token (Setup > Reset Security Token)",
            "3. Check email for security token",
            "4. Add credentials to .env file",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/salesforce",
        "cost_warning": "Salesforce API limits depend on edition (check your limits)",
        "popularity": 8,
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
        "supported_in_v0": True,  # Fully tested for v0.0.1
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
                "default": ["Charge", "Customer", "Subscription"],
            },
            {
                "name": "start_date",
                "type": "date",
                "prompt": "Start date (YYYY-MM-DD)",
                "default": None,
            },
        ],
        "setup_guide": [
            "1. Go to https://dashboard.stripe.com/test/apikeys (or /apikeys for live mode)",
            "2. Click 'Reveal test key' for Secret key",
            "3. Copy the key (starts with sk_test_ for test mode, sk_live_ for production)",
            "4. Recommendation: Use test mode during development",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/stripe_analytics",
        "cost_warning": "No additional cost (included with Stripe account)",
        "popularity": 10,
    },

    "shopify": {
        "display_name": "Shopify",
        "category": "E-commerce & Payment",
        "description": "Load e-commerce data from Shopify (orders, customers, products, etc.)",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "shopify_dlt",  # Note: source name is shopify_dlt
        "dlt_function": "shopify_source",
        "required_params": [
            {
                "name": "shop_url",
                "type": "string",
                "prompt": "Shopify shop URL (e.g., myshop.myshopify.com)",
                "help": "Your Shopify store URL",
            },
            {
                "name": "api_key_env",
                "type": "secret",
                "env_var": "SHOPIFY_API_KEY",
                "prompt": "Shopify Admin API Access Token",
                "help": "Admin API Access Token (starts with 'shpat_'). Generate in Shopify Admin > Apps > Develop apps > Create app > Configure > Admin API access token. Required scopes: read_orders, read_customers, read_products.",
            },
        ],
        "optional_params": [
            {
                "name": "resources",
                "type": "multiselect",
                "prompt": "Resources to sync",
                "choices": ["orders", "customers", "products", "inventory", "transactions"],
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
            "1. Custom app setup runs automatically during 'dango source add'",
            "2. OR manually run: dango auth shopify",
            "3. Create custom app in Shopify Admin > Apps > Develop apps",
            "4. Configure Admin API scopes (read permissions needed)",
            "5. Install app and reveal Admin API access token",
            "6. Enter shop URL (e.g., mystore.myshopify.com) and access token",
            "7. Credentials are permanent (stored in .dlt/secrets.toml)",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/shopify",
        "cost_warning": "Included with Shopify plan",
        "supported_in_v0": False,  # Blocked: Shopify deprecating legacy auth Jan 2026, awaiting dlt update
        "popularity": 9,
    },

    # ========================================
    # DEVELOPMENT
    # ========================================
    "github": {
        "display_name": "GitHub",
        "category": "Development",
        "description": "Load repository data, issues, pull requests, and commits from GitHub",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "github",
        "dlt_function": "github_reactions",
        "required_params": [
            {
                "name": "access_token_env",
                "type": "secret",
                "env_var": "GITHUB_ACCESS_TOKEN",
                "prompt": "GitHub Personal Access Token (classic)",
                "help": "Personal Access Token (classic) starting with 'ghp_'. Generate at https://github.com/settings/tokens. Required scopes: repo, read:org, read:user",
            },
            {
                "name": "repos",
                "type": "list",
                "prompt": "Repository list (format: owner/repo, comma-separated)",
                "help": "Example: facebook/react,microsoft/vscode",
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
    },

    # ========================================
    # OTHER
    # ========================================
    "slack": {
        "display_name": "Slack",
        "category": "Communication",
        "description": "Load messages, channels, and user data from Slack",
        "auth_type": AuthType.OAUTH,
        "dlt_package": "slack",
        "dlt_function": "slack_source",
        "required_params": [
            {
                "name": "token_env",
                "type": "secret",
                "env_var": "SLACK_TOKEN",
                "prompt": "Slack Bot User OAuth Token (starts with xoxb-)",
                "help": "Bot User OAuth Token (starts with 'xoxb-'). Create at https://api.slack.com/apps > Your App > OAuth & Permissions. Required scopes: channels:history, channels:read, users:read. Must invite bot to channels you want to sync.",
            },
        ],
        "optional_params": [
            {
                "name": "channels",
                "type": "list",
                "prompt": "Channel IDs to sync (empty = all channels)",
                "default": None,
                "help": "Find channel ID by right-clicking channel > View channel details",
            },
            {
                "name": "start_date",
                "type": "date",
                "prompt": "Start date for messages (YYYY-MM-DD)",
                "default": None,
            },
        ],
        "setup_guide": [
            "1. Go to https://api.slack.com/apps",
            "2. Create new app from scratch",
            "3. Add Bot Token Scopes: channels:history, channels:read, users:read",
            "4. Install app to workspace",
            "5. Copy Bot User OAuth Token",
            "6. Add to .env as SLACK_TOKEN",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/slack",
        "cost_warning": "Subject to Slack API rate limits",
        "popularity": 7,
    },

    "zendesk": {
        "display_name": "Zendesk",
        "category": "Business & CRM",
        "description": "Load support tickets, users, and chat data from Zendesk Support, Talk, and Chat",
        "auth_type": AuthType.BASIC,
        "dlt_package": "zendesk",
        "dlt_function": "zendesk_support",
        "required_params": [
            {
                "name": "subdomain",
                "type": "string",
                "prompt": "Zendesk subdomain (e.g., mycompany from mycompany.zendesk.com)",
                "help": "Your Zendesk subdomain",
            },
            {
                "name": "email_env",
                "type": "secret",
                "env_var": "ZENDESK_EMAIL",
                "prompt": "Zendesk email",
            },
            {
                "name": "token_env",
                "type": "secret",
                "env_var": "ZENDESK_TOKEN",
                "prompt": "Zendesk API token",
                "help": "Generate at Admin > Channels > API",
            },
        ],
        "optional_params": [
            {
                "name": "start_date",
                "type": "date",
                "prompt": "Start date (YYYY-MM-DD)",
                "default": None,
            },
        ],
        "setup_guide": [
            "1. Log in to Zendesk as admin",
            "2. Go to Admin > Channels > API",
            "3. Enable Token Access",
            "4. Click '+' to add new API token",
            "5. Copy token and add to .env as ZENDESK_TOKEN",
            "6. Add your Zendesk email to .env as ZENDESK_EMAIL",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/zendesk",
        "cost_warning": "Subject to Zendesk API rate limits",
        "popularity": 7,
    },

    # Additional verified sources (skeleton metadata - to be expanded)
    "google_ads": {
        "display_name": "Google Ads",
        "category": "Marketing & Analytics",
        "description": "Load ad campaigns and performance data from Google Ads",
        "auth_type": AuthType.OAUTH,
        "dlt_package": "google_ads",
        "dlt_function": "google_ads",
        "pip_dependencies": [{"pip": "google-ads", "import": "google.ads"}],
        "required_params": [],  # OAuth handles credentials; developer_token and customer_id are collected during auth
        "optional_params": [
            {
                "name": "resources",
                "type": "multiselect",
                "prompt": "Resources to sync",
                "choices": ["customers", "campaigns", "change_events", "customer_clients"],
                "default": ["customers", "campaigns"],
            },
        ],
        "setup_guide": [
            "1. OAuth setup runs automatically during 'dango source add'",
            "2. OR manually run: dango auth google_ads",
            "3. Follow the browser OAuth flow to authenticate",
            "4. Enter Developer Token from Google Ads API Center",
            "5. Enter Customer ID (find in Google Ads account URL, no hyphens)",
            "6. Credentials are permanent (refresh token stored in .dlt/secrets.toml)",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/google_ads",
        "supported_in_v0": True,  # OAuth implementation complete
        "popularity": 7,
    },

    "matomo": {
        "display_name": "Matomo Analytics",
        "category": "Marketing & Analytics",
        "description": "Load reports and raw visits data from Matomo",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "matomo",
        "dlt_function": "matomo_reports",
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
                "type": "string",
                "prompt": "Site ID to track",
                "help": "Found in Matomo dashboard (usually a number like '1')",
            },
        ],
        "optional_params": [],
        "setup_guide": [
            "1. Log in to Matomo",
            "2. Go to Settings > Platform > API",
            "3. Create API token",
            "4. Add to .env as MATOMO_API_TOKEN",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/matomo",
        "popularity": 5,
    },

    "mux": {
        "display_name": "Mux",
        "category": "Marketing & Analytics",
        "description": "Load video analytics data from Mux",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "mux",
        "dlt_function": "mux_source",
        "required_params": [
            {
                "name": "api_access_token_env",
                "type": "secret",
                "env_var": "MUX_API_ACCESS_TOKEN",
                "prompt": "Mux API Access Token ID",
                "help": "From Mux Dashboard > Settings > Access Tokens",
            },
            {
                "name": "api_secret_key_env",
                "type": "secret",
                "env_var": "MUX_API_SECRET_KEY",
                "prompt": "Mux API Secret Key",
                "help": "Secret key paired with access token",
            },
        ],
        "optional_params": [],
        "setup_guide": [
            "1. Log in to Mux Dashboard",
            "2. Go to Settings > Access Tokens",
            "3. Create new token with read permissions",
            "4. Copy both Token ID and Secret Key to .env",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/mux",
        "popularity": 4,
    },

    "airtable": {
        "display_name": "Airtable",
        "category": "Marketing & Analytics",
        "description": "Load tables from Airtable bases",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "airtable",
        "dlt_function": "airtable_source",
        "pip_dependencies": [{"pip": "pyairtable", "import": "pyairtable"}],
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
    },

    "pipedrive": {
        "display_name": "Pipedrive",
        "category": "Business & CRM",
        "description": "Load deals, contacts, and activities from Pipedrive CRM",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "pipedrive",
        "dlt_function": "pipedrive_source",
        "required_params": [
            {
                "name": "api_key_env",
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
                "choices": ["activities", "deals", "deals_flow", "deals_participants", "files", "filters", "leads", "notes", "organizations", "persons", "pipelines", "products", "projects", "stages", "tasks", "users"],
                "default": ["activities", "deals", "persons", "organizations"],
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
    },

    "freshdesk": {
        "display_name": "Freshdesk",
        "category": "Business & CRM",
        "description": "Load support tickets, agents, and companies from Freshdesk",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "freshdesk",
        "dlt_function": "freshdesk_source",
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
                "choices": ["tickets", "agents", "companies", "contacts", "groups", "roles", "skills"],
                "default": ["tickets", "agents", "companies"],
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
    },

    "jira": {
        "display_name": "Jira",
        "category": "Business & CRM",
        "description": "Load issues, users, workflows, and projects from Jira",
        "auth_type": AuthType.BASIC,
        "dlt_package": "jira",
        "dlt_function": "jira",
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
    },

    "workable": {
        "display_name": "Workable",
        "category": "Business & CRM",
        "description": "Load candidates, jobs, and events from Workable ATS",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "workable",
        "dlt_function": "workable_source",
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
    },

    "asana": {
        "display_name": "Asana",
        "category": "Business & CRM",
        "description": "Load tasks, projects, and workspaces from Asana",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "asana_dlt",  # Note: source name is asana_dlt
        "dlt_function": "asana_source",
        "required_params": [
            {
                "name": "access_token_env",
                "type": "secret",
                "env_var": "ASANA_ACCESS_TOKEN",
                "prompt": "Asana Personal Access Token",
                "help": "Generate at https://app.asana.com/0/my-apps",
            },
        ],
        "optional_params": [
            {
                "name": "resources",
                "type": "multiselect",
                "prompt": "Resources to sync",
                "choices": ["workspaces", "projects", "sections", "tags", "tasks", "stories", "teams", "users"],
                "default": ["workspaces", "projects", "tasks"],
            },
        ],
        "setup_guide": [
            "1. Go to https://app.asana.com/0/my-apps",
            "2. Click 'Create new token'",
            "3. Give it a description and create",
            "4. Copy token to .env as ASANA_ACCESS_TOKEN",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/asana",
        "popularity": 7,
    },

    "notion": {
        "display_name": "Notion",
        "category": "Files & Storage",
        "description": "Load pages and databases from Notion",
        "auth_type": AuthType.OAUTH,
        "dlt_package": "notion",
        "dlt_function": "notion_databases",
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
                "help": "Format: [{\"id\": \"db_id\", \"use_name\": \"my_db\"}] - Leave empty to sync all databases",
            },
            {
                "name": "page_ids",
                "type": "list",
                "prompt": "Page IDs to sync (comma-separated, empty = all)",
                "default": None,
                "help": "Comma-separated list of Notion page IDs",
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
    },

    "inbox": {
        "display_name": "Email Inbox (IMAP)",
        "category": "Files & Storage",
        "description": "Read messages and attachments from email inbox via IMAP",
        "auth_type": AuthType.BASIC,
        "dlt_package": "inbox",
        "dlt_function": "inbox_source",
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
    },

    "mongodb": {
        "display_name": "MongoDB",
        "category": "Databases",
        "description": "Load collections from MongoDB databases with incremental support",
        "auth_type": AuthType.BASIC,
        "dlt_package": "mongodb",
        "dlt_function": "mongodb",
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
    },

    "kafka": {
        "display_name": "Apache Kafka",
        "category": "Streaming",
        "description": "Extract messages from Kafka topics",
        "auth_type": AuthType.NONE,
        "dlt_package": "kafka",
        "dlt_function": "kafka_consumer",
        "pip_dependencies": [{"pip": "confluent-kafka", "import": "confluent_kafka"}],
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
    },

    "kinesis": {
        "display_name": "Amazon Kinesis",
        "category": "Streaming",
        "description": "Read messages from Kinesis streams",
        "auth_type": AuthType.SERVICE_ACCOUNT,
        "dlt_package": "kinesis",
        "dlt_function": "kinesis_stream",
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
            "3. Create JSON: {\"aws_access_key_id\": \"...\", \"aws_secret_access_key\": \"...\", \"region_name\": \"us-east-1\"}",
            "4. Add to .env as AWS_CREDENTIALS",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/kinesis",
        "popularity": 6,
    },

    "chess": {
        "display_name": "Chess.com",
        "category": "Other",
        "description": "Load player profiles and games from Chess.com API",
        "auth_type": AuthType.NONE,
        "dlt_package": "chess",
        "dlt_function": "source",
        "required_params": [
            {
                "name": "player_usernames",
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
    },

    "strapi": {
        "display_name": "Strapi",
        "category": "Other",
        "description": "Load content from Strapi headless CMS",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "strapi",
        "dlt_function": "strapi_source",
        "required_params": [
            {
                "name": "base_url",
                "type": "string",
                "prompt": "Strapi instance URL (e.g., https://cms.example.com)",
                "help": "Base URL of your Strapi installation",
            },
            {
                "name": "api_key_env",
                "type": "secret",
                "env_var": "STRAPI_API_KEY",
                "prompt": "Strapi API Token",
                "help": "Create at Settings > API Tokens",
            },
        ],
        "optional_params": [],
        "setup_guide": [
            "1. Log in to Strapi admin",
            "2. Go to Settings > API Tokens",
            "3. Create new token with read permissions",
            "4. Copy token to .env as STRAPI_API_KEY",
        ],
        "docs_url": "https://dlthub.com/docs/dlt-ecosystem/verified-sources/strapi",
        "popularity": 5,
    },

    "personio": {
        "display_name": "Personio",
        "category": "Other",
        "description": "Fetch employees, absences, and attendances from Personio HR",
        "auth_type": AuthType.API_KEY,
        "dlt_package": "personio",
        "dlt_function": "personio_source",
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
    },
}


# ============================================================================
# CATEGORY MAPPINGS
# ============================================================================

CATEGORIES = {
    "Local & Custom": ["csv", "rest_api"],
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
    "Databases": ["mongodb"],  # postgres/sql_database are built-in, not verified
    "Streaming": ["kafka", "kinesis"],
    "Development": ["github"],
    "Communication": ["slack"],
    "Other": ["chess", "strapi", "personio"],  # scrapy not available
}


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================


def get_source_metadata(source_type: str) -> Optional[Dict[str, Any]]:
    """Get metadata for a specific source type"""
    return SOURCE_REGISTRY.get(source_type)


def get_sources_by_category(category: str) -> List[str]:
    """Get all source types in a category"""
    return CATEGORIES.get(category, [])


def get_all_categories() -> List[str]:
    """Get list of all categories"""
    return list(CATEGORIES.keys())


def get_popular_sources(limit: int = 10) -> List[str]:
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
