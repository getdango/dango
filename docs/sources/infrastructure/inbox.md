# Email Inbox (IMAP)

**Category:** Files & Storage | **Auth:** Basic | **Wizard:** Yes

## Setup

1. Enable IMAP access on your email provider
2. For Gmail: create an [App Password](https://myaccount.google.com/apppasswords)
3. Run `dango source add`, select **Email Inbox (IMAP)**, and enter your IMAP server, email, and password

## Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `host` | Yes | IMAP server (e.g., `imap.gmail.com`) |
| `email_account_env` | Yes | Email address (env var: `EMAIL_ACCOUNT`) |
| `password_env` | Yes | Password or app password (env var: `EMAIL_PASSWORD`) |
| `folder` | No | Folder to read (default: `INBOX`) |

## Known Limitations

- Wizard flow verified; real sync not tested in Phase 5
- Incremental loading supported
- For Gmail, use an App Password — regular passwords won't work with 2FA enabled
