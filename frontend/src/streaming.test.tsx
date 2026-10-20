import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api, watchRun } from "./api";
import { LessonPage } from "./Lesson";
import type { Draft } from "./types";

vi.mock("./api", () => ({
  api: vi.fn(),
  watchRun: vi.fn(),
  patch: vi.fn(),
  post: vi.fn(),
  track: vi.fn(),
  download: vi.fn(),
}));
let host: HTMLDivElement;
let root: Root;
let draft: Draft;
let event: Parameters<typeof watchRun>[2];
let state: Parameters<typeof watchRun>[3];

beforeEach(() => {
  (
    globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
  ).IS_REACT_ACT_ENVIRONMENT = true;
  sessionStorage.clear();
  vi.resetAllMocks();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  draft = {
    id: "d1",
    topic: "分数",
    school_stage: "primary",
    grade: "grade_3",
    subject: "math",
    lesson_type: "new_lesson",
    total_duration_min: 40,
    generation_mode: "one_click",
    status: "creating",
    content_version: 0,
    module_config_version_id: "cfg",
    active_run_id: "r1",
    modules: ["教材分析", "教学过程"].map((name, i) => ({
      module_id: `m${i + 1}`,
      module_type: i ? "teaching_process" : "textbook_analysis",
      display_name: name,
      enabled: true,
      module_summary: "",
      is_custom: false,
      current_version_id: null,
      teacher_view_content: "",
      state: { generation_status: "pending" },
    })),
  };
  vi.mocked(api).mockImplementation(async () => structuredClone(draft));
  vi.mocked(watchRun).mockImplementation(
    async (_id, _signal, onEvent, onState) => {
      event = onEvent;
      state = onState;
      onState({ id: "r1", status: "running" });
    },
  );
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
});
async function mount() {
  await act(async () =>
    root.render(
      <MemoryRouter initialEntries={["/lessons/d1?run=r1"]}>
        <Routes>
          <Route path="/lessons/:id" element={<LessonPage />} />
        </Routes>
      </MemoryRouter>,
    ),
  );
}
async function emit(kind: string, data: Record<string, unknown>) {
  await act(async () => event({ id: "1", event: kind, data }));
}
async function click(text: string) {
  await act(async () => {
    const target = [...host.querySelectorAll<HTMLButtonElement>("button")].find(
      (b) => b.textContent?.includes(text),
    );
    expect(target, `找不到按钮${text}`).toBeTruthy();
    target!.click();
  });
}

it.each(["one_click", "interactive"])(
  "%s explains response replay without marking an unvalidated module complete",
  async (mode) => {
    draft.generation_mode = mode;
    await mount();
    await emit("module_progress", {
      module_id: "m1",
      phase: "llm_response_replayed",
    });
    const banner = host.querySelector(".generation-banner")?.textContent;
    expect(banner).toContain("已复用保存的模型响应，继续校验与保存");
    expect(banner).not.toContain("已生成 1/2 个模块");
    expect(host.querySelector(".rich-editor")).toBeNull();
  },
);

it("describes first dispatch without claiming that it was a retry", async () => {
  await mount();
  await emit("module_progress", { phase: "dispatch_published" });
  const banner = host.querySelector(".generation-banner")?.textContent;
  expect(banner).toContain("任务已投递，正在等待开始生成");
  expect(banner).not.toContain("重新投递");
});

it.each(["one_click", "interactive"])(
  "%s follows the generating module and renders deltas before completion",
  async (mode) => {
    draft.generation_mode = mode;
    await mount();
    await emit("module_started", { module_id: "m1", is_temporary: true });
    expect(host.querySelector(".generation-banner")?.textContent).toContain(
      "教材分析",
    );
    await emit("content_delta", {
      module_id: "m1",
      delta: "学生通过折纸",
      is_temporary: true,
    });
    expect(host.querySelector(".stream-content")?.textContent).toContain(
      "学生通过折纸",
    );
    await emit("content_delta", {
      module_id: "m1",
      delta: "理解平均分。",
      is_temporary: true,
    });
    expect(host.querySelector(".stream-content")?.textContent).toContain(
      "学生通过折纸理解平均分。",
    );
    await emit("module_done", { module_id: "m1", is_temporary: true });
    expect(host.querySelector(".generation-banner")?.textContent).toContain(
      "已生成 1/2 个模块",
    );
    await emit("module_started", {
      module_id: "m2",
      stage: "outline",
      is_temporary: true,
    });
    await emit("content_delta", {
      module_id: "m2",
      delta: "### 折纸观察（10分钟）\n解释平均分",
      is_temporary: true,
    });
    expect(host.querySelector(".module-editor-heading h2")?.textContent).toBe(
      "教学过程",
    );
    expect(host.querySelector(".stream-content")?.textContent).toContain(
      "折纸观察（10分钟）",
    );
    await emit("stage_started", {
      module_id: "m2",
      title: "折纸观察",
      stage_index: 1,
      stage_count: 3,
    });
    expect(host.querySelector(".generation-banner")?.textContent).toContain(
      "当前环节：折纸观察（1/3）",
    );
  },
);

it("lets teachers inspect earlier modules and resume following the live module", async () => {
  await mount();
  await emit("module_started", { module_id: "m1" });
  await emit("content_delta", { module_id: "m1", delta: "已收到的教材分析" });
  await emit("module_started", { module_id: "m2" });
  await emit("content_delta", {
    module_id: "m2",
    delta: "当前正在生成的教学过程",
  });
  await click("教材分析");
  await emit("module_progress", {
    module_id: "m2",
    phase: "quality_check_and_repair_started",
  });
  expect(host.querySelector(".module-editor-heading h2")?.textContent).toBe(
    "教材分析",
  );
  expect(host.querySelector(".stream-content")?.textContent).toContain(
    "已收到的教材分析",
  );
  await click("跟随当前模块");
  expect(host.querySelector(".module-editor-heading h2")?.textContent).toBe(
    "教学过程",
  );
  expect(host.querySelector(".stream-content")?.textContent).toContain(
    "当前正在生成的教学过程",
  );
});

it("discards abandoned attempt previews when the backend restarts generation", async () => {
  await mount();
  await emit("module_started", { module_id: "m1" });
  await emit("content_delta", { module_id: "m1", delta: "废弃的尝试" });
  await emit("module_progress", {
    phase: "generation_attempt_reset",
    reason: "provider_fallback",
  });
  expect(
    host.querySelector(".stream-content")?.textContent || "",
  ).not.toContain("废弃的尝试");
  await emit("module_started", { module_id: "m1" });
  await emit("content_delta", { module_id: "m1", delta: "备用模型的新正文" });
  expect(host.querySelector(".stream-content")?.textContent).toContain(
    "备用模型的新正文",
  );
  expect(host.querySelector(".stream-content")?.textContent).not.toContain(
    "废弃的尝试",
  );
});

it("keeps the streamed final text visible until the formal version finishes loading", async () => {
  await mount();
  await emit("module_started", { module_id: "m1" });
  await emit("content_delta", {
    module_id: "m1",
    delta: "流中的最终正文",
    replace_previous: true,
    is_temporary: false,
  });
  let resolve!: (value: Draft) => void;
  vi.mocked(api).mockImplementation(
    () =>
      new Promise<Draft>((r) => {
        resolve = r;
      }),
  );
  await act(async () => state({ id: "r1", status: "completed" }));
  expect(host.querySelector(".stream-content")?.textContent).toContain(
    "流中的最终正文",
  );
  const final = structuredClone(draft);
  final.status = "ready";
  final.modules[0].current_version_id = "v1";
  final.modules[0].teacher_view_content = "正式版本正文";
  await act(async () => resolve(final));
  expect(host.querySelector(".stream-content")).toBeNull();
  expect(host.querySelector(".rich-editor")?.textContent).toContain(
    "正式版本正文",
  );
});

it("hands off to the formal editor after a failed final readback is retried", async () => {
  await mount();
  await emit("module_started", { module_id: "m1" });
  await emit("content_delta", { module_id: "m1", delta: "暂时保留的流正文" });
  vi.mocked(api).mockRejectedValueOnce(new Error("临时读取失败"));
  await act(async () => state({ id: "r1", status: "completed" }));
  expect(host.querySelector(".stream-content")?.textContent).toContain(
    "暂时保留的流正文",
  );
  const final = structuredClone(draft);
  final.modules[0].current_version_id = "v1";
  final.modules[0].teacher_view_content = "重新读取的正式正文";
  vi.mocked(api).mockResolvedValue(final);
  await click("重新读取");
  expect(host.querySelector(".stream-content")).toBeNull();
  expect(host.querySelector(".rich-editor")?.textContent).toContain(
    "重新读取的正式正文",
  );
});
