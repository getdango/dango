# Matomo Analytics

**Category:** Marketing & Analytics | **Auth:** API Key | **Wizard:** Disabled

## Status

Matomo is registered but **disabled** in the wizard. The dlt Matomo source passes the auth token via a GET query parameter, which is a security risk (tokens appear in server access logs and browser history).

## Known Limitations

- **Duplicate row risk.** Like GA4, the vendored Matomo source uses append mode with no dedup.
- Token-in-URL security risk prevents wizard enablement.

## Workaround

Use the [dlt Native source](../non-wizard-sources.md#dlt-native-source) to write a custom Matomo integration that passes the token via headers instead.
