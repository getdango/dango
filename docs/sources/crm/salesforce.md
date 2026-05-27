# Salesforce

**Category:** Business & CRM | **Auth:** Service Account | **Wizard:** Yes

## Setup

1. Log in to Salesforce
2. Go to **Settings > Reset My Security Token** (sends token via email)
3. Run `dango source add` and select **Salesforce**
4. Enter your email, password, and security token when prompted

## Configuration

No additional parameters — credentials are collected during wizard setup.

**Pip dependency:** `simple-salesforce` (installed automatically)

Available resources: `account`, `contact`, `lead`, `opportunity`, `campaign`, `task`, `event`, `sf_user`, `user_role`, `product_2`

## Known Limitations

- Wizard flow verified; real sync not tested in Phase 5
- Salesforce credentials use a nested structure — Dango's `_CREDENTIAL_RESTRUCTURE` map handles the transformation automatically
- Incremental loading supported
