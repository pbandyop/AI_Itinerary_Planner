# Phase 8 — n8n PDF + email

The companion UI posts the current itinerary + user email to a **server-side**
Next.js route, which forwards JSON to your n8n webhook. n8n owns PDF generation
and email delivery.

## App wiring

1. Set `N8N_WEBHOOK_URL` in `apps/web/.env.local` (already documented in `.env.example`).
2. Restart `next dev` after changing the env var.
3. Generate a plan in the UI → **Email this plan** → enter email → **Send PDF**.

Proxy route: `POST /api/email-itinerary`

```json
{
  "email": "you@example.com",
  "city": "Jaipur",
  "summary": "…",
  "itinerary": { "trip": {}, "days": [] },
  "sources": [],
  "sent_at": "2026-07-16T00:00:00.000Z"
}
```

### Curl smoke test

With the Next.js app running:

```bash
curl -X POST http://localhost:3000/api/email-itinerary \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"you@example.com\",\"itinerary\":$(cat evals/fixtures/jaipur_2day_culture.json),\"sources\":[]}"
```

Or call n8n directly (workflow must be listening / Active):

```bash
curl -X POST "$N8N_WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"you@example.com\",\"city\":\"Jaipur\",\"itinerary\":$(cat evals/fixtures/jaipur_2day_culture.json),\"sources\":[],\"sent_at\":\"2026-07-16T00:00:00Z\"}"
```

## n8n workflow (build in cloud UI)

Suggested nodes:

1. **Webhook** — POST; path matching your URL id  
2. **IF** — `{{ $json.email }}` is not empty  
3. **Code** — build HTML from `itinerary.days` (morning / afternoon / evening)  
4. **HTML → PDF** (or community PDF node)  
5. **Gmail / SMTP** — To: `{{ $json.email }}`, attach PDF  
6. **Respond to Webhook** — `{ "ok": true, "message": "Email sent" }`

### Test vs production URL

| URL style | When it works |
|-----------|----------------|
| `/webhook-test/<id>` | Only while the workflow editor is in **Listen for test event** |
| `/webhook/<id>` | When the workflow is **Active** (use this for demos) |

### If the UI shows HTTP 500 / “Error in workflow”

The app reached n8n; a **later node crashed**. In n8n Cloud: **Executions** → open the red run → fix the highlighted node.

Common causes:
- Code/IF reads `$json.email` but payload is under `$json.body.*` (use `$json.body.itinerary` or add a Set node to unwrap `body`)
- HTML→PDF node missing / not configured
- Gmail/SMTP credentials not connected
- Webhook **Response Mode** = “When Last Node Finishes” but a middle node errors before Respond

Export your finished workflow from n8n and save it under `n8n/itinerary-pdf-email.json` when ready.

## PDF HTML (must match UI)

The Next.js route **`POST /api/email-itinerary`** now sends a ready-made **`html`**
field (clocks, hour wording, travel hints, relax notes, themes from placed stops).

In n8n:

1. **Replace** any old Code that builds lines like `Food · 60 min · then 14 min car`
2. Paste **`n8n/build_itinerary_html.js`** (uses `$json.html` from the app when present)
3. HTML→PDF / Convert to File should use **`{{ $json.html }}`**
4. Restart `next dev` after pulling so the app sends `html`

If your Code node still rebuilds the old minute format, it will overwrite the good HTML.