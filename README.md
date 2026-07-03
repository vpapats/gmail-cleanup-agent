# Gmail Cleanup Agent (Safe Triage)

A conservative, production-oriented Gmail triage system for personal inbox cleanup.

## What it does

- Connects to Gmail using OAuth2 with refreshable tokens.
- Classifies messages as `important`, `action_needed`, `low_priority`, or `review`.
- Protects potentially important/sensitive emails (attachments, replies, finance/legal/work signals).
- Protects starred Gmail messages from summary trashing.
- Generates a one-line summary before any destructive action.
- Sends a daily `Today's GMAIL FOMO summary` email for reviewed/noisy messages.
- Supports **shadow mode** (no deletion) and **active mode** (trash enabled).
- Logs every decision/action to persistent JSONL + CSV audit files.
- Applies status labels in Gmail:
  - `AI/Important`
  - `AI/Action-Needed`
  - `AI/Low-Priority`
  - `AI/Review`
- Sends summarized `review` and `low_priority` messages to Trash only after the digest email is sent.
- Marks summarized messages with `AI/FOMO-Summarized`.
- Restores false positives marked with `AI/Wrongly-Trashed` and protects their senders.

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

4. Export secrets (or load from a secret manager):

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`
- `OPENROUTER_API_KEY` for model-based sorting through OpenRouter.
- The automation is pinned to `google/gemini-3.1-flash-lite`.

The Gmail OAuth token must include `gmail.modify`, `gmail.labels`, and `gmail.send`.
Regenerate `GOOGLE_REFRESH_TOKEN` after adding the daily summary email feature.

5. Create runtime config:

```bash
cp config/settings.example.yaml config/settings.yaml
```

Keep `mode: shadow` for initial rollout.

## Safe initial configuration

- Use narrow `approved_trash_senders` (newsletter/no-reply only).
- Use `candidate_queries` that exclude existing `AI/*` labels so already-checked mail is not reviewed again.
- Keep `max_messages_per_run: 50` so the daily summary stays readable.
- Use `candidate_scan_limit` to scan deeper into the inbox backlog without reviewing more than 50 emails per run.
- Keep `use_model: true` to let Gemini scan email text and supported attachments.
- Keep `mode: shadow` until you have reviewed several audit runs.
- Keep high `min_trash_confidence` (e.g., `0.93+`).

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

4. Inspect Gmail labels (`AI/Important`, `AI/Action-Needed`, `AI/Low-Priority`, and `AI/Review`).

## Daily GMAIL FOMO summary

The scheduled GitHub Actions workflow runs once each morning. During daylight saving time
in Athens, the cron is set to 06:00 UTC, which is 09:00 Europe/Athens.
GitHub Actions can start a scheduled run a little late, and the summary email is sent
after setup, Gmail checks, and AI review complete, so the delivery time can vary.

GMAIL FOMO gradually works through inbox backlog by selecting inbox messages that do not
already have one of its AI labels. It scans deeper than the daily review limit, processes
older unreviewed messages first, and still reviews at most 50 emails in a run.

When `daily_summary.enabled` is true:

- `review` and `low_priority` emails are summarized with the selected OpenRouter model.
- The digest is sent to the authenticated Gmail account.
- Each reviewed email is marked with `AI/FOMO-Summarized`.
- In `active` mode, summarized emails are moved to Trash only after the digest email sends successfully.

## Activate real trashing

1. Confirm no false positives across multiple shadow runs.
2. Keep sender list narrow.
3. Set `mode: active` in `config/settings.yaml`.
4. Re-run triage and monitor `audit/audit.csv` and Gmail Trash.

## Correct a wrongly trashed message

1. In Gmail, apply the label `AI/Wrongly-Trashed` to the message.
2. The next automation run restores it to the inbox, removes `AI/Low-Priority`, and applies `AI/Important`.
3. Future messages from the same sender are protected from automatic trashing while that feedback label remains.

`AI/Wrongly-Trashed` is a feedback control, not a fifth category. Remove it from all messages
from that sender if you want to stop protecting the sender.

## Automation (GitHub Actions)

This repository includes `.github/workflows/gmail-triage.yml` to run triage automatically
once each morning at 09:00 Europe/Athens during daylight saving time.

You can also trigger it manually with **Run workflow** in GitHub Actions.
This is the production scheduler path (GitHub-hosted runners), not a Colab scheduler.

Required repository secrets:

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`
- `OPENROUTER_API_KEY` for model-based sorting through OpenRouter.
- Model: `google/gemini-3.1-flash-lite`.
- Optional variable: `OPENROUTER_MAX_ATTACHMENT_BYTES` defaults to `750000`.

The workflow runs:

```bash
PYTHONPATH=. python scripts/run_triage.py --config config/settings.yaml --audit-dir audit
```

If a workflow run fails quickly with exit code 1, check the **Validate required secrets**
step in the run logs/summary and ensure these GitHub repository secrets are set:
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `GOOGLE_REFRESH_TOKEN`.

If the **Validate Gmail auth** step reports `invalid_grant`, Google rejected the stored
refresh token. Regenerate it with `scripts/gmail_oauth_bootstrap.py`, then replace the
`GOOGLE_REFRESH_TOKEN` repository secret. Scheduled runs skip triage while auth is
invalid so GitHub does not send repeated failure emails; manual runs still fail loudly.

## Notes on safety

- If confidence is low, the system chooses `review`.
- The model cannot upgrade a non-low-priority rule decision into `low_priority`.
- Only `low_priority` messages at or above the configured confidence threshold can be trashed in active mode.
- Starred Gmail messages are always protected and labeled important instead of being trashed.
- User feedback overrides classification and protects the sender.
