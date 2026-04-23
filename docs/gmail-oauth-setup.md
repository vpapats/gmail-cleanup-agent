# Gmail OAuth setup

## Required API access

Enable Gmail API in Google Cloud for your project.

## OAuth client

Create OAuth credentials for a **Desktop app** and download the JSON credentials file.

## Bootstrap refresh token

```bash
python scripts/gmail_oauth_bootstrap.py --client-json /path/to/client_secret.json
```

The script stores token data at `.secrets/token.json` by default.
For Colab or other headless environments:

```bash
python scripts/gmail_oauth_bootstrap.py --client-json /path/to/your_client.json --no-browser
```

Then copy the full redirected localhost URL after consent and paste it back into the prompt.


## Environment variables

Set these before running triage:

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`

Optional model settings:

- `OPENAI_API_KEY`
- `OPENAI_MODEL`

## Scope model

The runtime uses least-privilege Gmail scopes:

- `https://www.googleapis.com/auth/gmail.modify`
- `https://www.googleapis.com/auth/gmail.labels`


### Redirect URI requirement

Make sure your OAuth client has this exact authorised redirect URI:

- `http://127.0.0.1:8765/callback`
