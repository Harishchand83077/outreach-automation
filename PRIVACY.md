# Privacy & Data Handling

This document describes how the Funding Outreach Automation app handles data when you run it (self-hosted or on a free-tier deploy).

## What we store

- **Leads:** Name, email, company, and workflow outputs (insights, email drafts, status, replies) are stored in the database (SQLite by default) on the machine where the backend runs.
- **No analytics:** The app does not send usage data to any third party. LLM calls go to the provider you configure (e.g. Groq); email is sent via your SMTP/IMAP credentials.

## Where data lives

- All lead and run data stays in **your** backend and database. If you deploy to Render/Railway/Vercel, data is stored on that provider’s infrastructure under their terms.
- Export: Use **Export CSV** in the dashboard to download your leads. Back up this data if you need to move or delete the server.

## Your responsibilities

- **GDPR / CCPA:** If you process personal data (e.g. EU or California residents), you are responsible for having a lawful basis, providing privacy notices, and honoring deletion requests. You can delete leads from the database or clear the DB to remove data.
- **Secrets:** Keep `.env` and API keys (Groq, SMTP, IMAP) secret. Never commit them to version control. Use your host’s environment variables for production.

## Contact

This is an open-source/student project. For data or security concerns, open an issue in the project repository or contact the maintainer.
