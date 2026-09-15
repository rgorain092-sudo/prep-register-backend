"""
Current affairs are intentionally kept simple and free-tier friendly:
instead of scraping news sites (fragile, and often against ToS), the
student pastes text from any source (a newspaper site, a digest email,
a PDF they extracted) and the AI turns it into a structured, tagged entry.
"""
import uuid
import ai_service


async def create_digest_entry(pool, target_exam: str, raw_text: str) -> dict:
    structured = ai_service.summarize_current_affairs(raw_text, target_exam)
    entry_id = str(uuid.uuid4())
    await pool.execute(
        """INSERT INTO current_affairs (id, target_exam, headline, summary, relevance_tags)
           VALUES ($1, $2, $3, $4, $5)""",
        entry_id,
        target_exam,
        structured["headline"],
        structured["summary"],
        structured.get("relevance_tags", []),
    )
    return {"id": entry_id, "target_exam": target_exam, **structured}


async def list_for_exam(pool, target_exam: str, limit: int = 30) -> list[dict]:
    rows = await pool.fetch(
        """SELECT id, target_exam, headline, summary, source_url, relevance_tags, published_date
           FROM current_affairs WHERE target_exam = $1
           ORDER BY published_date DESC LIMIT $2""",
        target_exam, limit,
    )
    return [dict(r) for r in rows]
