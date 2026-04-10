# Google OAuth Setup for the Gmail Cleanup Agent

This guide explains how to enable the Gmail API, create OAuth credentials and obtain a refresh token for the Gmail cleanup agent.  The process follows Google’s recommended flow for **installed desktop applications** and stores the resulting token locally in the `.secrets` directory.  Once the refresh token is obtained, you can configure GitHub Actions to use it without ever exposing your Google credentials in code.

## 1. Create a Google Cloud project

1. Visit [Google Cloud Console](https://console.cloud.google.com/) and create a new project.
2. In the **APIs & Services** dashboard, click **Enable APIs and Services** and enable the **Gmail API**.
3. Under **OAuth consent screen**, configure a consent screen.  For personal use, you can choose **External** and set yourself as the test user.  Provide the necessary app name and developer contact information.

## 2. Create OAuth client credentials

1. In **APIs & Services** → **Credentials**, click **Create credentials** and choose **OAuth client ID**.
2. Select **Desktop app** as the application type.  Give it a name like “Gmail Cleanup Agent”.
3. Add a redirect URI for the loopback flow: `http://127.0.0.1:8765/callback`.
4. After creation, click **Download JSON**.  Save this file securely; it contains your client ID and secret.

## 3. Install dependencies

Install the required Python packages.  It is recommended to use a virtual environment.

```bash
python3 -m pip install --upgrade google-auth google-auth-oauthlib google-api-python-client
```

## 4. Bootstrap a refresh token

The repository includes a helper script `scripts/gmail_oauth_bootstrap.py` that handles the OAuth flow and stores a refresh token in `.secrets/token.json`.  Run it as follows, replacing the path to your downloaded client JSON file:

```bash
python3 -m scripts.gmail_oauth_bootstrap --client-json path/to/your_client.json
```

The script will prompt you to open a browser window at a Google URL.  Sign in with the Gmail account you wish the agent to operate on and grant access.  After a successful authorization, the script stores a `token.json` file in the `.secrets` folder.  **Do not commit this file to version control.**

If you need to regenerate the token or add scopes, you can delete `.secrets/token.json` and run the bootstrap script again.

## 5. Configure GitHub repository secrets

After obtaining the refresh token, copy the following values into your repository’s **Settings → Secrets and variables → Actions**:

| Secret Name           | Value                                   |
|-----------------------|-----------------------------------------|
| `GOOGLE_CLIENT_ID`    | The `client_id` from your client JSON   |
| `GOOGLE_CLIENT_SECRET`| The `client_secret` from your client JSON|
| `GOOGLE_REFRESH_TOKEN`| Found in `.secrets/token.json` after bootstrap |
| `LLM_API_KEY`         | Your API key for an OpenAI‑compatible LLM service |

These secrets allow the GitHub Actions workflows to refresh an access token and call the LLM provider.  Do not include them in your source code.

## 6. Next steps

* Configure protected senders and a narrow initial trash lane in `config/config.py`.
* Test the agent in shadow mode (no deletion) by running the GitHub Actions workflow `gmail-agent.yml`.  Review the audit log in Google Sheets.
* Only enable auto‑trash after verifying that no important mail is flagged.

For more information on Google OAuth flows, see Google’s official documentation on [installed app authorization](https://developers.google.com/identity/protocols/oauth2#installed).
