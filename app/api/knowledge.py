"""Independent teacher-owned knowledge bases; files are parsed by background Workers."""
import asyncio
import hashlib
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from app.core.errors import AppError
from app.core.security import get_current_user
from app.db.models import KnowledgeBase, KnowledgeChunk, KnowledgeDocument, Outbox, Run, User
from app.db.session import get_session
from app.services.knowledge import chunk_view, document_view, owned_base, owned_document, search
from app.services.lessons import TERMINAL, digest, finish, now, serialize, uid
from app.tasks.queue import route_for

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_session)]
Teacher = Annotated[User, Depends(get_current_user)]


class BaseInput(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)

    @field_validator("title")
    @classmethod
    def trim_title(cls, value):
        if not value.strip():
            raise ValueError("知识库名称不能为空")
        return value.strip()


class SearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)


@router.get("/knowledge-bases")
async def list_bases(user: Teacher, session: Session):
    rows = (await session.scalars(select(KnowledgeBase).where(KnowledgeBase.user_id == user.id,
        KnowledgeBase.tenant_id == user.tenant_id, KnowledgeBase.deleted_at.is_(None)).order_by(KnowledgeBase.created_at.desc()))).all()
    counts = dict((await session.execute(select(KnowledgeDocument.base_id, func.count()).where(
        KnowledgeDocument.user_id == user.id, KnowledgeDocument.tenant_id == user.tenant_id,
        KnowledgeDocument.deleted_at.is_(None)).group_by(KnowledgeDocument.base_id))).all())
    return {"data": [{**serialize(base), "documents_count": counts.get(base.id, 0)} for base in rows]}


@router.post("/knowledge-bases")
async def create_base(body: BaseInput, user: Teacher, session: Session):
    base = KnowledgeBase(id=uid(), user_id=user.id, tenant_id=user.tenant_id, **body.model_dump())
    session.add(base)
    await session.commit()
    return {"data": {**serialize(base), "documents_count": 0}}


@router.patch("/knowledge-bases/{base_id}")
async def edit_base(base_id: str, body: BaseInput, user: Teacher, session: Session):
    base = await owned_base(session, base_id, user, lock=True)
    base.title, base.description, base.updated_at = body.title, body.description, now()
    await session.commit()
    return {"data": serialize(base)}


async def remove_document(session, document):
    # Match the Worker's Run -> document lock order to avoid delete/commit deadlocks.
    run = await session.scalar(select(Run).where(Run.id == document.run_id).with_for_update()) if document.run_id else None
    if run and run.status not in TERMINAL:
        run.cancel_requested = True
        await finish(session, run, "cancelled", message="知识文档已删除")
    await session.scalar(select(KnowledgeDocument).where(KnowledgeDocument.id == document.id).with_for_update())
    document.deleted_at, document.status = now(), "deleted"
    await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
    session.add(Outbox(id=uid(), event_type="delete_storage", payload={"keys": [document.file_path]}, status="pending"))


@router.delete("/knowledge-bases/{base_id}")
async def delete_base(base_id: str, request: Request, user: Teacher, session: Session):
    base = await owned_base(session, base_id, user, lock=True)
    documents = (await session.scalars(select(KnowledgeDocument).where(KnowledgeDocument.base_id == base.id,
        KnowledgeDocument.user_id == user.id, KnowledgeDocument.deleted_at.is_(None)).order_by(KnowledgeDocument.id))).all()
    for document in documents:
        await remove_document(session, document)
    base.deleted_at = now()
    await session.commit()
    for document in documents:
        if document.run_id:
            await request.app.state.dispatcher.notify(document.run_id)
    return {"data": {"deleted": True}}


@router.get("/knowledge-bases/{base_id}/documents")
async def list_documents(base_id: str, user: Teacher, session: Session):
    await owned_base(session, base_id, user)
    rows = (await session.scalars(select(KnowledgeDocument).where(KnowledgeDocument.base_id == base_id,
        KnowledgeDocument.user_id == user.id, KnowledgeDocument.deleted_at.is_(None))
        .order_by(KnowledgeDocument.created_at.desc()))).all()
    return {"data": [document_view(row) for row in rows]}


async def enqueue_parse(session, document, user):
    rid = uid()
    run = Run(id=rid, user_id=user.id, tenant_id=user.tenant_id, workflow_type="knowledge_document_parse",
        idempotency_key=f"knowledge-parse-{document.id}-{rid}", input_hash=digest({"document_id": document.id,
        "content_hash": document.content_hash}), payload={"document_id": document.id}, status="queued",
        job_id=f"run-{rid}", queue_name=route_for("knowledge_document_parse"))
    session.add(run)
    await session.flush()
    document.run_id, document.status, document.error_message = rid, "uploaded", None
    session.add(Outbox(id=uid(), run_id=rid, event_type="dispatch_run", payload={"run_id": rid, "execution_epoch": 0}, status="pending"))
    return run


@router.post("/knowledge-bases/{base_id}/documents")
async def upload_document(base_id: str, request: Request, user: Teacher, session: Session):
    await owned_base(session, base_id, user, lock=True)
    form = await request.form()
    file = form.get("file")
    if not isinstance(file, UploadFile):
        raise AppError("FILE_REQUIRED", "请选择需要上传的知识文档", 422)
    filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1][:255]
    suffix = Path(filename).suffix.lower()
    if suffix not in {".pdf", ".docx", ".md", ".markdown", ".txt"}:
        raise AppError("UNSUPPORTED_FILE", "支持 PDF、DOCX、Markdown 和 UTF-8 文本", 422)
    raw = await file.read(request.app.state.settings.upload_max_bytes + 1)
    if not raw or len(raw) > request.app.state.settings.upload_max_bytes:
        raise AppError("UPLOAD_SIZE_LIMIT", "文件为空或超过上传大小限制", 413)
    key = await asyncio.to_thread(request.app.state.storage.put, raw, suffix=suffix)
    try:
        document = KnowledgeDocument(id=uid(), base_id=base_id, user_id=user.id, tenant_id=user.tenant_id,
            filename=filename, file_path=key, content_hash=hashlib.sha256(raw).hexdigest(), status="uploaded")
        session.add(document)
        await session.flush()
        run = await enqueue_parse(session, document, user)
        await session.commit()
    except BaseException:
        await session.rollback()
        await asyncio.to_thread(request.app.state.storage.delete, key)
        raise
    await request.app.state.dispatcher.submit(run.id)
    return {"data": document_view(document)}


@router.get("/knowledge-documents/{document_id}")
async def get_document(document_id: str, user: Teacher, session: Session):
    return {"data": document_view(await owned_document(session, document_id, user))}


@router.delete("/knowledge-documents/{document_id}")
async def delete_document(document_id: str, request: Request, user: Teacher, session: Session):
    document = await owned_document(session, document_id, user)
    await remove_document(session, document)
    await session.commit()
    if document.run_id:
        await request.app.state.dispatcher.notify(document.run_id)
    return {"data": {"deleted": True}}


@router.post("/knowledge-documents/{document_id}/reindex")
async def reindex_document(document_id: str, request: Request, user: Teacher, session: Session):
    document = await owned_document(session, document_id, user, lock=True)
    if document.status in {"uploaded", "processing"}:
        raise AppError("RUN_ACTIVE", "文档仍在解析中", 409)
    run = await enqueue_parse(session, document, user)
    await session.commit()
    await request.app.state.dispatcher.submit(run.id)
    return {"data": document_view(document)}


@router.get("/knowledge-documents/{document_id}/chunks")
async def list_chunks(document_id: str, user: Teacher, session: Session,
                      offset: int = Query(default=0, ge=0), limit: int = Query(default=20, ge=1, le=100)):
    document = await owned_document(session, document_id, user)
    base = await owned_base(session, document.base_id, user)
    rows = (await session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id,
        KnowledgeChunk.user_id == user.id, KnowledgeChunk.tenant_id == user.tenant_id)
        .order_by(KnowledgeChunk.ordinal).offset(offset).limit(limit))).all()
    return {"data": {"items": [chunk_view(row, document, base) for row in rows], "total": document.chunk_count}}


@router.post("/knowledge-bases/{base_id}/search")
async def search_base(base_id: str, body: SearchInput, request: Request, user: Teacher, session: Session):
    await owned_base(session, base_id, user)
    hits = await search(session, user, body.query, [base_id], top_k=body.top_k,
                        max_chunks=request.app.state.settings.knowledge_search_max_chunks)
    return {"data": {"items": hits, "method": "lexical_bm25"}}
