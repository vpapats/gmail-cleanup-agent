# Gmail OAuth setup

## Required API access

Enable Gmail API in Google Cloud for your project.

## OAuth client

Create OAuth credentials and download the JSON credentials file. The current
bootstrap helper uses this redirect URI:

```text
http://localhost:8765/
```

Add it to the OAuth client's authorised redirect URIs before running the helper.

## Bootstrap refresh token

```bash
python scripts/gmail_oauth_bootstrap.py --client-json /path/to/client_secret.json
```

The script stores token data at `.secrets/token.json` by default. Copy only the
`refresh_token` value into the GitHub Actions secret named `GOOGLE_REFRESH_TOKEN`.

## Environment variables

Set these before running triage:

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`
- `OPENROUTER_API_KEY`

Optional model settings:

- `OPENROUTER_MODEL`
- `OPENROUTER_MAX_ATTACHMENT_BYTES`

## Scope model

The runtime uses least-privilege Gmail scopes:

- `https://www.googleapis.com/auth/gmail.modify`
- `https://www.googleapis.com/auth/gmail.labels`
- `https://www.googleapis.com/auth/gmail.send`

Regenerate `GOOGLE_REFRESH_TOKEN` after adding `gmail.send`; old refresh tokens
created without that scope cannot send the daily summary email.
