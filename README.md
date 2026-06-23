# Option Flow Dashboard — Vercel deployment

This is the same dashboard you've been running locally, restructured
for Vercel's serverless Python runtime so it can run always-on
without keeping a terminal open.

## What changed from the local version

- `server.py` moved to `api/index.py` — Vercel's Python runtime
  looks for a Flask instance named `app` at specific entrypoint
  paths, and `api/index.py` is one of them.
- `upstox_client.py` is copied inside `api/` alongside `index.py`,
  since Vercel bundles each function with the files in its own
  directory.
- `vercel.json` added — tells Vercel to route all requests to the
  one Python function, and sets `maxDuration: 60` so the function
  can use the full 60-second budget Fluid Compute provides on the
  free Hobby tier (the default 10s serverless limit would likely be
  too tight for this app's concurrent VWAP fetches).
- Flask's `template_folder` is set explicitly to the project-root
  `templates/` directory, since by default Flask would look for a
  `templates/` folder next to `api/index.py` (i.e. `api/templates/`,
  which doesn't exist) rather than at the project root.

**No logic changed.** The app was already fully stateless — your
Upstox access token travels with each request from the browser, it
never lived in server-side memory — so it already matched the
serverless model with zero functional changes needed.

## Why this needed real porting, not just an upload

Vercel runs serverless functions, not a persistent process. Your
local `python server.py` keeps one process running indefinitely;
Vercel instead spins up a fresh function instance per request (or
reuses a warm one briefly). The concurrency this app already uses
(a thread pool fetching VWAP candles in parallel) still works fine
*within* a single function invocation — it just doesn't carry state
*between* invocations, which this app never relied on anyway.

## Deploy steps

### 1. Get the code onto GitHub

Vercel deploys from a Git repository. If you don't already have one:

```
cd optionflow_vercel
git init
git add .
git commit -m "Option flow dashboard"
```

Create a new repository on GitHub (via github.com, empty, no
README), then:

```
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git branch -M main
git push -u origin main
```

### 2. Import the project on Vercel

1. Go to **vercel.com**, sign in (GitHub login works directly)
2. Click **Add New** → **Project**
3. Find and import the repository you just pushed
4. Vercel should auto-detect the Python runtime from
   `requirements.txt` — leave the framework preset as-is
5. Click **Deploy**

That's it — no environment variables needed, since your Upstox token
is entered in the browser each session, not stored server-side.

### 3. Open your deployed dashboard

Vercel gives you a URL like `https://your-project.vercel.app` once
the build finishes (usually under a minute). Open it, paste your
Upstox access token, pick an index, click Calculate — same as local.

## Using it day to day

Since the token isn't stored anywhere, you'll paste a fresh one each
day exactly like you do locally — that part of the workflow doesn't
change. What you get from this deployment is: no terminal to keep
open, accessible from your phone or any browser, and it stays up
without your laptop running.

## If Calculate times out

The 60-second budget (via Fluid Compute, automatically enabled on
new Vercel projects since 2025) should comfortably cover the
option-chain + change-OI + FII/DII + concurrent VWAP-candle fetches
this app does per click. If you do hit a timeout:

- Lower "Levels per side" — fewer strikes means fewer VWAP candle
  fetches, since the pre-filter scales with that number.
- Check Vercel's function logs (Project → Deployments → click a
  deployment → Functions tab) for the actual error; a timeout shows
  up distinctly from an Upstox API error.
- If you're on a plan without Fluid Compute enabled, enable it in
  Project Settings → Functions, or confirm `maxDuration: 60` in
  `vercel.json` is actually being respected (older accounts may
  default to the stricter classic serverless limits).

## Redeploying after changes

Push to your GitHub repo's main branch — Vercel automatically
rebuilds and redeploys. No manual redeploy step needed.

## Files

- `api/index.py` — Flask app (Vercel's entrypoint)
- `api/upstox_client.py` — Upstox API client, computation logic
- `templates/index.html` — the dashboard page
- `vercel.json` — routing + function duration config
- `requirements.txt` — Python dependencies
