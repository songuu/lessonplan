import asyncio
import json
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from app.core.catalog import normalize_modules, validate_lesson_input
from app.core.errors import AppError
from app.core.security import get_current_user
from app.db.models import (
    AnalyticsEvent,
    Draft,
    Event,
    Export,
    KnowledgeDocument,
    ModuleConfig,
    ModuleVersion,
    Outbox,
    PersonalLesson,
    PolishMessage,
    PolishSession,
    Reflection,
    Run,
    User,
)
from app.db.session import get_session
from app.services.lessons import (
    TERMINAL,
    active_run,
    assemble,
    check_lock,
    config_for,
    create_run,
    detail,
    digest,
    event,
    finish,
    input_of,
    invalidate,
    lock_run_with_draft,
    now,
    owned,
    serialize,
    uid,
    write_version,
)

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_session)]
Teacher = Annotated[User, Depends(get_current_user)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=100)
    topic: str = Field(min_length=1, max_length=200)
    school_stage: str = Field(min_length=1, max_length=30)
    grade: str = Field(min_length=1, max_length=30)
    subject: str = Field(min_length=1, max_length=40)
    lesson_type: str = Field(default="new_lesson", max_length=40)
    total_duration_min: int = Field(default=40, ge=5, le=240)
    requirements: str = Field(default="", max_length=12000)
    modules: list[dict] | None = Field(default=None, max_length=25)
    knowledge_base_ids: list[str] | None = Field(default=None, max_length=50)


class VersionLock(BaseModel):
    expected_content_version: int = Field(ge=1)
    expected_current_module_version_id: str | None
    expected_module_config_version_id: str


class SaveModule(VersionLock):
    teacher_view_content: str = Field(min_length=1, max_length=120000)
    source: Literal["manual_edit", "ai_polish"] = "manual_edit"
    teacher_instruction: str | None = Field(default=None, max_length=4000)


class Restore(VersionLock):
    version_id: str


class Step(BaseModel):
    module_id: str
    stage: Literal["outline", "activities"] | None = None


class OutlineStage(BaseModel):
    stage_id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=100)
    objective: str = Field(default="", max_length=1000)
    duration_min: int = Field(ge=1, le=240)


class Confirm(VersionLock):
    module_id: str
    outline: list[OutlineStage] | None = Field(default=None, min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_stages(self):
        if self.outline and len({s.stage_id for s in self.outline}) != len(self.outline):
            raise ValueError("大纲 stage_id 不可重复")
        return self


class PolishCreate(BaseModel):
    draft_id: str
    module_id: str
    scope: Literal["module", "selection"] = "module"
    selection_start: int | None = Field(default=None, ge=0)
    selection_end: int | None = Field(default=None, ge=1)


class PolishInput(BaseModel):
    instruction: str = Field(min_length=1, max_length=4000)
    working_content: str = Field(min_length=1, max_length=120000)


class ReflectionInput(BaseModel):
    content: str = Field(max_length=60000)
    expected_version: int = Field(default=0, ge=0)


def key_or_new(key):
    if key and len(key) > 180:
        raise AppError("INVALID_IDEMPOTENCY_KEY", "幂等键过长", 422)
    return key or str(uuid4())


async def dispatch(request, run):
    await request.app.state.dispatcher.submit(run.id)


async def style_context(session, user, data):
    from app.ai.assets import AssetBundle
    registry = AssetBundle().json("config/lesson_style_registry.json")
    references = (await session.scalars(select(PersonalLesson).where(
        PersonalLesson.user_id == user.id, PersonalLesson.status == "ready"))).all()
    def score(item):
        return sum(weight for field, weight in registry["matching_weights"].items()
                   if getattr(item, field) and getattr(item, field) == data.get(field))
    ranked = sorted([item for item in references if score(item) >= registry["minimum_personal_score"]], key=score, reverse=True)[:registry["maximum_references"]]
    # A teacher's nearby documents remain eligible; only minimal style summaries leave storage.
    summaries = [item.style_summary for item in ranked if item.style_summary]
    system_style = next((style for style in registry["system_styles"] if data.get("school_stage") in style["stages"]), registry["system_styles"][-1])
    value = {"source": "personal" if summaries else "system", "status": "used" if summaries else "fallback",
             "effective": bool(summaries), "reference_count": len(summaries), "summaries": summaries,
             "version": registry["version"], "system_style_id": system_style["id"], "style": system_style["style"]}
    value["hash"] = digest(value)
    return value


async def start(body, request, user, session, mode, key):
    payload = validate_lesson_input(body.model_dump())
    if payload.get("knowledge_base_ids") is None:
        payload.pop("knowledge_base_ids", None)
    stable_hash = digest({"input": payload, "mode": mode})
    key = key_or_new(key or body.request_id)
    # Serialize admission per owner in PostgreSQL; unique constraints also defend races.
    await session.scalar(select(User).where(User.id == user.id).with_for_update())
    existing = await session.scalar(select(Draft).where(Draft.user_id == user.id, Draft.request_id == body.request_id))
    keyed = await session.scalar(select(Run).where(Run.user_id == user.id, Run.idempotency_key == key))
    if existing or keyed:
        draft = existing or await session.get(Draft, keyed.draft_id)
        if not draft or draft.input_hash != stable_hash or draft.deleted_at:
            raise AppError("IDEMPOTENCY_CONFLICT", "请求标识已用于其他输入", 409)
        run = keyed or await session.scalar(select(Run).where(Run.draft_id == draft.id).order_by(Run.created_at))
        expected_workflow = "one_click" if mode == "one_click" else "interactive_step"
        if keyed and (keyed.draft_id != draft.id or keyed.workflow_type != expected_workflow
                      or keyed.payload.get("creation_request_id") != body.request_id):
            raise AppError("IDEMPOTENCY_CONFLICT", "幂等键已用于其他请求", 409)
        return {"data": {"draft_id": draft.id, "run_id": run.id}}
    modules = normalize_modules(body.modules if body.modules is not None else user.preferences.get("modules"))
    from app.services.knowledge import freeze_context
    knowledge_ids, knowledge_context = await freeze_context(session, user, body.knowledge_base_ids,
        body.topic + " " + body.requirements, max_chunks=request.app.state.settings.knowledge_search_max_chunks)
    did, cid = uid(), uid()
    draft = Draft(id=did, user_id=user.id, tenant_id=user.tenant_id, request_id=body.request_id,
        input_hash=stable_hash, topic=body.topic.strip(), school_stage=payload["school_stage"], grade=payload["grade"],
        subject=payload["subject"], lesson_type=payload["lesson_type"], total_duration_min=body.total_duration_min,
        requirements=body.requirements, knowledge_base_ids=knowledge_ids, knowledge_context_snapshot=knowledge_context,
        generation_mode=mode, status="creating", completion_status="writing",
        content_version=1, module_config_version_id=cid, modules=modules, current_module_version_map={},
        module_state_map={m["module_id"]: {"generation_status": "pending" if m.get("enabled", True) else "disabled",
            "confirmation_status": "unconfirmed", "freshness_status": "fresh", "freshness": "fresh"} for m in modules},
        style_intent_snapshot=await style_context(session, user, payload), hard_gate_status="not_checked",
        semantic_review_status="not_enabled")
    session.add(draft)
    await session.flush()
    session.add(ModuleConfig(id=cid, draft_id=did, modules=modules))
    first = next(m for m in modules if m.get("enabled", True))
    run_payload = {"creation_request_id": body.request_id} if mode == "one_click" else {"creation_request_id": body.request_id, "module_id": first["module_id"],
        "stage": "outline" if first["module_type"] == "teaching_process" else None}
    run = await create_run(session, draft, user, "one_click" if mode == "one_click" else "interactive_step", run_payload, key)
    await session.commit()
    await dispatch(request, run)
    return {"data": {"draft_id": draft.id, "run_id": run.id}}


@router.post("/lesson-drafts/generations/one-click")
async def one_click(body: Input, request: Request, user: Teacher, session: Session,
                    idempotency_key: str | None = Header(default=None)):
    async with request.app.state.admission_lock:
        return await start(body, request, user, session, "one_click", idempotency_key)


@router.post("/lesson-drafts/generations/interactive")
async def interactive(body: Input, request: Request, user: Teacher, session: Session,
                      idempotency_key: str | None = Header(default=None)):
    async with request.app.state.admission_lock:
        return await start(body, request, user, session, "interactive_step", idempotency_key)


@router.get("/lesson-drafts")
async def list_drafts(user: Teacher, session: Session, q: str = "", status: str | None = None,
                      limit: int = 100, offset: int = 0):
    query = select(Draft).where(Draft.user_id == user.id, Draft.deleted_at.is_(None))
    if q:
        query = query.where(Draft.topic.icontains(q, autoescape=True))
    if status:
        query = query.where(Draft.status == status)
    items = (await session.scalars(query.order_by(Draft.updated_at.desc()).limit(min(max(limit, 1), 200)).offset(max(offset, 0)))).all()
    return {"data": [serialize(d) for d in items]}


@router.get("/lesson-drafts/{draft_id}")
async def get_draft(draft_id: str, user: Teacher, session: Session):
    return {"data": await detail(session, await owned(session, Draft, draft_id, user))}


@router.delete("/lesson-drafts/{draft_id}")
async def delete_draft(draft_id: str, user: Teacher, session: Session):
    draft = await owned(session, Draft, draft_id, user, lock=True)
    draft.deleted_at = now()
    runs = (await session.scalars(select(Run).where(Run.draft_id == draft.id, Run.status.in_(["queued", "running"])).with_for_update())).all()
    for run in runs:
        run.cancel_requested = True
        await finish(session, run, "cancelled", code="DRAFT_DELETED", message="教案已删除")
        await close_artifact(session, run, "教案已删除，任务已取消")
    await session.commit()
    return {"data": {"deleted": True}}


@router.post("/lesson-drafts/{draft_id}/generations/one-click")
async def retry_draft(draft_id: str, request: Request, user: Teacher, session: Session,
                      idempotency_key: str | None = Header(default=None)):
    draft = await owned(session, Draft, draft_id, user, lock=True)
    run = await create_run(session, draft, user, "one_click", {}, key_or_new(idempotency_key))
    await session.commit()
    await dispatch(request, run)
    return {"data": {"draft_id": draft.id, "run_id": run.id}}


@router.post("/lesson-drafts/{draft_id}/interactive/step")
async def step(draft_id: str, body: Step, request: Request, user: Teacher, session: Session,
               idempotency_key: str | None = Header(default=None)):
    draft = await owned(session, Draft, draft_id, user, lock=True)
    module = config_for(draft, body.module_id)
    if draft.generation_mode != "interactive_step":
        raise AppError("INVALID_WORKFLOW", "此教案不属于结构化生成", 409)
    state = draft.module_state_map.get(body.module_id, {})
    stage = body.stage
    if module["module_type"] == "teaching_process":
        stage = stage or "outline"
        if stage == "activities" and not state.get("outline_confirmed"):
            raise AppError("OUTLINE_NOT_CONFIRMED", "请先确认教学过程大纲", 409)
    elif stage:
        raise AppError("INVALID_STAGE", "此模块不支持教学过程阶段", 422)
    # Generation follows the frozen order; unconfirmed earlier candidates are never stable context.
    for preceding in draft.modules:
        if preceding["module_id"] == body.module_id:
            break
        if preceding.get("enabled", True) and draft.module_state_map.get(preceding["module_id"], {}).get("confirmation_status") != "confirmed":
            raise AppError("PREVIOUS_MODULE_UNCONFIRMED", "请先确认前面的模块", 409)
    run = await create_run(session, draft, user, "interactive_step", {"module_id": body.module_id, "stage": stage}, key_or_new(idempotency_key))
    await session.commit()
    await dispatch(request, run)
    return {"data": {"draft_id": draft.id, "run_id": run.id}}


@router.post("/lesson-drafts/{draft_id}/interactive/confirm")
async def confirm(draft_id: str, body: Confirm, user: Teacher, session: Session):
    draft = await owned(session, Draft, draft_id, user, lock=True)
    config_for(draft, body.module_id)
    check_lock(draft, body.module_id, body)
    if await active_run(session, draft.id):
        raise AppError("RUN_ACTIVE", "请等待当前生成完成", 409)
    vid = draft.current_module_version_map.get(body.module_id)
    version = await session.get(ModuleVersion, vid) if vid else None
    if not version:
        raise AppError("MODULE_NOT_READY", "模块尚未生成", 409)
    state = dict(draft.module_state_map[body.module_id])
    if state.get("workflow_stage") == "outline":
        outline = [s.model_dump() for s in body.outline] if body.outline else state.get("outline")
        if not outline:
            raise AppError("OUTLINE_REQUIRED", "教学过程需要有效大纲", 422)
        state.update(outline=outline, outline_confirmed=True, confirmation_status="unconfirmed")
    else:
        state.update(confirmation_status="confirmed", confirmed_version_id=vid)
        version.confirmation_status = "confirmed"
    draft.module_state_map = {**draft.module_state_map, body.module_id: state}
    draft.content_version += 1
    await session.commit()
    return {"data": await detail(session, draft)}


@router.post("/lesson-drafts/{draft_id}/interactive/finalize")
async def finalize(draft_id: str, request: Request, user: Teacher, session: Session):
    draft = await owned(session, Draft, draft_id, user, lock=True)
    if await active_run(session, draft.id):
        raise AppError("RUN_ACTIVE", "请等待当前生成完成", 409)
    for module in draft.modules:
        if not module.get("enabled", True):
            continue
        state = draft.module_state_map.get(module["module_id"], {})
        if state.get("confirmation_status") != "confirmed" or state.get("workflow_stage") == "outline":
            raise AppError("MODULES_UNCONFIRMED", "请完成并确认所有启用模块", 409)
    if draft.hard_gate_status != "passed":
        raise AppError("QUALITY_NOT_PASSED", "教案尚未通过质量检查", 409)
    draft.status, draft.completion_status = "ready", "completed"
    snapshot = await assemble(session, draft)
    run = await create_run(session, draft, user, "interactive_finalize", {}, f"finalize-{draft.id}-{draft.content_version}", dispatch_required=False)
    if run.status not in TERMINAL:
        await event(session, run, "run_started", {"run_id": run.id})
        await event(session, run, "draft_ready", {"draft_id": draft.id, "snapshot_id": snapshot.id})
        await finish(session, run, "completed", result={"snapshot_id": snapshot.id})
    from app.services.lessons import WorkflowExecutor
    review_run = await WorkflowExecutor(request.app).queue_review(session, draft, run)
    await session.commit()
    if review_run:
        await dispatch(request, review_run)
    return {"data": {"draft_id": draft.id, "snapshot_id": snapshot.id, "run_id": run.id}}


async def save(draft_id, module_id, body, user, session, *, restore=None):
    draft = await owned(session, Draft, draft_id, user, lock=True)
    module = config_for(draft, module_id)
    check_lock(draft, module_id, body)
    if await active_run(session, draft.id):
        raise AppError("RUN_ACTIVE", "请等待当前运行完成再保存", 409)
    # Use the same normalization and safety boundary as AI output.
    content = restore.teacher_view_content if restore else body.teacher_view_content
    from app.ai.runtime import validate_teacher_content
    previous = await session.get(ModuleVersion, draft.current_module_version_map.get(module_id)) if draft.current_module_version_map.get(module_id) else None
    canonical = restore.canonical_content if restore else (previous.canonical_content if previous else None)
    from app.ai.runtime import RuntimeError as ContentError
    try:
        normalized = await asyncio.to_thread(validate_teacher_content, content, input_of(draft), module, canonical=canonical)
    except ContentError as exc:
        raise AppError(exc.code, exc.message, 422) from None
    content, canonical = normalized["teacher_view_content"], normalized.get("canonical_content", canonical)
    invalidate(draft, module_id)
    await write_version(session, draft, module_id, content, canonical=canonical,
        source="version_restore" if restore else body.source, confirmed=True,
        instruction=None if restore else body.teacher_instruction,
        stage=draft.module_state_map.get(module_id, {}).get("workflow_stage"))
    enabled = [m for m in draft.modules if m.get("enabled", True)]
    if draft.generation_mode == "one_click" and all(draft.current_module_version_map.get(m["module_id"]) for m in enabled):
        draft.status, draft.completion_status = "ready", "completed"
    await session.commit()
    return {"data": await detail(session, draft)}


@router.patch("/lesson-drafts/{draft_id}/modules/{module_id}")
async def save_module(draft_id: str, module_id: str, body: SaveModule, user: Teacher, session: Session):
    return await save(draft_id, module_id, body, user, session)


@router.get("/lesson-drafts/{draft_id}/modules/{module_id}/versions")
async def versions(draft_id: str, module_id: str, user: Teacher, session: Session):
    draft = await owned(session, Draft, draft_id, user)
    config_for(draft, module_id)
    records = (await session.scalars(select(ModuleVersion).where(ModuleVersion.draft_id == draft_id,
        ModuleVersion.module_id == module_id).order_by(ModuleVersion.version_no.desc()))).all()
    return {"data": [serialize(v) for v in records]}


@router.post("/lesson-drafts/{draft_id}/modules/{module_id}/restore")
async def restore(draft_id: str, module_id: str, body: Restore, user: Teacher, session: Session):
    version = await session.get(ModuleVersion, body.version_id)
    if not version or version.draft_id != draft_id or version.module_id != module_id:
        raise AppError("NOT_FOUND", "版本不存在", 404)
    return await save(draft_id, module_id, body, user, session, restore=version)


@router.get("/generation-runs/{run_id}")
async def get_run(run_id: str, user: Teacher, session: Session):
    return {"data": serialize(await owned(session, Run, run_id, user))}


@router.post("/generation-runs/{run_id}/cancel")
async def cancel(run_id: str, request: Request, user: Teacher, session: Session):
    await owned(session, Run, run_id, user)
    run, draft = await lock_run_with_draft(session, run_id)
    if run.status not in TERMINAL:
        run.cancel_requested = True
        await finish(session, run, "cancelled", code="USER_CANCELLED", message="已取消生成")
        await close_artifact(session, run, "任务已取消")
        if draft and run.workflow_type in {"one_click", "interactive_step"}:
            draft.status, draft.completion_status = "cancelled", "cancelled"
        await session.commit()
        await request.app.state.dispatcher.notify(run_id)
    return {"data": serialize(run)}


async def close_artifact(session, run, message):
    target = None
    if run.workflow_type == "export":
        target = await session.get(Export, run.payload.get("export_id"))
    elif run.workflow_type == "document_parse":
        target = await session.get(PersonalLesson, run.payload.get("document_id"))
    elif run.workflow_type == "knowledge_document_parse":
        target = await session.get(KnowledgeDocument, run.payload.get("document_id"))
    if target:
        target.status, target.error_message = "failed", message


@router.get("/generation-runs/{run_id}/events")
async def stream(run_id: str, request: Request, user: Teacher, session: Session,
                 last_event_id: str | None = Header(default=None), after: int = 0):
    await owned(session, Run, run_id, user)
    try:
        cursor = max(int(last_event_id or after), 0)
    except ValueError as exc:
        raise AppError("INVALID_EVENT_ID", "事件序号不正确", 422) from exc
    # Do not pin an authenticated request's database connection for a long SSE session.
    await session.rollback()
    async def generate():
        sequence, idle, pubsub = cursor, 0, None
        try:
            redis = request.app.state.dispatcher.redis
            if redis:
                try:
                    pubsub = redis.pubsub()
                    await pubsub.subscribe(f"lesson:run:{run_id}")
                except Exception:
                    if pubsub:
                        await pubsub.aclose()
                    pubsub = None
            while not await request.is_disconnected():
                async with request.app.state.db.session_factory() as stream_session:
                    run = await stream_session.get(Run, run_id)
                    events = (await stream_session.scalars(select(Event).where(Event.run_id == run_id,
                        Event.sequence > sequence).order_by(Event.sequence).limit(500))).all()
                    terminal = run.status in TERMINAL
                for item in events:
                    payload = {"run_id": run_id, "sequence": item.sequence, **item.payload}
                    yield f"id: {item.sequence}\nevent: {item.event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    sequence = item.sequence
                if terminal and len(events) < 500:
                    return
                idle = 0 if events else idle + 1
                if idle >= (10 if pubsub else 40):
                    yield "event: heartbeat\ndata: {}\n\n"
                    idle = 0
                if pubsub:
                    try:
                        await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
                    except Exception:
                        await pubsub.aclose()
                        pubsub = None
                else:
                    await asyncio.sleep(0.25)
        finally:
            if pubsub:
                await pubsub.aclose()
    return StreamingResponse(generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/module-polish-sessions")
async def polish_create(body: PolishCreate, user: Teacher, session: Session):
    draft = await owned(session, Draft, body.draft_id, user)
    config_for(draft, body.module_id)
    vid = draft.current_module_version_map.get(body.module_id)
    if not vid:
        raise AppError("MODULE_NOT_READY", "请先生成当前模块", 409)
    if body.scope == "selection" and (body.selection_start is None or body.selection_end is None or body.selection_end <= body.selection_start):
        raise AppError("INVALID_SELECTION", "选区范围不正确", 422)
    item = PolishSession(id=uid(), user_id=user.id, draft_id=draft.id, module_id=body.module_id,
        kind="module", scope=body.scope, base_version_id=vid, selection_start=body.selection_start,
        selection_end=body.selection_end)
    session.add(item)
    await session.commit()
    return {"data": serialize(item)}


@router.get("/module-polish-sessions/{session_id}")
async def polish_get(session_id: str, user: Teacher, session: Session):
    item = await owned(session, PolishSession, session_id, user)
    await owned(session, Draft, item.draft_id, user)
    messages = (await session.scalars(select(PolishMessage).where(PolishMessage.session_id == item.id).order_by(PolishMessage.created_at))).all()
    return {"data": {**serialize(item), "messages": [serialize(m) for m in messages]}}


async def send_polish(item, body, request, user, session, key):
    draft = await owned(session, Draft, item.draft_id, user, lock=True)
    if item.kind == "module" and draft.current_module_version_map.get(item.module_id) != item.base_version_id:
        raise AppError("VERSION_CONFLICT", "模块已更新，请重新打开润色", 409)
    if item.scope == "selection" and item.selection_end > len(body.working_content):
        raise AppError("INVALID_SELECTION", "选区已超出正文", 422)
    payload = {**body.model_dump(), "session_id": item.id, "message_id": uid(), "module_id": item.module_id,
        "scope": item.scope, "selection_start": item.selection_start, "selection_end": item.selection_end}
    workflow = "module_polish_message" if item.kind == "module" else "reflection_polish_message"
    run = await create_run(session, draft, user, workflow, payload, key_or_new(key),
        logical_payload={**body.model_dump(), "session_id": item.id})
    if run.payload.get("message_id") != payload["message_id"]:
        return {"data": {"run_id": run.id, "session_id": item.id}}
    session.add(PolishMessage(id=payload["message_id"], session_id=item.id, run_id=run.id,
        instruction=body.instruction, working_content=body.working_content))
    await session.commit()
    await dispatch(request, run)
    return {"data": {"run_id": run.id, "session_id": item.id}}


@router.post("/module-polish-sessions/{session_id}/messages")
async def polish_message(session_id: str, body: PolishInput, request: Request, user: Teacher, session: Session,
                         idempotency_key: str | None = Header(default=None)):
    return await send_polish(await owned(session, PolishSession, session_id, user), body,
                             request, user, session, idempotency_key)


@router.post("/lesson-drafts/{draft_id}/preview")
async def preview(draft_id: str, user: Teacher, session: Session):
    draft = await owned(session, Draft, draft_id, user, lock=True)
    if await active_run(session, draft.id):
        raise AppError("RUN_ACTIVE", "生成完成后即可预览", 409)
    snapshot = await assemble(session, draft)
    await session.commit()
    return {"data": serialize(snapshot)}


class ExportInput(BaseModel):
    format: Literal["docx", "pdf"]


@router.post("/lesson-drafts/{draft_id}/exports")
async def export_create(draft_id: str, body: ExportInput, request: Request, user: Teacher, session: Session,
                        idempotency_key: str | None = Header(default=None)):
    draft = await owned(session, Draft, draft_id, user, lock=True)
    if idempotency_key:
        old = await session.scalar(select(Run).where(Run.user_id == user.id, Run.idempotency_key == idempotency_key))
        if old:
            if old.workflow_type != "export" or old.draft_id != draft.id or old.payload.get("format") != body.format:
                raise AppError("IDEMPOTENCY_CONFLICT", "幂等键已用于其他输入", 409)
            record = await session.get(Export, old.payload["export_id"])
            return {"data": {**serialize(record), "run_id": old.id}}
    if draft.status != "ready" or draft.completion_status != "completed" or draft.hard_gate_status != "passed":
        raise AppError("DRAFT_NOT_EXPORTABLE", "教案全部完成并通过检查后即可导出", 409)
    if await active_run(session, draft.id):
        raise AppError("RUN_ACTIVE", "当前任务完成后即可导出", 409)
    snapshot = await assemble(session, draft)
    for module in draft.modules:
        if module.get("enabled", True):
            state = draft.module_state_map.get(module["module_id"], {})
            if state.get("generation_status") != "ready" or state.get("workflow_stage") == "outline":
                raise AppError("DRAFT_NOT_EXPORTABLE", "仍有模块未完整生成", 409)
    await session.flush()
    record = Export(id=uid(), draft_id=draft.id, snapshot_id=snapshot.id, user_id=user.id,
        format=body.format, status="processing", warnings=snapshot.warnings)
    session.add(record)
    await session.flush()
    run = await create_run(session, draft, user, "export", {"export_id": record.id, "format": body.format}, key_or_new(idempotency_key))
    await session.commit()
    await dispatch(request, run)
    return {"data": {**serialize(record), "run_id": run.id}}


@router.get("/lesson-exports/{export_id}")
async def export_get(export_id: str, user: Teacher, session: Session):
    record = await owned(session, Export, export_id, user)
    await owned(session, Draft, record.draft_id, user)
    return {"data": serialize(record)}


@router.get("/lesson-exports/{export_id}/download")
async def export_download(export_id: str, request: Request, user: Teacher, session: Session):
    record = await owned(session, Export, export_id, user)
    await owned(session, Draft, record.draft_id, user)
    if record.status != "success" or not record.file_path:
        raise AppError("EXPORT_NOT_READY", "导出文件尚未就绪", 409)
    content = await asyncio.to_thread(request.app.state.storage.get, record.file_path)
    media = "application/pdf" if record.format == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return Response(content, media_type=media, headers={"Content-Disposition": f'attachment; filename="lesson-{record.id}.{record.format}"'})


@router.get("/lesson-drafts/{draft_id}/post-lesson-reflection")
async def reflection_get(draft_id: str, user: Teacher, session: Session):
    await owned(session, Draft, draft_id, user)
    item = await session.scalar(select(Reflection).where(Reflection.draft_id == draft_id))
    return {"data": serialize(item) or {"content": "", "version": 0}}


@router.put("/lesson-drafts/{draft_id}/post-lesson-reflection")
async def reflection_save(draft_id: str, body: ReflectionInput, user: Teacher, session: Session):
    await owned(session, Draft, draft_id, user, lock=True)
    item = await session.scalar(select(Reflection).where(Reflection.draft_id == draft_id).with_for_update())
    if (item.version if item else 0) != body.expected_version:
        raise AppError("VERSION_CONFLICT", "反思已更新，请刷新后重试", 409)
    if item:
        item.content, item.version = body.content, item.version + 1
    else:
        item = Reflection(id=uid(), draft_id=draft_id, user_id=user.id, content=body.content, version=1)
        session.add(item)
    await session.commit()
    return {"data": serialize(item)}


@router.post("/lesson-drafts/{draft_id}/post-lesson-reflection/polish")
async def reflection_polish(draft_id: str, body: PolishInput, request: Request, user: Teacher, session: Session):
    await owned(session, Draft, draft_id, user, lock=True)
    item = PolishSession(id=uid(), user_id=user.id, draft_id=draft_id, module_id=None, kind="reflection", scope="module")
    session.add(item)
    await session.flush()
    return await send_polish(item, body, request, user, session, None)


@router.get("/personal-lessons")
async def personal_list(user: Teacher, session: Session):
    items = (await session.scalars(select(PersonalLesson).where(PersonalLesson.user_id == user.id).order_by(PersonalLesson.created_at.desc()).limit(200))).all()
    return {"data": [{**{k: v for k, v in serialize(item).items() if k not in {"content", "file_path", "thumbnail_path", "chunks"}},
                      "thumbnail_url": f"/api/v1/personal-lessons/{item.id}/thumbnail" if item.thumbnail_path else None} for item in items]}


@router.post("/personal-lessons", openapi_extra={"requestBody": {"required": True, "content": {
    "multipart/form-data": {"schema": {"type": "object", "required": ["file"],
        "properties": {"file": {"type": "string", "format": "binary"}}}}}}})
async def personal_upload(request: Request, user: Teacher, session: Session):
    # Auth dependencies run before this explicit parser; the outer ASGI guard also bounds chunked bodies.
    async with request.form(max_files=1, max_fields=0) as form:
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise AppError("FILE_REQUIRED", "请选择一个教案文件", 422)
        filename = Path(file.filename or "").name
        suffix = Path(filename).suffix.lower()
        if suffix not in {".docx", ".pdf"}:
            raise AppError("UNSUPPORTED_FILE", "请上传 DOCX 或 PDF 文件", 422)
        raw = await file.read(request.app.state.settings.upload_max_bytes + 1)
    if not raw or len(raw) > request.app.state.settings.upload_max_bytes:
        raise AppError("FILE_TOO_LARGE", "文件为空或超出大小限制", 422)
    if (suffix == ".pdf" and not raw.startswith(b"%PDF")) or (suffix == ".docx" and not raw.startswith(b"PK")):
        raise AppError("INVALID_FILE", "文件内容与扩展名不符", 422)
    key = await asyncio.to_thread(request.app.state.storage.put, raw, suffix=suffix)
    document = PersonalLesson(id=uid(), user_id=user.id, tenant_id=user.tenant_id, filename=filename,
                              file_path=key, status="processing")
    rid = uid()
    run = Run(id=rid, draft_id=None, user_id=user.id, tenant_id=user.tenant_id, workflow_type="document_parse",
        idempotency_key=f"upload-{document.id}", input_hash=digest(raw.hex()), payload={"document_id": document.id},
        status="queued", queue_name="lesson:thumbnail", job_id=f"run-{rid}")
    session.add_all([document, run])
    await session.flush()
    session.add(Outbox(id=uid(), run_id=rid, event_type="dispatch_run", payload={"run_id": rid}, status="pending"))
    await session.commit()
    await dispatch(request, run)
    return {"data": {"id": document.id, "run_id": rid, "status": document.status}}


@router.delete("/personal-lessons/{document_id}")
async def personal_delete(document_id: str, request: Request, user: Teacher, session: Session):
    await owned(session, PersonalLesson, document_id, user)
    runs = (await session.scalars(select(Run).where(Run.user_id == user.id,
        Run.workflow_type == "document_parse", Run.status.in_(["queued", "running"]),
        Run.payload["document_id"].as_string() == document_id).with_for_update())).all()
    # Match the worker's Run -> PersonalLesson lock order, including implicit FK/write locks.
    item = await owned(session, PersonalLesson, document_id, user, lock=True)
    for run in runs:
        run.cancel_requested = True
        await finish(session, run, "cancelled", code="DOCUMENT_DELETED", message="文档已删除")
    keys = [key for key in [item.file_path, item.thumbnail_path] if key]
    intent = Outbox(id=uid(), run_id=None, event_type="delete_storage", payload={"keys": keys}, status="pending")
    session.add(intent)
    await session.delete(item)
    await session.commit()
    for key in keys:
        await asyncio.to_thread(request.app.state.storage.delete, key)
    intent.status = "published"
    await session.commit()
    return {"data": {"deleted": True}}


@router.get("/personal-lessons/{document_id}/thumbnail")
async def personal_thumbnail(document_id: str, request: Request, user: Teacher, session: Session):
    item = await owned(session, PersonalLesson, document_id, user)
    if not item.thumbnail_path:
        raise AppError("THUMBNAIL_NOT_READY", "缩略图尚未就绪", 404)
    content = await asyncio.to_thread(request.app.state.storage.get, item.thumbnail_path)
    return Response(content, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})


@router.get("/personal-lessons/{document_id}/download")
async def personal_download(document_id: str, request: Request, user: Teacher, session: Session):
    item = await owned(session, PersonalLesson, document_id, user)
    content = await asyncio.to_thread(request.app.state.storage.get, item.file_path)
    return Response(content, media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="lesson-{item.id}{Path(item.filename).suffix}"'})


class AnalyticsInput(BaseModel):
    event_name: str = Field(min_length=1, max_length=100)
    properties: dict = Field(default_factory=dict)


@router.post("/analytics/events")
async def analytics(body: AnalyticsInput, user: Teacher, session: Session):
    allowed = {"generation_started", "module_confirmed", "polish_started", "polish_applied", "module_saved",
               "version_restored", "preview_opened", "export_started", "reflection_saved", "sse_reconnected", "sse_failed"}
    if body.event_name not in allowed:
        raise AppError("UNKNOWN_EVENT", "不支持的埋点事件", 422)
    allowed_properties = {"draft_id", "run_id", "module_id", "format", "mode", "duration_ms", "error_code"}
    session.add(AnalyticsEvent(id=uid(), user_id=user.id, event_name=body.event_name,
        properties={k: v for k, v in body.properties.items() if k in allowed_properties and isinstance(v, (str, int, float, bool))}))
    await session.commit()
    return {"data": {"accepted": True}}
