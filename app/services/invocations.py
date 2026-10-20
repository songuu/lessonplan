"""Write-once model receipts; execution leases authorize application, not loss of paid results."""
import copy
import hashlib
import re

from sqlalchemy import select, update

from app.core.errors import AppError
from app.db.models import LLMInvocation, Run
from app.services.lessons import LeaseLost, database_now, digest, lock_matching_run, lock_run_with_draft, uid

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_STRINGS = {"component_name", "component_kind", "llm_provider", "model_name", "prompt_version",
    "call_outcome", "primary_attempt_outcome", "provider_request_id", "finish_reason", "provider", "outcome", "error_code"}
_NUMBERS = {"elapsed_ms", "prompt_tokens", "completion_tokens", "total_tokens"}
_BOOLEANS = {"fallback_used", "is_demo", "usage_reported"}


def safe_response_log(value):
    result = {}
    for key, item in (value or {}).items():
        if key in _STRINGS and isinstance(item, str) and len(item) <= 200 and not any(ord(c) < 32 for c in item):
            result[key] = item
        elif key in _NUMBERS and isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 10**12:
            result[key] = item
        elif key in _BOOLEANS and isinstance(item, bool):
            result[key] = item
        elif key == "attempts" and isinstance(item, list):
            result[key] = [safe_response_log(entry) for entry in item[:8] if isinstance(entry, dict)]
    return result


def _receipt(row, *, claimed=False):
    fields = ("id", "invocation_key", "step_key", "attempt", "call_id", "call_index", "status", "request_hash",
        "module_ids", "route", "response_body", "response_hash", "response_log", "error_code", "capture_epoch")
    return {**{key: copy.deepcopy(getattr(row, key)) for key in fields}, "claimed": claimed}


class InvocationJournal:
    def __init__(self, executor, run_id, fingerprint):
        if executor.lease is None or executor.lease.run_id != run_id:
            raise LeaseLost()
        self.executor, self.run_id, self.fingerprint = executor, run_id, fingerprint
        # Retain the exact transport identity even when another process takes the Run lease.
        self.lease = executor.lease

    @classmethod
    def for_workflow(cls, executor, run_id, fingerprint):
        settings = executor.app.state.settings
        # Explicit model settings only: never persist API keys, credentials or arbitrary config.
        names = ("ai_provider", "llm_base_url", "llm_model", "llm_one_click_model", "llm_structured_model",
            "llm_module_operation_model", "llm_review_model", "llm_fallback_model", "llm_fallback_base_url", "llm_max_tokens")
        context = digest({"workflow": fingerprint, "model_config": {name: getattr(settings, name) for name in names}})
        return cls(executor, run_id, context)

    def _context(self, row):
        if row.context_fingerprint != self.fingerprint:
            raise AppError("LLM_INVOCATION_CONTEXT_CHANGED", "模型调用上下文已变化，不能复用旧响应", 409)

    async def find(self, step_key, attempt):
        async with self.executor.app.state.db.session_factory() as session:
            await self.executor.lock_execution(session, self.run_id)
            row = await session.scalar(select(LLMInvocation).where(LLMInvocation.run_id == self.run_id,
                LLMInvocation.step_key == step_key, LLMInvocation.attempt == attempt).with_for_update())
            if row is None:
                return None
            self._context(row)
            if row.status == "in_flight" and (row.capture_owner != self.lease.owner or row.capture_epoch != self.lease.epoch):
                row.status, row.error_code = "outcome_unknown", "LLM_OUTCOME_UNKNOWN"
            result = _receipt(row)
            await session.commit()
            return result

    async def reserve(self, step_key, attempt, request_hash, call_id, call_index, module_ids, route):
        if (not isinstance(step_key, str) or not 1 <= len(step_key) <= 200
            or not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1
            or not isinstance(call_index, int) or isinstance(call_index, bool) or call_index < 1
            or not isinstance(request_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", request_hash)
            or not isinstance(call_id, str) or not re.fullmatch(r"call-[1-9][0-9]{0,8}", call_id)
            or not isinstance(route, str) or not re.fullmatch(r"[a-z_]{1,60}", route)
            or not isinstance(module_ids, list) or len(module_ids) > 100
            or any(not isinstance(mid, str) or not 1 <= len(mid) <= 100 for mid in module_ids)):
            raise AppError("LLM_INVOCATION_INVALID", "模型调用意图格式无效", 422)
        async with self.executor.app.state.db.session_factory() as session:
            await self.executor.lock_execution(session, self.run_id)
            row = await session.scalar(select(LLMInvocation).where(LLMInvocation.run_id == self.run_id,
                LLMInvocation.step_key == step_key, LLMInvocation.attempt == attempt).with_for_update())
            if row is not None:
                self._context(row)
                if row.request_hash != request_hash:
                    raise AppError("LLM_INVOCATION_CONTEXT_CHANGED", "模型调用请求与原意图不一致", 409)
                return _receipt(row)
            row = LLMInvocation(id=uid(), run_id=self.run_id,
                invocation_key=digest({"run": self.run_id, "step": step_key, "attempt": attempt}),
                step_key=step_key, attempt=attempt, context_fingerprint=self.fingerprint, request_hash=request_hash,
                call_id=call_id, call_index=call_index, module_ids=list(module_ids), route=route,
                status="in_flight", capture_epoch=self.lease.epoch, capture_owner=self.lease.owner,
                created_at=await database_now(session))
            session.add(row)
            await session.flush()
            await session.commit()
            return _receipt(row, claimed=True)

    async def _transport_row(self, session, invocation_id):
        # Keep Draft->Run->Invocation lock order consistent with business finalization.
        run, draft = await lock_run_with_draft(session, self.run_id)
        row = await session.scalar(select(LLMInvocation).where(LLMInvocation.id == invocation_id,
            LLMInvocation.run_id == self.run_id).with_for_update())
        if (run is None or row is None or (draft and draft.deleted_at)
                or row.capture_owner != self.lease.owner or row.capture_epoch != self.lease.epoch):
            raise LeaseLost()
        self._context(row)
        return row

    async def save_response(self, invocation_id, completion):
        if not isinstance(completion.content, str) or not completion.content.strip():
            raise AppError("LLM_RESPONSE_INVALID", "模型完整响应为空或格式无效", 502)
        encoded = completion.content.encode("utf-8")
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise AppError("LLM_RESPONSE_LIMIT", "模型响应超出回执保存上限", 502)
        content_hash = hashlib.sha256(encoded).hexdigest()
        async with self.executor.app.state.db.session_factory() as session:
            row = await self._transport_row(session, invocation_id)
            if row.response_hash is not None:
                if row.response_hash != content_hash:
                    raise AppError("LLM_RESPONSE_CONFLICT", "已保存模型响应与迟到回执不一致", 409)
                return _receipt(row)
            if row.status not in {"in_flight", "outcome_unknown"}:
                raise AppError("LLM_INVOCATION_CLOSED", "模型调用已关闭，不能覆盖回执", 409)
            row.response_body, row.response_hash = completion.content, content_hash
            row.response_log = safe_response_log(completion.log)
            row.status, row.error_code = "response_saved", None
            row.response_saved_at = await database_now(session)
            await session.commit()
            return _receipt(row)

    async def _mark(self, invocation_id, code, status):
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,80}", code):
            code = "LLM_OUTCOME_UNKNOWN" if status == "outcome_unknown" else "LLM_PROVIDER_FAILED"
        async with self.executor.app.state.db.session_factory() as session:
            row = await self._transport_row(session, invocation_id)
            if row.status in {"in_flight", "outcome_unknown"} and row.response_hash is None:
                row.status, row.error_code = status, code
                await session.commit()

    async def mark_unknown(self, invocation_id, error_code):
        await self._mark(invocation_id, error_code, "outcome_unknown")

    async def mark_failed(self, invocation_id, error_code):
        await self._mark(invocation_id, error_code, "failed")

    async def mark_applied(self, session):
        # The caller holds the current execution lock and owns the business COMMIT/ROLLBACK.
        # Do not refresh the ORM Run here: its pending event sequence belongs to this transaction.
        lease = self.lease
        conditions = (Run.id == self.run_id, Run.status == "running", Run.execution_epoch == lease.epoch,
            Run.lease_owner == lease.owner, Run.cancel_requested.is_(False))
        if not await lock_matching_run(session, *conditions):
            raise LeaseLost()
        if not await lock_matching_run(session, *conditions, Run.lease_expires_at > await database_now(session)):
            raise LeaseLost()
        await session.execute(update(LLMInvocation).where(LLMInvocation.run_id == self.run_id,
            LLMInvocation.context_fingerprint == self.fingerprint, LLMInvocation.status == "response_saved")
            .values(status="applied", applied_at=await database_now(session)).execution_options(synchronize_session=False))
