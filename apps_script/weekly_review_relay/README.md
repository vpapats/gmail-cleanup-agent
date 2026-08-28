# Weekly review confirmation relay

This Google Apps Script web app is the authenticated bridge between the private HTML
attachment and the repository's `Apply Approved Weekly Gmail Review` workflow. It never
stores Gmail credentials and does not read or change Gmail itself. It validates the signed
review form, dispatches the exact three-label selection map, and polls the redacted apply
ledger until Gmail read-back is complete.

## One-time deployment

1. Create a standalone Google Apps Script project owned by the Gmail account being reviewed.
2. Copy `Code.gs`, `Status.html`, and `appsscript.json` from this directory into the project.
3. Add these **Script properties** (never commit their values):
   - `GITHUB_TOKEN`: fine-grained token for this repository with Actions read/write and Contents read.
   - `GITHUB_REPOSITORY`: `vpapats/gmail-cleanup-agent`.
   - `GITHUB_REF`: `main`.
   - `STATE_BRANCH`: `gmail-fomo-state`.
   - `APPROVAL_SECRET`: the same random secret as the GitHub Actions secret below.
   - `ALLOWED_USER_EMAIL`: the Gmail address that owns the review (defence in depth).
4. Deploy as a Web app, **execute as me**, with access restricted to **only myself**.
5. In GitHub repository settings add:
   - Actions secret `WEEKLY_REVIEW_APPROVAL_SECRET` with the same value as `APPROVAL_SECRET`.
   - Actions variable `WEEKLY_REVIEW_APP_URL` with the deployed `/exec` URL.
6. Merge the implementation to `main`, then run `Gmail Weekly Quality Audit` once manually.

The review attachment contains a signed review ID and opaque item IDs. A modified or partial
form is rejected. GitHub performs stale-state checks immediately before every Gmail mutation,
reads back labels and Trash state, writes the learning record, and publishes an idempotent
ledger. A completed ledger cannot be replayed.
