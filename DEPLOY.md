# Deploy Guide — Free Tier (Student-Friendly)

Deploy the Funding Outreach Automation app using **100% free** services. No credit card required for the options below.

---

## Architecture (Free Stack)

| Component   | Free service        | Notes                          |
|------------|---------------------|---------------------------------|
| Backend API| **Render** or **Railway** | Python/FastAPI; SQLite on disk |
| Frontend   | **Vercel** or **Netlify** | Static React build; proxies API |
| Database   | **SQLite** (included) or **Neon** (free Postgres) | Neon if you want persistent DB |
| Secrets    | Env vars on each platform | No extra cost                  |

---

## Step 1: Prepare the repo

1. **Commit everything** (so deploy services can clone):
   ```bash
   git add -A
   git commit -m "Prepare for deploy"
   git push origin main
   ```
2. **Optional:** Add a `.env.example` in the repo (no real keys). Your repo already has one; ensure it lists every variable the backend needs (e.g. `GROQ_API_KEY`, `SMTP_EMAIL`, `SMTP_PASSWORD`, etc.) so you can copy-paste into the host’s dashboard.

---

## Step 2: Deploy backend (Render — free)

1. Go to [render.com](https://render.com) and sign up (free, GitHub login works).
2. **New → Web Service**.
3. Connect your GitHub repo and select this project.
4. Configure:
   - **Name:** `funding-outreach-api` (or any name).
   - **Region:** Choose closest to you.
   - **Runtime:** **Python 3**.
   - **Build command:** (create venv and install into it so the start command finds uvicorn)
     ```bash
     python -m venv .venv && .venv/bin/pip install --upgrade pip && .venv/bin/pip install -r requirements.txt
     ```
   - **Start command:**
     ```bash
     .venv/bin/python -m uvicorn api_server:app --host 0.0.0.0 --port $PORT
     ```
   - **Instance type:** **Free**.
5. **Environment variables** (required for insights and email generation):
   - **Where to add them:** In the Render dashboard, open your service (e.g. outreach-automation). In the **left sidebar**, click **Environment**. Click **"Add Environment Variable"** (or "Add from .env" to paste from a file). Add each key below; mark secrets (API keys, passwords) as **Secret** so they’re hidden.
   - **Keys to add:**
   - `GROQ_API_KEY` — Get a key at [console.groq.com/keys](https://console.groq.com/keys). Without this, the app will not generate insights or emails and the dashboard will show "LLM not configured."
   - `SMTP_HOST`, `SMTP_PORT`, `SMTP_EMAIL`, `SMTP_PASSWORD`
   - `IMAP_HOST`, `IMAP_PORT`, `IMAP_EMAIL`, `IMAP_PASSWORD`
   - `CALENDAR_LINK`
   - `AUTO_APPROVE=0`
   - `DB_PATH=leads.db` (Render free tier has ephemeral disk; DB resets on redeploy. For persistence use Neon in Step 2b.)
6. Click **Create Web Service**. Wait for the first deploy.
7. Copy the service URL, e.g. `https://funding-outreach-api.onrender.com`. This is your **API URL**.

**Render free tier:** Service sleeps after ~15 min idle; first request after sleep can take 30–60 s. No credit card needed.

**If deploy fails with “uvicorn: command not found” (exit 127) or "No module named uvicorn":** Use the build/start in step 4 (create .venv in build; start with `.venv/bin/python -m uvicorn api_server:app --host 0.0.0.0 --port $PORT`).

---

**Troubleshoot "no approval / nothing generates":** Render → your service → **Logs**; click Run outreach and watch for `Generating insights` or errors (e.g. 429). After ~30 s, expand the lead row — it may show "Error generating insights: ...". In browser F12 → Network, check `POST /api/run` (should be 200) and `GET /api/leads/pending` (should get pending items when backend is waiting).

---

## Step 2b (Optional): Persistent database with Neon (free)

If you want the database to survive redeploys and restarts:

1. Go to [neon.tech](https://neon.tech) and sign up (free).
2. Create a project and copy the **connection string** (e.g. `postgresql://user:pass@host/dbname?sslmode=require`).
3. Install PostgreSQL driver in the project:
   - Add to `requirements.txt`: `psycopg2-binary` or `asyncpg` (if you add async Postgres support).
   - For minimal change, you can keep SQLite and skip this; only add if you implement Postgres in `database.py`.
4. In Render, add env var: `DATABASE_URL=<your Neon connection string>`.
5. In your app, if you add Postgres support: read `DATABASE_URL` and use it instead of SQLite when set.

If you keep SQLite only, skip this step; the app runs as-is with SQLite on Render (data resets on redeploy on free tier).

---

## Step 3: Deploy frontend (Vercel — free)

1. Go to [vercel.com](https://vercel.com) and sign up (GitHub login).
2. **Add New → Project** and import the same repo.
3. **Configure:**
   - **Root directory:** `frontend` (important).
   - **Framework preset:** Vite.
   - **Build command:** `npm run build` (default).
   - **Output directory:** `dist` (default).
4. **Environment variables:**
   - `VITE_API_URL` = your backend root URL from Step 2, e.g. `https://funding-outreach-api.onrender.com` (the app adds `/api` for routes)
   - (Your frontend will use this to call the API; see “Frontend API URL” below.)
5. Deploy. Vercel gives you a URL like `https://your-project.vercel.app`.

**Frontend API URL:** Set `VITE_API_URL` to your Render backend root (e.g. `https://xxx.onrender.com`). The app will request `{VITE_API_URL}/api/leads`, `{VITE_API_URL}/api/upload`, etc. For local dev, `/api` is proxied to localhost:8000.

---

## Step 4: CORS and security

- Backend (Render) already allows CORS from any origin (`allow_origins=["*"]`). For production you can restrict to your Vercel URL later.
- All secrets stay in backend env vars; the frontend never sees API keys. HTTPS is provided by Render and Vercel.

---

## Step 5: Run a quick test

1. Open your Vercel frontend URL.
2. Upload a small Excel file (Name, Email, Company).
3. Click “Run outreach” and approve insights/email in the UI.
4. Check that the lead status updates and (if configured) an email is sent.

If the backend was sleeping, the first request may take a minute; subsequent ones are fast.

---

## Alternative: Railway (backend) + Netlify (frontend)

- **Railway:** [railway.app](https://railway.app) — free tier with a monthly allowance. New Web Service from repo; build: `pip install -r requirements.txt`; start: `python -m uvicorn api_server:app --host 0.0.0.0 --port $PORT`. Add env vars same as above.
- **Netlify:** [netlify.com](https://netlify.com) — deploy frontend: set base directory to `frontend`, build command `npm run build`, publish directory `frontend/dist`. Add `VITE_API_URL` to env and use it in the app for API calls.

---

## Privacy & compliance

- See **PRIVACY.md** in the repo for data handling and your responsibilities (GDPR/CCPA, backups, secrets).
- In production, add a link to your privacy notice in the app footer if you collect personal data.

## Checklist

- [ ] Repo pushed; `.env` not committed; `.env.example` documents all vars.
- [ ] Backend deployed (Render or Railway); all env vars set; `/api/health` returns OK.
- [ ] Frontend deployed (Vercel or Netlify); `VITE_API_URL` set to backend URL.
- [ ] Test: upload → run → approve → verify status/email.

---

## Optional: Custom domain

- On Vercel: Project → Settings → Domains → add your domain (e.g. `outreach.yourdomain.com`). Free for one custom domain.
- On Render: Dashboard → Service → Settings → Custom Domain. You can point a subdomain to the API.

All steps above use free tiers only and are suitable for students and side projects.
