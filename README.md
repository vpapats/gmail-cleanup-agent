# Gmail Cleanup Agent (Safe Triage)

A conservative, production-oriented Gmail triage system for personal inbox cleanup.

## What it does

- Connects to Gmail using OAuth2 with refreshable tokens.
- Classifies messages into `keep`, `review`, or `summarize_then_trash`.
- Protects potentially important/sensitive emails (attachments, replies, finance/legal/work signals).
- Generates a one-line summary before any destructive action.
- Supports **shadow mode** (no deletion) and **active mode** (trash enabled).
- Logs every decision/action to persistent JSONL + CSV audit files.
- Applies status labels in Gmail:
  - `AI/Protected`
  - `AI/Review`
  - `AI/Kept`
  - `AI/Trash-After-Summary`

## Project structure

```
.
├── src/
│   ├── auth.py
│   ├── gmail_client.py
│   ├── classifier.py
│   ├── triage.py
│   ├── audit.py
│   └── models.py
├── scripts/
│   ├── setup_labels.py
│   ├── run_triage.py
│   ├── validate.py
│   └── gmail_oauth_bootstrap.py
├── config/
│   └── settings.example.yaml
├── docs/
│   └── gmail-oauth-setup.md
└── README.md
```

## Setup

1. Create Google OAuth Desktop credentials and enable Gmail API.
2. Install dependencies:

```bash
python -m pip install -r requirements.txt
```

3. Bootstrap token:

```bash
python scripts/gmail_oauth_bootstrap.py --client-json /path/to/client_secret.json
```

In Colab/headless environments, use:

```bash
python scripts/gmail_oauth_bootstrap.py --client-json /path/to/client_secret.json --no-browser
```

If Google returns `Error 400: invalid_request` with `Missing required parameter: redirect_uri`,
confirm your OAuth client has:

- `http://127.0.0.1:8765/callback`

4. Export secrets (or load from a secret manager):

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`
- Optional for model stage: `OPENAI_API_KEY`, `OPENAI_MODEL`

5. Create runtime config:

```bash
cp config/settings.example.yaml config/settings.yaml
```

Keep `mode: shadow` for initial rollout.

Use exact senders (e.g. `noreply@example.com`) or domain lanes with `@domain.tld`.

## Safe initial configuration

- Use narrow `approved_trash_senders` (newsletter/no-reply only).
- Use restrictive query buckets in `candidate_queries`.
- Keep `use_model: false` initially.
- Keep high `min_trash_confidence` (e.g., `0.93+`).
- Keep `max_trash_per_run` and `max_trash_per_sender` small during validation.

## Validation flow

1. Ensure labels:

```bash
python scripts/setup_labels.py --config config/settings.yaml
```

2. Run shadow triage:

```bash
python scripts/run_triage.py --config config/settings.yaml --audit-dir audit
```

3. Review potential trash candidates:

```bash
python scripts/validate.py --audit-csv audit/audit.csv
```

4. Inspect Gmail labels (`AI/Review`, `AI/Trash-After-Summary`) manually.

## Activate real trashing

1. Confirm no false positives across multiple shadow runs.
2. Keep sender list narrow.
3. Set `mode: active` in `config/settings.yaml`.
4. Re-run triage and monitor `audit/audit.csv` and Gmail Trash.

## Notes on safety

- If confidence is low, the system chooses `review`.
- Model stage (if enabled) cannot upgrade non-trash decisions into trash automatically.
- Protection signals force `review` regardless of low-value hints.
