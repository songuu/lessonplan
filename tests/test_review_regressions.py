from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.ai.assets import AssetBundle
from app.ai.gateway import Completion, GatewayError, LLMGateway
from app.ai.quality import ModuleQualityGate
from app.ai.runtime import LessonRuntime
from app.db.models import Outbox
from app.integrations.documents import DocumentAdapter
from tests.conftest import login
from tests.test_workflows import create, wait_run


def test_dictionary_codes_select_subject_assets_and_explicit_quality_boundaries():
    assets = AssetBundle()
    assets.prompt("structured", {"subject": "math", "school_stage": "primary", "lesson_type": "review_lesson"}, [])
    assert "prompts/subject_guides/math.md" in assets.consumed
    gate = ModuleQualityGate()
    inputs = {"topic": "形容词", "subject": "english", "school_stage": "primary", "lesson_type": "new_lesson"}
    result = gate.check("适用学科：数学；课型：复习课。", inputs, {})
    assert not result["passed"]
    assert {f["code"] for f in result["findings"]} >= {"subject_mismatch", "lesson_type_mismatch"}
    assert gate.check("本课面向：小学。适用学科：英语；课型：新授课。", inputs, {})["passed"]


def test_uploaded_metadata_has_grade_and_lesson_type_without_fabricating():
    result = DocumentAdapter().parse_upload("lesson.txt", "小学三年级 数学 复习课\n分数的复习。".encode())
    assert result["metadata"]["grade"] == "grade_3"
    assert result["metadata"]["subject"] == "math"
    assert result["metadata"]["lesson_type"] == "review_lesson"
    unclear = DocumentAdapter().parse_upload("unclear.txt", "这是一份没有年级学科证据的教案。".encode())
    assert unclear["metadata"]["grade"] is None and unclear["metadata"]["subject"] is None
    for label, value in [("高中一年级", "grade_10"), ("高中二年级", "grade_11"), ("高中三年级", "grade_12")]:
        high = DocumentAdapter().parse_upload("high.txt", f"{label} 数学 新授课".encode())
        assert high["metadata"]["grade"] == value


@pytest.mark.asyncio
async def test_json_provider_truncation_is_failure():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={
        "choices": [{"finish_reason": "length", "message": {"content": "尚未完成的正文"}}]}))) as client:
        gateway = LLMGateway(SimpleNamespace(ai_provider="openai", llm_model="primary", llm_api_key="test",
            llm_base_url="https://provider.invalid/v1"), client=client)
        with pytest.raises(GatewayError) as error:
            await gateway.complete([], route="module_operation")
        assert error.value.code == "provider_truncated"


@pytest.mark.asyncio
async def test_blueprint_uses_structured_model_when_no_generic_model():
    class Gateway:
        async def complete(self, messages, *, route, mock_content, **kwargs):
            assert route == "structured"
            return Completion(mock_content, {"component_name": route})
    result = await LessonRuntime(SimpleNamespace(ai_provider="mock"), gateway=Gateway()).generate(
        {"topic": "分数", "generation_mode": "interactive_step"},
        [{"module_id": "objectives", "module_type": "teaching_objectives"}], dependencies={})
    assert result["blueprint"]["topic"] == "分数"


@pytest.mark.asyncio
async def test_reconcile_recovers_published_but_unexecuted_inline_job(client):
    headers = await login(client)
    dispatcher = client.app.state.dispatcher
    original = dispatcher.submit
    async def frozen(run_id):
        async with client.app.state.db.session_factory() as session:
            intent = await session.scalar(select(Outbox).where(Outbox.run_id == run_id))
            intent.status = "published"
            await session.commit()
    dispatcher.submit = frozen
    item = await create(client, headers)
    dispatcher.submit = original
    await dispatcher.reconcile()
    assert (await wait_run(client, headers, item["run_id"]))["status"] == "completed"
