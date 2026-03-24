# Dango Data Sources

Dango supports 33 data sources via wizard-guided setup, REST API configuration, and custom dlt scripts.

## Quick Start

```bash
dango source add    # Interactive wizard — pick a source, enter credentials
dango sync          # Run your first sync
```

## Sources by Category

### Marketing & Analytics

| Source | Auth | Incremental | Wizard |
|--------|------|-------------|--------|
| [Facebook Ads](marketing/facebook-ads.md) | OAuth | Yes | Yes |
| [Google Ads](marketing/google-ads.md) | OAuth | Yes | Yes |
| [Google Analytics (GA4)](marketing/google-analytics.md) | OAuth | Yes | Yes |
| [HubSpot](crm/hubspot.md) | API Key | Yes | Yes |
| [Matomo](marketing/matomo.md) | API Key | — | No (disabled) |
| [Airtable](other/airtable.md) | API Key | No | Yes |
| [Mux](other/mux.md) | API Key | No | Yes |

### CRM & Business

| Source | Auth | Incremental | Wizard |
|--------|------|-------------|--------|
| [Salesforce](crm/salesforce.md) | Service Account | Yes | Yes |
| [Pipedrive](crm/pipedrive.md) | API Key | Yes | Yes |
| [Freshdesk](crm/freshdesk.md) | API Key | Yes | Yes |
| [Zendesk](crm/zendesk.md) | Basic | Yes | Yes |
| [Workable](other/workable.md) | API Key | Yes | Yes |

### Productivity & Communication

| Source | Auth | Incremental | Wizard |
|--------|------|-------------|--------|
| [Slack](productivity/slack.md) | API Key | Yes | Yes |
| [Notion](productivity/notion.md) | API Key | No | Yes |
| [GitHub](productivity/github.md) | API Key | Yes | Yes |

### E-commerce & Payment

| Source | Auth | Incremental | Wizard |
|--------|------|-------------|--------|
| [Stripe](ecommerce/stripe.md) | API Key | Yes | Yes |

### Databases

| Source | Auth | Incremental | Wizard |
|--------|------|-------------|--------|
| [PostgreSQL](databases/postgresql.md) | Connection URL | Yes | Yes |
| [MongoDB](databases/mongodb.md) | Connection URL | Yes | Yes |

### Infrastructure & Streaming

| Source | Auth | Incremental | Wizard |
|--------|------|-------------|--------|
| [Apache Kafka](infrastructure/kafka.md) | Config JSON | Yes | Yes |
| [Amazon Kinesis](infrastructure/kinesis.md) | AWS Credentials | Yes | Yes |
| [Email Inbox (IMAP)](infrastructure/inbox.md) | Basic | Yes | Yes |

### Local & Custom

| Source | Auth | Incremental | Wizard |
|--------|------|-------------|--------|
| [Local Files](local/local-files.md) | None | Yes | Yes |
| [Google Sheets](local/google-sheets.md) | OAuth | No | Yes |
| [REST API (Generic)](non-wizard-sources.md#rest-api) | Configurable | Yes | Yes |
| [dlt Native (Advanced)](non-wizard-sources.md#dlt-native-source) | Varies | Varies | Yes |

### Other

| Source | Auth | Incremental | Wizard |
|--------|------|-------------|--------|
| [Chess.com](other/chess.md) | None | No | Yes |

## Choosing a Source Type

- **Have an API key?** Use the wizard — `dango source add` and pick your source.
- **Need a custom REST API?** Use the [REST API source](non-wizard-sources.md#rest-api) — declarative config, no Python needed.
- **Want full control?** Use a [dlt Native source](non-wizard-sources.md#dlt-native-source) — write Python with `@dlt.source`/`@dlt.resource`.
- **Source not listed?** REST API covers most HTTP APIs. For databases or message queues, check the [dlt verified sources](https://dlthub.com/docs/dlt-ecosystem/verified-sources) and use dlt Native.

## Disabled Sources

These sources are registered but disabled due to known issues:

| Source | Reason |
|--------|--------|
| Shopify | Pending investigation — dlt vendor may be incompatible with Jan 2026 API deprecation |
| Matomo | Token passed via GET parameter (security risk) |
| Jira | Wrong endpoint in dlt source |
| Asana | Asana SDK removed from dlt source |
| Strapi | Untested, requires Docker Strapi instance |
| Personio | Enterprise-only API |

See also: [OAuth Pattern](oauth-pattern.md) | [Known Limitations](known-limitations.md)
