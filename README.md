# Gmail Cleanup Agent

This repository contains a professional‑grade Gmail automation agent designed to reduce inbox noise by classifying and disposing of low‑value informational emails while protecting any messages that may be important.  The agent integrates with Gmail, Google Sheets and an LLM provider (via the OpenAI‑compatible API) to automatically summarise newsletters and promotional emails, move them to the trash, and produce an audit log and digest.

The project is organised to support development and deployment via GitHub Actions.  A local bootstrap script obtains OAuth credentials for Gmail.  Secrets are stored using GitHub repository secrets.  The agent runs in **shadow mode** (no deletion) until explicitly enabled.

## Features

* **Protected senders and signals** — Hard rules ensure that direct conversations, attachments, invoices, administrative notices, leads and personal emails are never auto‑trashed.
* **LLM‑based classification** — Low‑risk mail (e.g. newsletters, market briefings and promos) is summarised and classified by an LLM via an OpenAI‑compatible API.  Only clearly low‑value items are eligible for auto‑deletion.
* **Narrow initial trash lane** — The first version only considers explicitly approved newsletter senders for auto‑trash.  Everything else is kept or flagged for manual review.
* **Audit log and digest** — Every decision is logged to a Google Sheet, and a digest email summarises the agent’s actions after each run.
* **GitHub Actions workflow** — A scheduled workflow runs the agent on a regular basis.  Secrets for Gmail access and the LLM provider are stored in the repository’s settings.

## Getting started

1. **Clone this repository** to your machine and navigate into it.

2. **Set up a Google Cloud project**:
   - Enable the Gmail API.
   - Configure an OAuth consent screen (external or internal as required).
   - Create OAuth client credentials for a *desktop application*.
   - Add the loopback redirect URI `http://127.0.0.1:8765/callback`.
   - Download the client JSON file.

3. **Bootstrap a refresh token** using the local script:

   ```bash
   python3 -m pip install --upgrade google-auth google-auth-oauthlib google-api-python-client
   python3 -m scripts.gmail_oauth_bootstrap --client-json path/to/your_client.json
   ```

   The script will open a browser for you to grant Gmail access.  It will store a `token.json` in the `.secrets` directory.  **Do not commit this file.**

4. **Create repository secrets** in GitHub Settings → Secrets and variables → Actions:
   - `GOOGLE_CLIENT_ID`: found in your client JSON.
   - `GOOGLE_CLIENT_SECRET`: found in your client JSON.
   - `GOOGLE_REFRESH_TOKEN`: the refresh token stored in `.secrets/token.json` after bootstrap.
   - `LLM_API_KEY`: your API key for an OpenAI‑compatible service (e.g. OpenRouter or OpenAI).

5. **Configure protected senders and trash lane** in `config/config.py`.  Start with a narrow list of newsletter domains/senders only.

6. **Run the agent in shadow mode** using the workflow.  Inspect the Google Sheet log to ensure no important mail is flagged.  Only then enable auto‑trash by toggling the relevant flag in the configuration.

See `docs/gmail-oauth-setup.md` for more detailed instructions on setting up Google OAuth.

## Directory structure

```
.
├── README.md
├── docs/
│   └── gmail-oauth-setup.md
├── scripts/
│   ├── gmail_auth.py
│   ├── gmail_oauth_bootstrap.py
│   ├── validate_gmail_auth.py
│   └── run_agent.py
├── config/
│   └── config.py
├── .github/
│   └── workflows/
│       ├── gmail-auth-check.yml
│       └── gmail-agent.yml
├── .secrets/
│   └── .gitignore
└── requirements.txt
```

## License

This project is provided as-is without warranty.  You may use and adapt it for personal use under the terms of the MIT license included with this repository.
