# Deploy web (Next.js) to Vercel

The companion UI lives in `apps/web`. Browser calls go to same-origin `/api/agent/*`, which Next.js rewrites to the Railway agent (`AGENT_BASE_URL` / `NEXT_PUBLIC_AGENT_BASE_URL`).

## Project settings (important)

Vercel **Root Directory** must be **`apps/web`** (monorepo). That is required for GitHub / dashboard deploys from the repo root — otherwise `npm install` fails looking for `/package.json`.

| Deploy method | Where to run | Root Directory |
|---------------|--------------|----------------|
| GitHub / dashboard Redeploy | repo root (automatic) | `apps/web` |
| CLI | **repo root** (`npx vercel --prod`) | `apps/web` |

Do **not** run `vercel --prod` from inside `apps/web` while Root Directory is `apps/web` — Vercel will look for `apps/web/apps/web` and fail.

Agent-only commits are skipped when `apps/web` is unchanged (`ignoreCommand`).

## Quick path (CLI)

From the **repo root**:

```bash
npx vercel login
npx vercel link   # itinerary-planner-web; confirm Root Directory = apps/web

# Production env (once)
npx vercel env add AGENT_BASE_URL production
# paste: https://agent-production-1675.up.railway.app

npx vercel env add NEXT_PUBLIC_AGENT_BASE_URL production
# paste the same Railway URL

# Optional: n8n PDF + email webhook (server-only)
npx vercel env add N8N_WEBHOOK_URL production

# Deploy
npx vercel --prod
```

## Dashboard path (first-time import)

1. [vercel.com/new](https://vercel.com/new) → Import `AI_Itinerary_Planner`
2. **Root Directory** → **`apps/web`**
3. Environment variables (Production + Preview):

| Name | Value |
|------|--------|
| `AGENT_BASE_URL` | `https://agent-production-1675.up.railway.app` |
| `NEXT_PUBLIC_AGENT_BASE_URL` | same as above |
| `N8N_WEBHOOK_URL` | optional n8n production webhook |

4. Deploy (or use CLI from repo root as above)

**Live (this project):** https://itinerary-planner-web-seven.vercel.app  
**Agent:** https://agent-production-1675.up.railway.app

## After deploy

1. Copy the Vercel URL (e.g. `https://….vercel.app`).
2. Update Railway `CORS_ORIGINS` to include it (and keep localhost for local UI):

   ```bash
   npx @railway/cli variable set CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000,https://YOUR_APP.vercel.app --service agent
   ```

   Same-origin `/api/agent` rewrites avoid most CORS issues; CORS still matters for any direct browser → agent calls.

3. Smoke-test: open the site, start a short voice/text plan for Jaipur.

## Notes

- Long itinerary `/invoke` calls often exceed **Vercel’s 120s external-rewrite timeout**.  
  The web client therefore calls `NEXT_PUBLIC_AGENT_BASE_URL` (Railway) **directly from the browser** when that URL is a public `https://` host.  
  Keep Railway `CORS_ORIGINS` updated with your Vercel domain.
- Railway closes HTTP with **no bytes for ~5 minutes**. The client uses `Accept: application/x-ndjson` so `/invoke` streams keepalive pings during long Overpass/LLM/revise runs.
- `next.config.ts` still rewrites `/api/agent` for local/dev; change the Railway URL → redeploy the web app so `NEXT_PUBLIC_*` is baked in.
- Do not commit `.env.local` or API keys.
- Do not commit `.vercel/` (local link only).
