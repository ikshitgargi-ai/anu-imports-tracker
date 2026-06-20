# Anu Imports Tracker — Go-Live Runbook

Plain-English steps to put the standalone **Anu Imports** app online and start the
reps on it. You are standing up **three things**, all separate from the NB tracker:

| Piece | Host | Free? |
|---|---|---|
| Database (Postgres) | Neon | Free tier fine |
| Backend (Flask API) | Render | Free works; $7/mo = no cold starts |
| Frontend (the phone app) | Vercel | Free |

**The one rule that keeps it separate from NB:** the database must be a **brand-new
Neon project**. Never paste the NB tracker's database URL into this app.

---

## Step 1 — Database (Neon) · ~3 min
1. Go to **neon.tech** → sign in → **New Project**, name it `anu-imports`.
2. Region: **US East (N. Virginia)** (same as NB, lowest latency to Render).
3. Copy the **pooled** connection string it shows (looks like
   `postgresql://...-pooler...neon.tech/neondb?sslmode=require`). Keep it handy.

## Step 2 — Push the code to GitHub · ~5 min
The two folders to publish (each becomes its own repo):
- `anu-imports-tracker/`  → backend
- `anu-imports-web/`      → frontend

From a terminal, for each folder: create the repo with the GitHub CLI and push
(`gh repo create ikshitgargi-ai/anu-imports-tracker --public --source=. --push`).
Tell me if you want me to script this exactly — it's two commands per folder.

## Step 3 — Backend (Render) · ~8 min
1. **render.com** → **New** → **Blueprint** → pick the `anu-imports-tracker` repo.
   Render reads `render.yaml` and proposes the service + the daily cron.
2. It will ask for the environment values. Fill them from **`.env.example`** in this
   folder. The shortcuts:
   - `DATABASE_URL` = the Neon string from Step 1.
   - `SOD_USER` / `SOD_PASSWORD` / `SOD_AGENT_ID` = **open the existing lcbo-tracker
     service on Render → Environment → "Reveal" → copy the identical values.** (Same
     LCBO data account, agent #1113.)
   - `SOD_CRON_TOKEN`, `ADMIN_TOKEN` = run `openssl rand -hex 32` once each.
   - `RESEND_API_KEY` = from resend.com (enables the shelf-photo emails to you).
3. Click **Apply**. First build ~3–5 min. When it's live, open
   `https://anu-imports-tracker.onrender.com/healthz` — you should see `"status":"healthy"`.
4. **Cost note:** the blueprint sets the web service to **free** (cold-starts ~50s
   after idle) and the daily cron to **starter** ($7/mo, because Render cron needs a
   paid tier). If you want zero cost, delete the cron block — the app's built-in
   scheduler still runs the nightly SOD sync. If you want no cold-starts, bump the
   web service to **starter** ($7/mo) like the NB tracker.

## Step 4 — Frontend (Vercel) · ~4 min
1. **vercel.com** → **Add New Project** → import the `anu-imports-web` repo.
2. One environment variable: `NEXT_PUBLIC_API_BASE = https://anu-imports-tracker.onrender.com`.
3. Deploy. You get a URL like `anu-imports-web.vercel.app` — **that's the app the reps open.**
   (I can drive this step for you via the connected Vercel tools once the backend URL exists.)

## Step 5 — First run · ~2 min
1. Open the app → menu → **RPR Tasting Blitz** → tap **"Load the 148-store list"**
   (this calls `/api/rpr/ingest`; for the button to be authorized, set
   `NEXT_PUBLIC_ADMIN_TOKEN` on Vercel to the same value as the backend's
   `ADMIN_TOKEN`). Or skip the button and run the one curl below.
2. The nightly SOD sync (02:05 ET) fills in live LCBO stock for all 9 SKUs. To see
   data immediately instead of waiting, run the optional history migration in
   `.env.example` (copies past SOD data for the 9 Anu SKUs from the NB database, read-only).

One-tap ingest from a terminal if you prefer:
```
curl -X POST https://anu-imports-tracker.onrender.com/api/rpr/ingest \
  -H "X-Admin-Token: <your ADMIN_TOKEN>"
```

---

## How the reps start using it
- Open the Vercel URL on the iPhone → **Share → Add to Home Screen** (installs it as
  an app icon, separate from the NB tracker's icon).
- On `/today` (or the Schedule strip) **pick your name** (Ikshit / Vaneet / Ed / Namit).
- **RPR Tasting Blitz**: work a run in order, tap a store, log the tasting, flip
  *display secured*, shoot the shelf photo (it emails you). Tick stores off as you go.
- **HORECA Near Me**: tap *use my location*, pick 20/50/100 km, get accounts to walk into.
- **Reports**: weekly/monthly with a compare view; every report you open is saved.

## Daily / weekly checks (you, the operator)
- `…/healthz` should say healthy; `…/api/admin/daily-health-check` runs the full
  self-test (SOD freshness, sync success, data integrity) and the app emails you if
  something's stale.
- Reports → compare two weeks to watch tastings, displays secured, and new HORECA grow.

## Safety guarantees baked in
- Postgres only — nothing is lost on a redeploy.
- Separate database, backend URL, and login from the NB tracker — they cannot mix.
- The history migration reads the NB database **read-only** and refuses to run if you
  accidentally point source and target at the same database.
