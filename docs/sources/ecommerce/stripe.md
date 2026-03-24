# Stripe

**Category:** E-commerce & Payment | **Auth:** API Key | **Wizard:** Yes

## Setup

1. Go to [dashboard.stripe.com/apikeys](https://dashboard.stripe.com/apikeys)
2. Reveal your Secret key (starts with `sk_test_` or `sk_live_`)
3. Copy the key
4. Run `dango source add`, select **Stripe**, and enter the key

## Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `stripe_secret_key_env` | Yes | Stripe API Key (env var: `STRIPE_API_KEY`) |
| `endpoints` | No | Endpoints to sync (default: all) |
| `start_date` | No | Start date (default: `90daysAgo`) |

Available endpoints: Charge, Customer, Subscription, Invoice, Product, Price, PaymentIntent

**Pip dependency:** `stripe` (installed automatically)

## Known Limitations

- Wizard flow verified; real sync not tested in Phase 5
- Incremental loading supported
- Use test mode keys (`sk_test_`) during development
