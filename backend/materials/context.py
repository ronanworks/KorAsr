"""Local lexical retrieval of relevant course-document excerpts."""

import re
import unicodedata

from sqlmodel import Session as DBSession, select

from backend.storage.database import engine
from backend.storage.models import SessionMaterial


MAX_CONTEXT_CHARS = 4_000
BLOCK_CHARS = 600
_WORD_RE = re.compile(r"[a-z0-9가-힣]+|[\u4e00-\u9fff]+", re.IGNORECASE)


def _terms(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    terms = set()
    for match in _WORD_RE.finditer(normalized):
        word = match.group()
        if len(word) >= 2:
            terms.add(word)
        if len(word) >= 3 and any("가" <= char <= "힣" or "\u4e00" <= char <= "\u9fff" for char in word):
            terms.update(word[i:i + 2] for i in range(len(word) - 1))
    return terms


def _blocks(text: str):
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = " ".join(paragraph.split())
        while paragraph:
            if len(paragraph) <= BLOCK_CHARS:
                yield paragraph
                break
            split = paragraph.rfind(" ", 0, BLOCK_CHARS + 1)
            split = split if split >= BLOCK_CHARS // 2 else BLOCK_CHARS
            yield paragraph[:split].strip()
            paragraph = paragraph[split:].strip()


def get_relevant_material_context(
    session_id: str, source_text: str, max_chars: int = 1_500
) -> str:
    """Return bounded, relevant local excerpts for an already-authorized session.

    No model or network request occurs. The caller must have already checked
    session ownership; this internal function intentionally accepts no user ID.
    Empty text or no lexical overlap returns an empty string.
    """
    budget = min(max(int(max_chars), 0), MAX_CONTEXT_CHARS)
    query_terms = _terms(source_text)
    if budget == 0 or not query_terms:
        return ""

    with DBSession(engine) as db:
        rows = db.exec(
            select(SessionMaterial)
            .where(SessionMaterial.session_id == session_id)
            .where(SessionMaterial.extraction_status == "ready")
            .order_by(SessionMaterial.created_at, SessionMaterial.id)
        ).all()
        ranked = []
        for material in rows:
            for position, block in enumerate(_blocks(material.extracted_text)):
                # Most slides/paragraphs do not discuss the current sentence.
                # A cheap substring check avoids tokenizing every unrelated
                # block on the latency-sensitive live path.
                normalized_block = unicodedata.normalize("NFKC", block).casefold()
                if not any(term in normalized_block for term in query_terms):
                    continue
                block_terms = _terms(block)
                overlap = query_terms & block_terms
                if not overlap:
                    continue
                score = len(overlap) / max(len(block_terms) ** 0.5, 1)
                ranked.append((score, material.created_at, position, material.original_filename, block))

    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    chosen = []
    used = 0
    for _score, _created_at, _position, filename, block in ranked:
        piece = f"[Material: {filename}]\n{block}"
        remaining = budget - used - (2 if chosen else 0)
        if remaining <= 0:
            break
        if len(piece) > remaining:
            if chosen:
                continue
            piece = piece[:remaining]
        chosen.append(piece)
        used += len(piece) + (2 if len(chosen) > 1 else 0)
        if used >= budget:
            break
    return "\n\n".join(chosen)
