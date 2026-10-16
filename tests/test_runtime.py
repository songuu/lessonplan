import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.ai.assets import AssetBundle
from app.ai.gateway import Completion, GatewayError, LLMGateway
from app.ai.quality import ModuleQualityGate, SafePatchProcessor
from app.ai.runtime import LessonRuntime, RuntimeError, validate_teacher_content


def settings(**values):
    values.setdefault("ai_provider", "mock")
    return SimpleNamespace(**values)


def test_one_click_respects_custom_module_order_and_single_call():
    async def check():
        events = []

        async def sink(kind, data):
            events.append((kind, data))

        result = await LessonRuntime(settings()).generate(
            {"topic": "分数的意义", "subject": "数学", "school_stage": "小学"},
            [{"module_id": "custom-1", "module_type": "custom_module", "name": "操作任务"},
             {"module_id": "objectives", "module_type": "teaching_objectives", "name": "教学目标"}],
            event_sink=sink,
        )
        assert [m["module_id"] for m in result["modules"]] == ["custom-1", "objectives"]
        assert len(result["calls"]) == 1
        assert result["calls"][0]["llm_provider"] == "mock"
        assert all("演示" in m["teacher_view_content"] for m in result["modules"])
        assert all(data.get("module_id") for kind, data in events if kind.startswith("module_"))
        assert result["assets"]["bundle_hash"]
    asyncio.run(check())


def test_structured_reuses_blueprint_and_teacher_outline_exactly():
    async def check():
        runtime = LessonRuntime(settings())
        blueprint = {"topic": "分数", "version": 1, "objectives": ["理解单位1"]}
        outline = [{"stage_id": "teacher-a", "title": "折纸观察", "duration_min": 13},
                   {"stage_id": "teacher-z", "title": "解释迁移", "duration_min": 31}]
        events = []

        async def sink(kind, data):
            events.append((kind, data))

        result = await runtime.generate({"topic": "分数", "total_duration_min": 40},
                                        [{"module_id": "process", "module_type": "teaching_process"}],
                                        blueprint=blueprint, stage="activities", outline=outline,
                                        event_sink=sink)
        assert result["blueprint"] == blueprint
        assert len(result["calls"]) == 2
        content = result["modules"][0]["teacher_view_content"]
        assert content.startswith("### 折纸观察（13分钟）")
        assert "### 解释迁移（31分钟）" in content
        assert [data["stage_id"] for kind, data in events if kind == "stage_started"] == ["teacher-a", "teacher-z"]
        assert result["modules"][0]["canonical_content"]["outline"] == outline
        assert result["modules"][0]["metadata"]["stage"] == "activities"
    asyncio.run(check())


def test_first_structured_step_plans_once_and_outline_is_structured():
    async def check():
        result = await LessonRuntime(settings()).generate(
            {"topic": "长方形面积", "total_duration_min": 40},
            [{"module_id": "process", "module_type": "teaching_process"}], stage="outline")
        assert len(result["calls"]) == 2
        assert result["blueprint"]["topic"] == "长方形面积"
        assert result["modules"][0]["canonical_content"]["outline"]
    asyncio.run(check())


def test_gateway_fallback_records_primary_error_without_secret():
    async def check():
        def handler(request):
            if json.loads(request.content)["model"] == "primary":
                return httpx.Response(503, json={"error": {"message": "sensitive-provider-details"}})
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
                'data: {"choices":[{"delta":{"content":"有效正文"}}]}\n\n'
                'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6}}\n\n'
                'data: [DONE]\n\n').encode())

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gateway = LLMGateway(settings(ai_provider="openai", llm_model="primary",
                                      llm_fallback_model="backup", llm_api_key="secret-test-key",
                                      llm_base_url="https://provider.invalid/v1"), client=client)
        result = await gateway.complete([{"role": "user", "content": "test"}], route="one_click")
        assert result.content == "有效正文"
        assert result.log["fallback_used"] is True
        assert result.log["call_outcome"] == "success"
        assert result.log["primary_attempt_outcome"] == "failed"
        assert result.log["total_tokens"] == 6
        assert "secret" not in json.dumps(result.log)
        assert "sensitive-provider-details" not in json.dumps(result.log)
        await client.aclose()
    asyncio.run(check())


def test_empty_gateway_response_is_error_and_cancellation_propagates():
    async def check():
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, content=b"data: [DONE]\n\n", headers={"content-type": "text/event-stream"})))
        gateway = LLMGateway(settings(ai_provider="openai", llm_api_key="x",
                                      llm_base_url="https://provider.invalid/v1", llm_model="a"), client=client)
        with pytest.raises(GatewayError) as error:
            await gateway.complete([{"role": "user", "content": "a"}], route="structured")
        assert error.value.code == "provider_empty_response"
        assert error.value.calls[0]["call_outcome"] == "failed"
        await client.aclose()
    asyncio.run(check())


def test_safe_patch_quality_formula_and_textbook_boundaries():
    source = "<internal_style>secret</internal_style>\n第32页：讨论分数。音标 /ˈtiːtʃə/ \\(\\unknown{x}\\)"
    patched, patches = SafePatchProcessor().apply(source, {})
    assert "secret" not in patched and "第32页" not in patched
    assert "/ˈtiːtʃə/" in patched and "\\unknown" in patched
    quality = ModuleQualityGate().check(patched, {"topic": "分数"}, {"module_type": "textbook_analysis"})
    assert quality["passed"] is True
    assert quality["math_render_state"] == "invalid"
    assert patches
    unsafe = ModuleQualityGate().check("数学 \\(\\input{/etc/passwd}\\)", {}, {})
    assert unsafe["passed"] is False
    assert any(x["code"] == "unsafe_formula" for x in unsafe["findings"])


def test_assets_progressive_subject_routing_and_integrity():
    bundle = AssetBundle()
    math = bundle.prompt("one_click", {"subject": "数学", "school_stage": "高中", "topic": "函数"},
                         [{"module_type": "teaching_objectives"}])
    assert "数学" in math and "函数" in math
    assert "IPA" not in math
    assert bundle.validate() == []


def test_temporary_modules_finish_before_quality_and_one_click_reuses_no_planner():
    async def check():
        events = []

        async def sink(kind, data):
            events.append((kind, data))

        result = await LessonRuntime(settings()).generate(
            {"topic": "面积", "generation_mode": "one_click"},
            [{"module_id": "a", "module_type": "teaching_objectives"},
             {"module_id": "b", "module_type": "teaching_methods"}],
            blueprint={"topic": "旧蓝图", "objectives": []}, event_sink=sink)
        assert len(result["calls"]) == 1
        quality_index = next(i for i, (kind, _) in enumerate(events) if kind == "module_progress")
        temporary_done = [(i, data) for i, (kind, data) in enumerate(events)
                          if kind == "module_completed" and data["is_temporary"]]
        assert [data["module_id"] for _, data in temporary_done] == ["a", "b"]
        assert all(i < quality_index for i, _ in temporary_done)
    asyncio.run(check())


def test_parse_fallback_uses_json_contract_and_dependency_order():
    class FakeGateway:
        def __init__(self):
            self.responses = iter(["invalid boundary", "invalid JSON", "目标：解释面积。", "板书：面积与单位。"])
            self.requests = []

        async def complete(self, messages, **kwargs):
            self.requests.append(messages)
            text = next(self.responses)
            if kwargs.get("on_delta"):
                await kwargs["on_delta"](text)
            return Completion(text, {"component_name": kwargs["route"], "call_outcome": "success"})

    async def check():
        gateway = FakeGateway()
        result = await LessonRuntime(settings(), gateway=gateway).generate(
            {"topic": "面积"}, [{"module_id": "board", "module_type": "blackboard_design"},
                              {"module_id": "goals", "module_type": "teaching_objectives"}])
        assert len(result["calls"]) == 4
        assert "JSON" in gateway.requests[1][0]["content"]
        assert "<!-- MODULE" not in gateway.requests[1][0]["content"]
        assert json.loads(gateway.requests[2][1]["content"])["module"]["module_id"] == "goals"
        assert result["modules"][0]["teacher_view_content"] == "板书：面积与单位。"
    asyncio.run(check())


def test_gateway_timeout_partial_stream_and_cancel_are_not_false_success():
    async def check():
        async def timeout_handler(request):
            raise httpx.ReadTimeout("sensitive network error", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler))
        gateway = LLMGateway(settings(ai_provider="openai", llm_model="model", llm_api_key="key",
                                      llm_base_url="https://provider.invalid/v1"), client=client)
        with pytest.raises(GatewayError) as error:
            await gateway.complete([], route="review")
        assert error.value.code == "provider_timeout"
        assert "sensitive" not in str(error.value)
        await client.aclose()

        async def cancel_handler(request):
            raise asyncio.CancelledError()

        client = httpx.AsyncClient(transport=httpx.MockTransport(cancel_handler))
        gateway.client = client
        with pytest.raises(asyncio.CancelledError):
            await gateway.complete([], route="review")
        await client.aclose()

        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content='data: {"choices":[{"delta":{"content":"不完整"}}]}\n\n'.encode())))
        gateway.client = client
        with pytest.raises(GatewayError) as error:
            await gateway.complete([], route="review")
        assert error.value.code == "provider_incomplete_stream"
        await client.aclose()
    asyncio.run(check())


def test_quality_edit_preserves_comparison_formulas_and_blocks_wrong_topic():
    content = r"比较 \(x<y>z\)，解释而非改变结论。"
    assert validate_teacher_content(content, {}, {})["teacher_view_content"] == content
    with pytest.raises(RuntimeError) as error:
        validate_teacher_content("本课课题：《圆周长》\n学习周长。", {"topic": "分数的意义"}, {})
    assert error.value.code == "module_quality_failed"


def test_polish_selection_and_review_do_not_mutate_input():
    async def check():
        source = "学生解释理由。"
        runtime = LessonRuntime(settings())
        candidate = await runtime.polish({"topic": "面积"}, source, "更明确", scope="selection")
        assert candidate["scope"] == "selection"
        assert "演示" in candidate["content"]
        assert source == "学生解释理由。"
        review = await runtime.review({"topic": "面积"}, [{"module_id": "a", "content": source}])
        assert review["findings"][0]["severity"] == "info"
        assert len(review["calls"]) == 1
    asyncio.run(check())

