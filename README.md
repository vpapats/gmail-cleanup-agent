# Gmail Cleanup Agent (Safe Triage)

A conservative, production-oriented Gmail triage system for personal inbox cleanup.

## Weekly quality auditor and manual review

Every Monday at 09:00 `Europe/Athens`, the auditor downloads the previous week's
successful daily-run artifacts and independently re-evaluates each unique decision with
`google/gemini-3.1-flash-lite`. A result is accepted automatically only when the evidence
is clear and auditor confidence is at least `0.85`.

The audit itself never changes Gmail labels. It sends one concise Greek email with a
private HTML review attachment. The review table opens each Gmail message and shows its
receipt date, current label, auditor recommendation, evidence, and a final-label dropdown.
The dropdown starts with the current label, so leaving it unchanged confirms the original
classification. A fixed `Confirm & Apply` button submits all decisions together.

The attachment posts only the signed Review ID, opaque item IDs, and the three allowed
label values to the private Google Apps Script relay in
`apps_script/weekly_review_relay/`. The relay contains no Gmail credentials. It verifies
the form signature and starts the repository's apply workflow with a fine-grained GitHub
token stored in Script Properties. Sender, subject, receipt date, and evidence remain only
inside the inbox attachment because this repository is public.

The reviewer has exactly three choices: `kept`, `action_needed`, and
`digest_and_trash`. There are no Retry, Skip, or new Gmail labels. Technical retrieval
failures are retried once, reported separately, and excluded from ambiguous cases.

`Confirm & Apply` starts `.github/workflows/apply-weekly-review.yml`. It verifies that Gmail
still has the state seen during the audit, changes only the three existing AI classification
labels, and reads the result back. A final `digest_and_trash` decision moves the message to
Gmail Trash; a final `kept` or `action_needed` decision restores it if it was in Trash. Trash
is recoverable and no permanent-delete API is used. Confirmations and changes are stored as
encrypted content-level learning. Stale Gmail state aborts the whole preflight before any
label change, and each message is checked again immediately before mutation. Daily triage
and review apply share one Gmail-write lock. Apply is idempotent; only incomplete reviews
may be retried. A completed ledger prevents historical approval reuse. The redacted ledger
is stored on the state branch and as a 90-day workflow artifact.

Manual audit: GitHub Actions → `Gmail Weekly Quality Audit` → `Run workflow`.

## What it does

- Connects to Gmail using OAuth2 with refreshable tokens.
- Classifies messages as `kept`, `action_needed`, or `digest_and_trash`.
- Protects potentially important/sensitive emails (attachments, replies, finance/legal/work signals).
- Protects starred Gmail messages from summary trashing.
- Generates a one-line summary before any destructive action.
- Sends a daily `Today's GMAIL FOMO summary` email for reviewed/noisy messages, including each message's Gmail receipt date in Athens time.
- Supports **shadow mode** (no deletion) and **active mode** (trash enabled).
- Logs every decision/action to persistent JSONL + CSV audit files.
- Applies status labels in Gmail:
  - `AI/Kept`
  - `AI/Action-Needed`
  - `AI/Digest-and-Trash`
- Sends summarized `digest_and_trash` messages to Trash only after the digest email is sent.
- Marks summarized messages with `AI/FOMO-Summarized`.
- Restores false positives marked with `AI/Wrongly-Trashed`, explains them in the next daily summary, and learns content-level signals without protecting the sender universally.

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
├── apps_script/
│   └── weekly_review_relay/
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
- Keep `recent_messages_per_run: 20` so the newest inbox mail is always considered.
- Use `candidate_scan_limit` to scan deeper into the inbox backlog without reviewing more than 50 emails per run.
- Keep `use_model: true` to let Gemini scan email text and supported attachments.
- Keep `mode: shadow` until you have reviewed several audit runs.
- Keep `min_trash_confidence` high enough to block uncertain classifications. The active configuration uses `0.85`; starred and feedback-protected messages remain hard-blocked from Trash.

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

4. Inspect Gmail labels (`AI/Kept`, `AI/Action-Needed`, and `AI/Digest-and-Trash`).

## Daily GMAIL FOMO summary

The scheduled GitHub Actions workflow targets 09:17 `Europe/Athens` throughout the year,
with a 10:17 fallback. Explicit UTC cron candidates avoid dependence on timezone-aware
scheduler registration; an Athens-aware gate selects the two valid slots across daylight
saving changes. Before scheduled or recovery execution continues, the gate checks every
same-day workflow run. If any earlier `Run triage` step started, the later run skips triage
to avoid duplicate email or Gmail actions. If the earlier run never reached triage, the
fallback or independent recovery may run. GitHub Actions can still start a scheduled run
late, and the summary email is sent after setup, Gmail checks, and AI review complete, so the
delivery time can vary.

GMAIL FOMO gradually works through inbox backlog by selecting inbox messages that do not
already have one of its AI labels. It scans deeper than the daily review limit, always
keeps a slice of the newest inbox mail in the run, then fills the remaining quota with
older unreviewed messages. It still reviews at most 50 emails in a run.

When `daily_summary.enabled` is true:

- `digest_and_trash` emails are summarized with the selected OpenRouter model.
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
2. The next daily automation run immediately restores it to the inbox and independently reviews the content and supported attachments.
3. The correction reason, evidence, certainty, and reusable lesson are included in the normal `Today's GMAIL FOMO summary` email; no separate correction email is sent.
4. After that summary is successfully sent and the correction ID is safely committed to the encrypted GitHub state, the automation removes trash-related AI labels and `AI/Wrongly-Trashed`, leaving `AI/Kept`. If delivery or state verification fails, the restored email remains pending so the next daily run retries it.

`AI/Wrongly-Trashed` is a pending feedback control, not a fifth category. `AI/Kept` is the
only correction label left after successful review. Completed correction IDs are stored in
`.gmail-fomo/feedback-state.enc.json` on the dedicated `gmail-fomo-state` branch. The
repository is public, so the file contains only an authenticated Fernet ciphertext; the
dedicated `GMAIL_FOMO_STATE_KEY` Actions secret is never committed. Every update uses the
previous GitHub blob SHA, then downloads, decrypts, and verifies the remote state before any
pending correction label is removed. Future messages are matched to at most three prior
corrections using meaningful
signals such as warranty records, attachments, order references, reply context, financial
records, or deadlines. A sender/domain match alone is deliberately insufficient and never
creates blanket sender protection.

The one-time `Migrate Gmail Feedback State` workflow copies IDs from the former unsent Gmail
draft into the encrypted branch state. It deletes the legacy draft only after remote
read-back verification and never sends a summary or other email.

Product-specific warranty information and warranty documents are hard-kept even when the
surrounding email appears promotional. Warranty expiration is not evaluated. Promotions that
only offer a new or extended warranty remain eligible for normal promotional classification.

## Automation (GitHub Actions)

This repository includes `.github/workflows/gmail-triage.yml` to run triage automatically
at 09:17 `Europe/Athens`, with a duplicate-safe fallback at 10:17. A deduplicated
`daily_recovery` dispatch is reserved for an independent watchdog when GitHub does not create
either scheduled event.

You can also trigger it manually with **Run workflow** in GitHub Actions.
This is the production scheduler path (GitHub-hosted runners), not a Colab scheduler.

Required repository secrets:

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`
- `GMAIL_FOMO_STATE_KEY` (a dedicated Fernet key; never reuse a Google or OpenRouter secret)
- `OPENROUTER_API_KEY` for model-based sorting through OpenRouter.
- `WEEKLY_REVIEW_APPROVAL_SECRET` shared only with the private Apps Script relay.
- Model: `google/gemini-3.1-flash-lite`.
- Optional variable: `OPENROUTER_MAX_ATTACHMENT_BYTES` defaults to `750000`.
- Required variable: `WEEKLY_REVIEW_APP_URL`, the private Apps Script `/exec` URL.

Deploy the private confirmation relay once by following
`apps_script/weekly_review_relay/README.md`. Future weekly reviews do not require a Review PR
or the repository setting that allows Actions to create pull requests.

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

- If confidence is below the configured destructive-action threshold, the message is deferred.
- Only `digest_and_trash` messages at or above that threshold can be trashed in active mode.
- Starred Gmail messages are always protected and labeled `AI/Kept` instead of being trashed.
- User feedback keeps the corrected email and supplies content-level examples; it does not protect every future email from the sender.
