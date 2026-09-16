import os
import uuid
import tempfile
from typing import Optional

import asyncpg
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr

import auth
import storage
import ai_service
import current_affairs_service

app = FastAPI(title="AI Learning Platform")

# Allow the frontend (hosted on a different domain — Netlify/Cloudflare Pages)
# to call this API. Lock this down to your real frontend URL in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_DSN = os.getenv("DATABASE_URL", "postgresql://localhost/ai_learning")
pool: Optional[asyncpg.Pool] = None


@app.on_event("startup")
async def startup():
    global pool
    pool = await asyncpg.create_pool(DB_DSN)


@app.on_event("shutdown")
async def shutdown():
    await pool.close()


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------- Schemas ----------

class SignupRequest(BaseModel):
    username: str
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class GenerateMockTestRequest(BaseModel):
    domain: str          # EXAMS | SKILLS | LANGUAGES
    target_tag: str      # e.g. "UPSC Prelims GS", "RBI Grade B Quant", "Python Core"
    difficulty: str      # EASY | MEDIUM | HARD | EXPERT
    num_questions: int = 20
    duration_minutes: int = 30


class SubmitAttemptRequest(BaseModel):
    test_id: str
    answers: list[int]  # selected option index per question, in order


class ChatRequest(BaseModel):
    query: str


class RoutineRequest(BaseModel):
    available_hours_per_day: int
    focus_targets: list[str]


class CurrentAffairsDigestRequest(BaseModel):
    target_exam: str
    raw_text: str


# ---------- Auth ----------

@app.post("/auth/signup")
async def signup(req: SignupRequest):
    existing = await pool.fetchrow("SELECT id FROM users WHERE email = $1", req.email)
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    user_id = str(uuid.uuid4())
    await pool.execute(
        "INSERT INTO users (id, username, email, password_hash) VALUES ($1, $2, $3, $4)",
        user_id, req.username, req.email, auth.hash_password(req.password),
    )
    token = auth.create_access_token(user_id)
    return {"access_token": token, "token_type": "bearer", "user": {"id": user_id, "username": req.username}}


@app.post("/auth/login")
async def login(req: LoginRequest):
    row = await pool.fetchrow("SELECT id, username, password_hash FROM users WHERE email = $1", req.email)
    if not row or not auth.verify_password(req.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = auth.create_access_token(str(row["id"]))
    return {"access_token": token, "token_type": "bearer", "user": {"id": str(row["id"]), "username": row["username"]}}


@app.get("/auth/me")
async def me(user_id: str = Depends(auth.get_current_user_id)):
    row = await pool.fetchrow(
        "SELECT id, username, email, selected_exams, active_skills, active_languages, daily_study_hours "
        "FROM users WHERE id = $1", user_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return dict(row)


# ---------- Storage: single-shot upload (Supabase Storage, 50MB free-tier cap) ----------

@app.post("/storage/upload")
async def upload_file(
    file: UploadFile = File(...),
    extracted_text_sample: str = Form(""),
    user_id: str = Depends(auth.get_current_user_id),
):
    file_bytes = await file.read()
    if len(file_bytes) > storage.MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds {storage.MAX_FILE_SIZE // (1024*1024)}MB limit (free-tier Supabase Storage cap)",
        )

    kb_id = str(uuid.uuid4())
    try:
        result = storage.upload_file(user_id, file.filename, file.content_type, file_bytes)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upload failed: {e}")

    await pool.execute(
        """INSERT INTO knowledge_base (id, user_id, file_name, cloud_storage_key, file_size_bytes, status)
           VALUES ($1, $2, $3, $4, $5, 'PROCESSING')""",
        kb_id, user_id, file.filename, result["file_key"], len(file_bytes),
    )

    # Auto-tag from the first-page sample the client already extracted client-side.
    sample = extracted_text_sample or file.filename
    try:
        tags = ai_service.ai_auto_arrange(sample)
    except Exception as e:
        await pool.execute(
            "UPDATE knowledge_base SET status = 'FAILED', failure_reason = $2 WHERE id = $1",
            kb_id, f"auto-tag failed: {e}",
        )
        raise HTTPException(status_code=502, detail="AI tagging failed, file kept but unfiled")

    await pool.execute(
        """UPDATE knowledge_base
           SET domain = $2, target_subject = $3, content_category = $4, ai_tags = $5,
               status = 'READY', processed_at = now()
           WHERE id = $1""",
        kb_id, tags["domain"], tags["target_subject"], tags["category"],
        __import__("json").dumps(tags),
    )

    # Best-effort full-text indexing for RAG chat. Only attempted for PDFs, and
    # never blocks the READY status above if it fails — chat just won't find this doc.
    if file.filename.lower().endswith(".pdf"):
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(file_bytes)
                local_path = tmp.name
            full_text = ai_service.extract_text_from_pdf(local_path)
            ai_service.vectorize_and_index(
                full_text, result["file_key"], user_id,
                {"knowledge_base_id": kb_id, "target_subject": tags["target_subject"]},
            )
        except Exception:
            pass  # tagging already succeeded; indexing is a bonus, not required

    return {"knowledge_base_id": kb_id, "status": "READY", "tags": tags}


@app.get("/knowledge-base")
async def list_knowledge_base(domain: Optional[str] = None, user_id: str = Depends(auth.get_current_user_id)):
    if domain:
        rows = await pool.fetch(
            "SELECT * FROM knowledge_base WHERE user_id = $1 AND domain = $2 ORDER BY uploaded_at DESC",
            user_id, domain,
        )
    else:
        rows = await pool.fetch(
            "SELECT * FROM knowledge_base WHERE user_id = $1 ORDER BY uploaded_at DESC", user_id
        )
    return [dict(r) for r in rows]


@app.delete("/knowledge-base/{kb_id}")
async def delete_knowledge_base(kb_id: str, user_id: str = Depends(auth.get_current_user_id)):
    row = await pool.fetchrow(
        "SELECT cloud_storage_key FROM knowledge_base WHERE id = $1 AND user_id = $2", kb_id, user_id
    )
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        storage.delete_file(row["cloud_storage_key"])
    except Exception:
        pass  # DB row is the source of truth for the app; storage cleanup best-effort
    await pool.execute("DELETE FROM knowledge_base WHERE id = $1", kb_id)
    return {"deleted": True}


# ---------- AI chat (RAG over the user's own uploaded materials) ----------

@app.post("/ai/chat")
async def ai_chat(req: ChatRequest, user_id: str = Depends(auth.get_current_user_id)):
    try:
        return ai_service.rag_chat(user_id, req.query)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI chat failed: {e}")


# ---------- Mock tests ----------

@app.post("/mock-tests/generate")
async def generate_mock_test(req: GenerateMockTestRequest, user_id: str = Depends(auth.get_current_user_id)):
    try:
        payload = ai_service.generate_mock_test(req.domain, req.target_tag, req.difficulty, req.num_questions)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Mock test generation failed: {e}")

    test_id = str(uuid.uuid4())
    total_marks = len(payload["questions"])
    await pool.execute(
        """INSERT INTO mock_test_series
           (id, title, associated_domain, target_tag, difficulty, duration_minutes, total_marks, question_payload)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
        test_id, f"{req.target_tag} — {req.difficulty.title()}", req.domain, req.target_tag,
        req.difficulty, req.duration_minutes, total_marks, __import__("json").dumps(payload),
    )
    # Strip correct_index/explanation before returning so the client can't cheat by reading the payload.
    questions_for_client = [
        {"question": q["question"], "options": q["options"]} for q in payload["questions"]
    ]
    return {
        "test_id": test_id, "title": f"{req.target_tag} — {req.difficulty.title()}",
        "duration_minutes": req.duration_minutes, "questions": questions_for_client,
    }


@app.get("/mock-tests")
async def list_mock_tests(domain: Optional[str] = None):
    if domain:
        rows = await pool.fetch(
            "SELECT id, title, associated_domain, target_tag, difficulty, duration_minutes, total_marks, created_at "
            "FROM mock_test_series WHERE associated_domain = $1 ORDER BY created_at DESC", domain,
        )
    else:
        rows = await pool.fetch(
            "SELECT id, title, associated_domain, target_tag, difficulty, duration_minutes, total_marks, created_at "
            "FROM mock_test_series ORDER BY created_at DESC LIMIT 50"
        )
    return [dict(r) for r in rows]


@app.post("/mock-tests/submit")
async def submit_attempt(req: SubmitAttemptRequest, user_id: str = Depends(auth.get_current_user_id)):
    row = await pool.fetchrow("SELECT question_payload FROM mock_test_series WHERE id = $1", req.test_id)
    if not row:
        raise HTTPException(status_code=404, detail="Test not found")
    import json
    payload = row["question_payload"]
    payload = json.loads(payload) if isinstance(payload, str) else payload
    result = ai_service.grade_attempt(payload["questions"], req.answers)

    attempt_id = str(uuid.uuid4())
    await pool.execute(
        """INSERT INTO test_attempts (id, user_id, test_id, score_obtained, accuracy_percentage, ai_performance_feedback)
           VALUES ($1, $2, $3, $4, $5, $6)""",
        attempt_id, user_id, req.test_id, result["correct"], result["accuracy_percentage"],
        f"{result['correct']}/{result['total']} correct.",
    )
    return {"attempt_id": attempt_id, **result}


@app.get("/test-attempts")
async def my_attempts(user_id: str = Depends(auth.get_current_user_id)):
    rows = await pool.fetch(
        """SELECT ta.id, ta.score_obtained, ta.accuracy_percentage, ta.attempted_at,
                  mt.title, mt.target_tag, mt.difficulty
           FROM test_attempts ta JOIN mock_test_series mt ON mt.id = ta.test_id
           WHERE ta.user_id = $1 ORDER BY ta.attempted_at DESC""",
        user_id,
    )
    return [dict(r) for r in rows]


# ---------- Study routine ----------

@app.post("/routine/generate")
async def generate_routine_route(req: RoutineRequest, user_id: str = Depends(auth.get_current_user_id)):
    try:
        routine = ai_service.generate_routine(req.available_hours_per_day, req.focus_targets)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Routine generation failed: {e}")

    await pool.execute("DELETE FROM study_routines WHERE user_id = $1", user_id)
    for block in routine:
        await pool.execute(
            """INSERT INTO study_routines (id, user_id, day_of_week, start_time, end_time, focus_domain, topic_headline)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            str(uuid.uuid4()), user_id, block["day"], block["start_time"], block["end_time"],
            block["domain_type"], block["topic"],
        )
    await pool.execute(
        "UPDATE users SET daily_study_hours = $2 WHERE id = $1", user_id, req.available_hours_per_day
    )
    return {"routine": routine}


@app.get("/routine")
async def get_routine(user_id: str = Depends(auth.get_current_user_id)):
    rows = await pool.fetch(
        "SELECT * FROM study_routines WHERE user_id = $1 ORDER BY day_of_week, start_time", user_id
    )
    return [dict(r) for r in rows]


@app.patch("/routine/{block_id}/complete")
async def complete_block(block_id: str, user_id: str = Depends(auth.get_current_user_id)):
    row = await pool.fetchrow(
        "SELECT 1 FROM study_routines WHERE id = $1 AND user_id = $2", block_id, user_id
    )
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    await pool.execute("UPDATE study_routines SET completed = TRUE WHERE id = $1", block_id)
    return {"completed": True}


# ---------- Current affairs ----------

@app.post("/current-affairs/digest")
async def add_current_affairs_digest(req: CurrentAffairsDigestRequest):
    try:
        return await current_affairs_service.create_digest_entry(pool, req.target_exam, req.raw_text)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Digest summarization failed: {e}")


@app.get("/current-affairs")
async def get_current_affairs(target_exam: str):
    return await current_affairs_service.list_for_exam(pool, target_exam)
