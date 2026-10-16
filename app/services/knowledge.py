"""Private knowledge ingestion, bounded lexical retrieval and frozen source references."""
from __future__ import annotations

import asyncio
import hashlib
import math
import re
import unicodedata
from collections import Counter

from sqlalchemy import delete, or_, select

from app.core.errors import AppError
from app.db.models import KnowledgeBase, KnowledgeChunk, KnowledgeDocument
from app.integrations.documents import DocumentAdapter
from app.services.lessons import event, finish, serialize, uid

STOP_WORDS = set("a an and are as at be by for from in is it of on or that the this to was with".split())


def tokenize(text):
    normalized = unicodedata.normalize("NFKC", text).lower()
    tokens = []
    for word in re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", normalized):
        if "\u3400" <= word[0] <= "\u9fff":
            tokens.extend(word[i:i + 2] for i in range(max(1, len(word) - 1)))
        elif word not in STOP_WORDS:
            tokens.append(word)
    return tokens


async def owned_base(session, base_id, user, *, lock=False):
    query = select(KnowledgeBase).where(KnowledgeBase.id == base_id, KnowledgeBase.user_id == user.id,
        KnowledgeBase.tenant_id == user.tenant_id, KnowledgeBase.deleted_at.is_(None))
    # Metadata writes do not change FK keys: NO KEY UPDATE still serializes uploads/deletes
    # while allowing the parser's child inserts to acquire their required KEY SHARE.
    obj = await session.scalar(query.with_for_update(key_share=True) if lock else query)
    if not obj:
        raise AppError("NOT_FOUND", "未找到该知识库", 404)
    return obj


async def owned_document(session, document_id, user, *, lock=False):
    query = select(KnowledgeDocument).join(KnowledgeBase, KnowledgeBase.id == KnowledgeDocument.base_id).where(
        KnowledgeDocument.id == document_id, KnowledgeDocument.user_id == user.id,
        KnowledgeDocument.tenant_id == user.tenant_id, KnowledgeDocument.deleted_at.is_(None),
        KnowledgeBase.user_id == user.id, KnowledgeBase.tenant_id == user.tenant_id, KnowledgeBase.deleted_at.is_(None))
    obj = await session.scalar(query.with_for_update(of=KnowledgeDocument) if lock else query)
    if not obj:
        raise AppError("NOT_FOUND", "未找到该知识文档", 404)
    return obj


def document_view(document):
    # Storage keys never become public resource identifiers.
    return {key: value for key, value in serialize(document).items() if key != "file_path"}


def chunk_view(chunk, document, base, *, score=None):
    result = {"id": chunk.id, "document_id": document.id, "base_id": base.id,
        "filename": document.filename, "base_name": base.title, "ordinal": chunk.ordinal,
        "text": chunk.text, "content_hash": chunk.content_hash}
    if score is not None:
        result["score"] = round(score, 6)
    return result


async def search(session, user, query, base_ids, *, top_k=6, max_chunks=10000):
    tokens = list(dict.fromkeys(tokenize(query)))[:64]
    if not base_ids or not tokens:
        return []
    statement = select(KnowledgeChunk, KnowledgeDocument, KnowledgeBase).join(
        KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id).join(
        KnowledgeBase, KnowledgeBase.id == KnowledgeChunk.base_id).where(
        KnowledgeChunk.user_id == user.id, KnowledgeChunk.tenant_id == user.tenant_id,
        KnowledgeBase.user_id == user.id, KnowledgeBase.tenant_id == user.tenant_id,
        KnowledgeBase.id.in_(base_ids), KnowledgeBase.deleted_at.is_(None),
        KnowledgeDocument.user_id == user.id, KnowledgeDocument.tenant_id == user.tenant_id,
        KnowledgeDocument.status == "ready", KnowledgeDocument.deleted_at.is_(None),
        or_(*(KnowledgeChunk.search_text.contains(term, autoescape=True) for term in tokens)))
    rows = (await session.execute(statement.order_by(KnowledgeChunk.id).limit(max_chunks + 1))).all()
    if len(rows) > max_chunks:
        raise AppError("KNOWLEDGE_SEARCH_LIMIT", "匹配资料过多，请缩小知识库范围或使用更具体的检索词", 422)
    if not rows:
        return []
    average = sum(row[0].token_count for row in rows) / len(rows) or 1
    frequencies = {term: sum(term in row[0].token_counts for row in rows) for term in tokens}
    ranked = []
    for chunk, document, base in rows:
        score = 0.0
        for term in tokens:
            count = chunk.token_counts.get(term, 0)
            if count:
                inverse = math.log(1 + (len(rows) - frequencies[term] + .5) / (frequencies[term] + .5))
                score += inverse * count * 2.2 / (count + 1.2 * (.25 + .75 * chunk.token_count / average))
        if score > 0:
            ranked.append((score, chunk, document, base))
    ranked.sort(key=lambda row: (-row[0], row[1].id))
    return [chunk_view(chunk, document, base, score=score) for score, chunk, document, base in ranked[:top_k]]


async def freeze_context(session, user, base_ids, query, *, max_chunks=10000):
    if base_ids is None:
        base_ids = list((await session.scalars(select(KnowledgeBase.id).where(KnowledgeBase.user_id == user.id,
            KnowledgeBase.tenant_id == user.tenant_id, KnowledgeBase.deleted_at.is_(None)).order_by(KnowledgeBase.id).limit(51))).all())
        if len(base_ids) > 50:
            raise AppError("KNOWLEDGE_SCOPE_REQUIRED", "请为本次生成选择不超过50个知识库", 422)
    base_ids = list(dict.fromkeys(base_ids))
    for base_id in base_ids:
        await owned_base(session, base_id, user)
    hits = await search(session, user, query, base_ids, max_chunks=max_chunks)
    return base_ids, [{**hit, "source_ref": f"K{index + 1}"} for index, hit in enumerate(hits)]


async def parse_knowledge_document(executor, run_id, payload):
    """Parsing is repeatable; only the current execution may publish its chunk set."""
    app = executor.app
    async with app.state.db.session_factory() as session:
        run, _ = await executor.lock_execution(session, run_id)
        document = await session.get(KnowledgeDocument, payload["document_id"])
        base = await session.get(KnowledgeBase, document.base_id) if document else None
        if document and (document.user_id != run.user_id or document.tenant_id != run.tenant_id):
            raise AppError("KNOWLEDGE_OWNERSHIP_MISMATCH", "知识文档与任务所有者不一致", 404)
        if not document or document.deleted_at or not base or base.deleted_at or document.run_id != run.id:
            await finish(session, run, "cancelled")
            await session.commit()
            return
        filename, key = document.filename, document.file_path
        document.status = "processing"
        await session.commit()
    raw = await asyncio.to_thread(app.state.storage.get, key)
    parsed = await asyncio.to_thread(DocumentAdapter(max_upload_bytes=app.state.settings.upload_max_bytes).parse_upload, filename, raw)
    chunks = []
    for ordinal, item in enumerate(parsed["chunks"]):
        text = item["text"]
        counts = Counter(tokenize(text))
        chunks.append({"id": uid(), "ordinal": ordinal, "text": text,
            "content_hash": hashlib.sha256(text.encode()).hexdigest(), "search_text": " ".join(counts),
            "token_counts": dict(counts), "token_count": sum(counts.values())})
    async with app.state.db.session_factory() as session:
        run, _ = await executor.lock_execution(session, run_id)
        document = await session.scalar(select(KnowledgeDocument).where(KnowledgeDocument.id == payload["document_id"]).with_for_update())
        base = await session.get(KnowledgeBase, document.base_id) if document else None
        if not document or document.deleted_at or not base or base.deleted_at or document.run_id != run.id:
            await finish(session, run, "cancelled")
        else:
            await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
            session.add_all(KnowledgeChunk(document_id=document.id, base_id=base.id, user_id=run.user_id,
                tenant_id=run.tenant_id, **chunk) for chunk in chunks)
            document.status, document.chunk_count, document.error_message = "ready", len(chunks), None
            await event(session, run, "module_progress", {"phase": "knowledge_indexed", "document_id": document.id,
                "chunk_count": len(chunks)})
            await finish(session, run, "completed", result={"document_id": document.id, "chunk_count": len(chunks)})
        await session.commit()
    await app.state.dispatcher.notify(run_id)
