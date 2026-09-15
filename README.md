# Deploying The Prep Register — free stack (no card required)

## 1. Database (Supabase — already done if you followed along)
Postgres via Supabase. Schema already applied. Your `DATABASE_URL` uses the
**Session pooler** connection string (IPv4-compatible, works with Render).

## 2. File storage (Supabase Storage — same project, no separate signup)
1. In your Supabase project, left sidebar → **Storage** → **New bucket**.
2. Name it `knowledge-base` (must match `STORAGE_BUCKET_NAME` below). Leave it **private**
   (not public) — the backend accesses it with the service role key, never the browser directly.
3. Get your service role key: **Project Settings → API** → copy the **`service_role`** secret key
   (NOT the `anon` key — service_role bypasses row-level security, which is why it must only ever
   live on the backend, never in frontend code).
4. Your `SUPABASE_URL` is the project URL shown on that same API settings page
   (e.g. `https://gojpyjeappumajawhrlt.supabase.co`).
5. **Limit**: free-tier Supabase Storage caps individual files at 50MB, and 1GB total. The backend
   and frontend are both set to enforce/reflect this 50MB cap.

## 3. AI (Google AI Studio, free tier)
Get a Gemini API key at https://aistudio.google.com/apikey — powers auto-tagging, mock test
generation, RAG chat, and current-affairs summarization.

## 4. Backend hosting (Render.com, free tier)
1. Push the `backend/` folder to a GitHub repo.
2. Render → New → Web Service → connect the repo.
3. Build command: `pip install -r requirements.txt`
   Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Environment variables:
   - `DATABASE_URL` — your Supabase Session pooler connection string
   - `GEMINI_API_KEY` — from step 3
   - `SUPABASE_URL` — from step 2
   - `SUPABASE_SERVICE_ROLE_KEY` — from step 2
   - `STORAGE_BUCKET_NAME` — `knowledge-base`
   - `JWT_SECRET_KEY` — any long random string
   - `CORS_ORIGINS` — your frontend URL (e.g. `https://your-frontend.pages.dev`)
5. Deploy. Free tier sleeps after ~15 min idle, ~30s to wake on the next request.

## 5. Frontend hosting (Cloudflare Pages or Netlify, free)
1. Rename `app.html` to `index.html`, drag-and-drop into Netlify Drop (or connect the repo to
   Cloudflare Pages). No build step.
2. Open the deployed site → **Account tab** → paste your Render backend URL → Save.
3. Sign up, then use the **AI Assistant tab** to upload a PDF (≤50MB) and chat, and
   **Mock Tests → Generate with AI**.

## Notes / limitations of this MVP
- 50MB per file, 1GB total storage — free-tier Supabase Storage limits. If you outgrow this later,
  swapping in Cloudflare R2 (10GB free, needs a card on file for verification) or AWS S3 gets you
  back to large-file support; `storage.py` is the only file that would need to change.
- The vector index (`chroma_db`) lives on Render's local disk, wiped on redeploy/restart — fine
  for testing. For persistence, move embeddings into Postgres via the `pgvector` extension
  (Supabase supports it natively).
- Everything except AI features (syllabus, schedule, notes, NCERT checklist, etc.) works with no
  backend configured at all — stays local to the device via `localStorage`.
