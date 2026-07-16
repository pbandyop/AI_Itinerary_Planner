# Deploy agent (LangGraph / FastAPI) to Railway

## Quick path (CLI)

From the **repo root**:

```bash
# Login (browser)
npx @railway/cli login

# Create project + link this directory
npx @railway/cli init

# Set secrets (required)
npx @railway/cli variables set LLM_PROVIDER=gemini
npx @railway/cli variables set GOOGLE_API_KEY=your_key_here
npx @railway/cli variables set RAG_EMBEDDINGS=bm25
npx @railway/cli variables set CORS_ORIGINS=http://localhost:3000,https://YOUR_FRONTEND.vercel.app
npx @railway/cli variables set AGENT_WORKFLOW_MODE=true
npx @railway/cli variables set ORCHESTRATOR_LLM=true
npx @railway/cli variables set ITINERARY_LLM=true
npx @railway/cli variables set SYNTHESIS_LLM=true
npx @railway/cli variables set REVIEWER_LLM=true
npx @railway/cli variables set ITINERARY_STRATEGY=legacy

# Deploy
npx @railway/cli up

# Public HTTPS URL
npx @railway/cli domain
```

Health check: `GET https://YOUR_SERVICE.up.railway.app/health`

## Dashboard path

1. [railway.app](https://railway.app) → New Project → Deploy from GitHub  
2. Select `AI_Itinerary_Planner` (or this repo)  
3. Root directory = repo root (uses `railway.json` → `services/agent/Dockerfile`)  
4. Add the variables above  
5. Generate a domain under Settings → Networking  

## Notes

- Image defaults to **`RAG_EMBEDDINGS=bm25`** (no HuggingFace model download; smaller memory). Tips still cite corpus chunks.  
- For stronger RAG later, set `RAG_EMBEDDINGS=huggingface` and use a larger Railway plan (BGE cold-start is heavy).  
- `PORT` is injected by Railway; the agent already honors it.  
- Point the web app at the public URL:  
  `NEXT_PUBLIC_AGENT_BASE_URL=https://YOUR_SERVICE.up.railway.app`  
  and include your frontend origin in `CORS_ORIGINS`.
