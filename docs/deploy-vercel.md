# Deploy web (Next.js) to Vercel

The companion UI lives in `apps/web`. Browser calls go to same-origin `/api/agent/*`, which Next.js rewrites to the Railway agent (`AGENT_BASE_URL` / `NEXT_PUBLIC_AGENT_BASE_URL`).

## Quick path (CLI)

From **`apps/web`**:

```bash
npx vercel login

# Link / create project (Root Directory = apps/web if linking from repo root)
npx vercel link

# Production env (set before first production deploy)
npx vercel env add AGENT_BASE_URL production
# paste: https://agent-production-1675.up.railway.app

npx vercel env add NEXT_PUBLIC_AGENT_BASE_URL production
# paste the same Railway URL

# Optional: n8n PDF + email webhook (server-only)
npx vercel env add N8N_WEBHOOK_URL production

# Deploy
npx vercel --prod
```

Or from the **repo root** with an explicit path:

```bash
npx vercel --prod --cwd apps/web
```

## Dashboard path

1. [vercel.com/new](https://vercel.com/new) → Import `AI_Itinerary_Planner`
2. **Root Directory** → `apps/web`
3. Environment variables (Production + Preview):

| Name | Value |
|------|--------|
| `AGENT_BASE_URL` | `https://agent-production-1675.up.railway.app` |
| `NEXT_PUBLIC_AGENT_BASE_URL` | same as above |
| `N8N_WEBHOOK_URL` | optional n8n production webhook |

4. Deploy

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
