import os
import json
import fitz  # PyMuPDF
from google import genai
from google.genai import types
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma
from langchain_google_genai import GoogleGenerativeAIEmbeddings

embeddings = GoogleGenerativeAIEmbeddings(model="models/text-embedding-004")
vector_db = Chroma(persist_directory="./chroma_db", embedding_function=embeddings)

AI_CLIENT = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODEL = "gemini-2.5-flash"

ARRANGER_SYSTEM_INSTRUCTION = """
You are the Master AI Coordinator for a competitive-exam and skills learning platform.
Categorize each upload strictly into one of:
1. EXAMS (UPSC, State PSC, Bank, SSC CHSL, RRB NTPC, RBI Grade A/B, and similar)
2. SKILLS (Python, Web Dev, Data Science, etc.)
3. LANGUAGES (English, Spanish, Hindi, German, etc.)
Further classify into: 'SYLLABUS', 'NOTES', 'NCERT', 'MOCK_TEST', 'CURRENT_AFFAIRS', 'EXAM_PATTERN'.
Respond ONLY with valid JSON, no markdown fences, matching:
{"domain": "EXAMS|SKILLS|LANGUAGES", "target_subject": str, "category": str,
 "recommended_routine_slot": str, "tags": [str]}
"""


def extract_text_from_pdf(local_path: str, max_pages: int = 3000) -> str:
    """Local extraction only — file must already be downloaded from storage."""
    doc = fitz.open(local_path)
    pages = []
    for i in range(min(max_pages, len(doc))):
        pages.append(doc[i].get_text())
    doc.close()
    return "\n".join(pages)


def _parse_json_response(text: str) -> dict:
    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(cleaned)


def ai_auto_arrange(extracted_text_sample: str) -> dict:
    """Classifies a document into domain/subject/category/tags."""
    prompt = f"Analyze this document excerpt:\n---\n{extracted_text_sample[:4000]}\n---"
    response = AI_CLIENT.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=ARRANGER_SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
        ),
    )
    return _parse_json_response(response.text)


def vectorize_and_index(full_text: str, file_key: str, user_id: str, metadata: dict) -> int:
    """Chunks full document text and commits it to the RAG index, scoped to the owning user."""
    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=150)
    chunks = splitter.split_text(full_text)
    if not chunks:
        return 0
    vector_db.add_texts(
        texts=chunks,
        metadatas=[{"source": file_key, "user_id": user_id, **metadata} for _ in chunks],
    )
    return len(chunks)


def rag_chat(user_id: str, query: str, k: int = 5) -> dict:
    """RAG search restricted to the calling user's own materials only."""
    matched = vector_db.similarity_search(query, k=k, filter={"user_id": user_id})
    context = "\n\n".join(node.page_content for node in matched)

    prompt = f"""
You are an expert tutor for competitive exams (UPSC, State PSC, Bank, SSC, RRB, RBI Grade A/B),
technical skills, and languages. Answer the student's question using the source context below.
If the context is insufficient, answer from general expert knowledge but say so explicitly.

Source context:
{context}

Question: {query}
"""
    response = AI_CLIENT.models.generate_content(model=MODEL, contents=prompt)
    return {
        "answer": response.text,
        "sources": list({node.metadata.get("source") for node in matched}),
    }


DIFFICULTY_GUIDANCE = {
    "EASY": "Basic recall and single-step reasoning. Suitable for a first attempt on the topic.",
    "MEDIUM": "Standard exam-level difficulty, mixing recall with applied reasoning.",
    "HARD": "Multi-step reasoning, tricky distractors, the kind that separates top scorers.",
    "EXPERT": "Toughest tier — ambiguous options, requires deep conceptual clarity and speed.",
}


def generate_mock_test(domain: str, target_tag: str, difficulty: str, num_questions: int = 20) -> dict:
    """Generates a set of MCQs at the requested difficulty tier for a given exam/subject."""
    guidance = DIFFICULTY_GUIDANCE.get(difficulty, DIFFICULTY_GUIDANCE["MEDIUM"])
    prompt = f"""
Generate {num_questions} multiple-choice questions for {target_tag} ({domain}).
Difficulty tier: {difficulty} — {guidance}
Return ONLY a JSON object:
{{"questions": [
  {{"question": str, "options": [str, str, str, str], "correct_index": int, "explanation": str}}
]}}
"""
    response = AI_CLIENT.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return _parse_json_response(response.text)


def grade_attempt(questions: list[dict], user_answers: list[int]) -> dict:
    """Server-side scoring — never trust a client-reported score."""
    total = len(questions)
    correct = 0
    review = []
    for i, q in enumerate(questions):
        given = user_answers[i] if i < len(user_answers) else -1
        is_correct = given == q["correct_index"]
        if is_correct:
            correct += 1
        review.append({
            "question": q["question"],
            "given_index": given,
            "correct_index": q["correct_index"],
            "is_correct": is_correct,
            "explanation": q.get("explanation", ""),
        })
    accuracy = round((correct / total) * 100, 2) if total else 0.0
    return {"correct": correct, "total": total, "accuracy_percentage": accuracy, "review": review}


def generate_routine(available_hours_per_day: int, focus_targets: list[str]) -> list[dict]:
    prompt = f"""
Design a 7-day study routine.
Daily study window: {available_hours_per_day} hours.
Targets: {', '.join(focus_targets)}.
Balance exam syllabus coverage (GS/Quant/Reasoning/Current Affairs), technical skill practice,
and active language use. Return ONLY a JSON array of objects with keys:
"day" (1-7), "start_time" (HH:MM), "end_time" (HH:MM), "topic", "domain_type".
"""
    response = AI_CLIENT.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return _parse_json_response(response.text)


def summarize_current_affairs(raw_text: str, target_exam: str) -> dict:
    """Turns a pasted news digest into a structured, exam-relevant summary."""
    prompt = f"""
Summarize the following news content into exam-relevant current affairs for {target_exam} aspirants.
Return ONLY a JSON object:
{{"headline": str, "summary": str (3-5 sentences), "relevance_tags": [str]}}

Content:
---
{raw_text[:6000]}
---
"""
    response = AI_CLIENT.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return _parse_json_response(response.text)
