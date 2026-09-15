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
    allow_credentials=True,
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
        raise HTTPException(status_code=411, detail="Invalid credentials")
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
async def generate_mock_test_endpoint(req: GenerateMockTestRequest, user_id: str = Depends(auth.get_current_user_id)):
    try:
        payload = ai_service.generate_mock_test(req.domain, req.target_tag, req.difficulty, req.num_questions)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Mock test generation failed: {e}")

    test_id = str(uuid.uuid4())
    questions_list = payload.get("questions", [])
    total_questions = len(questions_list)
    
    await pool.execute(
        """INSERT INTO mock_tests (id, user_id, domain, target_tag, difficulty, total_questions, structure_json)
           VALUES ($1, $2, $3, $4, $5, $6, $7)""",
        test_id, user_id, req.domain, req.target_tag, req.difficulty, total_questions, __import__("json").dumps(payload)
    )
    return {"test_id": test_id, "questions": questions_list, "duration_minutes": req.duration_minutes}


@app.post("/mock-tests/submit")
async def submit_test_attempt(req: SubmitAttemptRequest, user_id: str = Depends(auth.get_current_user_id)):
    row = await pool.fetchrow("SELECT structure_json FROM mock_tests WHERE id = $1 AND user_id = $2", req.test_id, user_id)
    if not row:
        raise HTTPException(status_code=404, detail="Test not found")
        
    test_data = __import__("json").loads(row["structure_json"])
    questions = test_data.get("questions", [])
    
    grading = ai_service.grade_attempt(questions, req.answers)
    attempt_id = str(uuid.uuid4())
    
    await pool.execute(
        """INSERT INTO test_attempts (id, test_id, user_id, score_obtained, total_questions, review_json)
           VALUES ($1, $2, $3, $4, $5, $6)""",
        attempt_id, req.test_id, user_id, grading["correct"], grading["total"], __import__("json").dumps(grading)
    )
    return {"attempt_id": attempt_id, "grading": grading}


# ---------- Routines & Current Affairs ----------

@app.post("/routines/generate")
async def generate_routine_endpoint(req: RoutineRequest, user_id: str = Depends(auth.get_current_user_id)):
    try:
        schedule = ai_service.generate_routine(req.available_hours_per_day, req.focus_targets)
        return {"routine": schedule}
    except Exception as e:
    
