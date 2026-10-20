import { act, StrictMode } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api, post, watchRun } from "./api";
import { LessonPage } from "./Lesson";
import type { Draft, Run } from "./types";

vi.mock("./api", () => ({
  api: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  watchRun: vi.fn(),
  track: vi.fn(),
  download: vi.fn(),
}));
vi.mock("./Editor", () => ({
  RichEditor: ({ value, readonly }: { value: string; readonly: boolean }) => (
    <div className="rich-editor" data-readonly={String(readonly)}>
      {value}
    </div>
  ),
}));

let host: HTMLDivElement, root: Root, draft: Draft;
let emitEvent: Parameters<typeof watchRun>[2];
let emitState: Parameters<typeof watchRun>[3];
const nav = (name: string) =>
  Array.from(
    host.querySelectorAll<HTMLButtonElement>(".module-sidebar nav button"),
  ).find((button) => button.textContent?.includes(name))!;
const action = (name: string) =>
  Array.from(host.querySelectorAll<HTMLButtonElement>("button")).find(
    (button) => button.textContent?.trim() === name,
  )!;
const heading = () =>
  host.querySelector(".module-editor-heading h2")?.textContent;
async function mount(query = "", strict = false) {
  await act(async () => {
    const page = (
      <MemoryRouter initialEntries={[`/lessons/d1${query}`]}>
        <Routes>
          <Route path="/lessons/:id" element={<LessonPage />} />
        </Routes>
      </MemoryRouter>
    );
    root.render(strict ? <StrictMode>{page}</StrictMode> : page);
  });
}
async function emit(event: string, data: Record<string, unknown>) {
  await act(async () => emitEvent({ id: "1", event, data }));
}
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
    generation_mode: "interactive_step",
    status: "interactive_working",
    content_version: 1,
    module_config_version_id: "cfg",
    modules: ["教材分析", "教学目标", "教学重点"].map((display_name, i) => ({
      module_id: `m${i + 1}`,
      module_type: [
        "textbook_analysis",
        "teaching_objectives",
        "teaching_key_points",
      ][i],
      display_name,
      enabled: true,
      module_summary: "",
      is_custom: false,
      current_version_id: i === 0 ? "v1" : null,
      teacher_view_content: i === 0 ? "已经完整生成的教材分析" : "",
      state: {
        generation_status: i === 0 ? "ready" : "pending",
        confirmation_status: "unconfirmed",
      },
    })),
  };
  vi.mocked(api).mockImplementation(async () => structuredClone(draft));
  vi.mocked(watchRun).mockImplementation(
    async (id, _signal, onEvent, onState) => {
      emitEvent = onEvent;
      emitState = onState;
      const stepBody = vi
        .mocked(post)
        .mock.calls.filter(([path]) => path.endsWith("/interactive/step"))
        .at(-1)?.[1] as { module_id: string } | undefined;
      onState({
        id,
        status: id === "old-run" ? "completed" : "running",
        ...(stepBody
          ? { workflow_type: "interactive_step", payload: stepBody }
          : {}),
      });
    },
  );
  vi.mocked(post).mockImplementation(async (path) => {
    if (path.endsWith("/interactive/confirm")) {
      draft.modules[0].state.confirmation_status = "confirmed";
      draft.content_version++;
      return {};
    }
    return { run_id: "new-run" };
  });
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
});

it("keeps later modules locked until the current module is confirmed", async () => {
  await mount();
  expect(nav("教学目标").disabled).toBe(true);
  expect(nav("教学重点").disabled).toBe(true);
  await act(async () => nav("教学重点").click());
  expect(heading()).toBe("教材分析");
  expect(post).not.toHaveBeenCalled();
  await act(async () => action("确认，继续下一步").click());
  expect(heading()).toBe("教学目标");
  expect(post).toHaveBeenNthCalledWith(
    2,
    "/lesson-drafts/d1/interactive/step",
    {
      module_id: "m2",
      expected_content_version: 2,
      expected_current_module_version_id: null,
      expected_module_config_version_id: "cfg",
    },
  );
  expect(nav("教学目标").disabled).toBe(false);
  expect(nav("教学重点").disabled).toBe(true);
  await act(async () => nav("教材分析").click());
  expect(heading()).toBe("教材分析");
  await act(async () => nav("教学目标").click());
  expect(heading()).toBe("教学目标");
});

it("continues from a revisited confirmed module without confirming it again", async () => {
  for (const [i, item] of draft.modules.slice(0, 2).entries()) {
    item.current_version_id = `v${i + 1}`;
    item.teacher_view_content = `${item.display_name}已生成`;
    item.state = {
      generation_status: "ready",
      confirmation_status: "confirmed",
    };
  }
  await mount();
  await act(async () => nav("教学目标").click());
  expect(heading()).toBe("教学目标");
  expect(action("继续下一步").disabled).toBe(false);
  await act(async () => action("继续下一步").click());
  expect(heading()).toBe("教学重点");
  expect(post).toHaveBeenCalledTimes(1);
  expect(post).toHaveBeenCalledWith(
    "/lesson-drafts/d1/interactive/step",
    expect.objectContaining({ module_id: "m3" }),
  );
  expect(draft.content_version).toBe(1);
});

it("automatically generates an empty unlocked module entered through the directory", async () => {
  draft.modules[0].state.confirmation_status = "confirmed";
  await mount();
  expect(post).not.toHaveBeenCalled();
  await act(async () => nav("教学目标").click());
  expect(post).toHaveBeenCalledTimes(1);
  expect(post).toHaveBeenCalledWith(
    "/lesson-drafts/d1/interactive/step",
    expect.objectContaining({ module_id: "m2" }),
  );
  await act(async () => nav("教材分析").click());
  expect(post).toHaveBeenCalledTimes(1);
  expect(action("重新生成").disabled).toBe(true);
});

it("admits one automatic first-module request under StrictMode", async () => {
  draft.modules[0].current_version_id = null;
  draft.modules[0].teacher_view_content = "";
  draft.modules[0].state.generation_status = "pending";
  await mount("", true);
  expect(post).toHaveBeenCalledTimes(1);
  expect(post).toHaveBeenCalledWith(
    "/lesson-drafts/d1/interactive/step",
    expect.objectContaining({ module_id: "m1" }),
  );
});

it("automatically starts only the outline when entering a new teaching-process module", async () => {
  draft.modules[0].state.confirmation_status = "confirmed";
  draft.modules[1].module_type = "teaching_process";
  await mount();
  await act(async () => nav("教学目标").click());
  expect(post).toHaveBeenCalledTimes(1);
  expect(post).toHaveBeenCalledWith(
    "/lesson-drafts/d1/interactive/step",
    expect.objectContaining({ module_id: "m2", stage: "outline" }),
  );
});

it("keeps teaching activities behind the explicit outline confirmation step", async () => {
  draft.modules[0].state.confirmation_status = "confirmed";
  draft.modules[1].module_type = "teaching_process";
  draft.modules[1].current_version_id = "outline-version";
  draft.modules[1].state = {
    generation_status: "ready",
    workflow_stage: "outline",
    outline_confirmed: true,
    outline: [{ stage_id: "s1", title: "导入", duration_min: 40 }],
  };
  await mount();
  await act(async () => nav("教学目标").click());
  expect(post).not.toHaveBeenCalled();
  expect(action("生成课堂活动").disabled).toBe(false);
});

it.each([true, false])(
  "waits for authoritative terminal readback with active_run_id present=%s",
  async (activePointer) => {
    draft.modules[0].current_version_id = null;
    draft.modules[0].teacher_view_content = "";
    draft.modules[0].state.generation_status = "pending";
    if (activePointer) draft.active_run_id = "current-run";
    await mount("?run=current-run");
    let readback!: (value: Draft) => void;
    vi.mocked(api).mockImplementation(
      () =>
        new Promise<Draft>((resolve) => {
          readback = resolve;
        }),
    );
    await act(async () =>
      emitState({ id: "current-run", status: "completed" } as Run),
    );
    expect(post).not.toHaveBeenCalled();
    draft.active_run_id = undefined;
    draft.modules[0].current_version_id = "v1";
    draft.modules[0].teacher_view_content = "刚刚生成的教材分析";
    draft.modules[0].state.generation_status = "ready";
    await act(async () => readback(structuredClone(draft)));
    expect(post).not.toHaveBeenCalled();
  },
);

it("does not automatically retry a failed server task whose module still appears pending", async () => {
  draft.modules[0].current_version_id = null;
  draft.modules[0].teacher_view_content = "";
  draft.modules[0].state.generation_status = "pending";
  draft.latest_run_id = "failed-run";
  vi.mocked(watchRun).mockImplementation(
    async (id, _signal, _onEvent, onState) => {
      onState({ id, status: "failed", payload: { module_id: "m1" } });
    },
  );
  await mount();
  expect(post).not.toHaveBeenCalled();
});

it.each(
  ["failed", "failed_retryable", "cancelled"].flatMap((status) =>
    ["", "?run=old-run"].map((query) => ({ status, query })),
  ),
)(
  "does not restart a $status draft opened from history with query '$query'",
  async ({ status, query }) => {
    draft.status = status;
    draft.modules[0].current_version_id = null;
    draft.modules[0].teacher_view_content = "";
    draft.modules[0].state.generation_status = "pending";
    await mount(query);
    expect(post).not.toHaveBeenCalled();
  },
);

it("offers an explicit retry instead of looping after automatic startup fails", async () => {
  draft.modules[0].state.confirmation_status = "confirmed";
  vi.mocked(post).mockRejectedValue(new Error("连接中断"));
  await mount();
  await act(async () => nav("教学目标").click());
  expect(post).toHaveBeenCalledTimes(1);
  expect(action("重试生成").disabled).toBe(false);
  await act(async () => nav("教材分析").click());
  await act(async () => nav("教学目标").click());
  expect(post).toHaveBeenCalledTimes(1);
  vi.mocked(post).mockResolvedValue({ run_id: "new-run" });
  await act(async () => action("重试生成").click());
  expect(post).toHaveBeenCalledTimes(2);
});

it.each(["failed", "failed_retryable", "cancelled"])(
  "does not automatically rerun a module with generation status %s",
  async (status) => {
    draft.modules[0].state.confirmation_status = "confirmed";
    draft.modules[1].state.generation_status = status;
    await mount();
    await act(async () => nav("教学目标").click());
    expect(post).not.toHaveBeenCalled();
  },
);

it("shows the confirmed final module without a misleading next-step action", async () => {
  for (const [i, item] of draft.modules.entries()) {
    item.current_version_id = `v${i + 1}`;
    item.teacher_view_content = `${item.display_name}已生成`;
    item.state = {
      generation_status: "ready",
      confirmation_status: "confirmed",
    };
  }
  await mount();
  await act(async () => nav("教学重点").click());
  expect(action("当前模块已确认").disabled).toBe(true);
  expect(action("继续下一步")).toBeUndefined();
  expect(post).not.toHaveBeenCalled();
});

it("allows read-only lookback during generation and prevents a second AI action", async () => {
  draft.modules[0].state.confirmation_status = "confirmed";
  draft.active_run_id = "current-run";
  await mount();
  await emit("module_started", { module_id: "m2" });
  await emit("content_delta", { module_id: "m2", delta: "目标正在生成" });
  await act(async () => nav("教材分析").click());
  expect(heading()).toBe("教材分析");
  expect(host.querySelector(".rich-editor")?.textContent).toContain(
    "已经完整生成",
  );
  expect(
    host.querySelector(".rich-editor")?.getAttribute("data-readonly"),
  ).toBe("true");
  expect(action("重新生成").disabled).toBe(true);
  expect(action("AI 润色").disabled).toBe(true);
  expect(action("继续下一步").disabled).toBe(true);
  expect(nav("教学重点").disabled).toBe(true);
  await act(async () => {
    action("重新生成").click();
    action("AI 润色").click();
  });
  expect(post).not.toHaveBeenCalled();
  await emit("module_progress", {
    module_id: "m2",
    phase: "quality_check_and_repair_started",
  });
  expect(heading()).toBe("教材分析");
  await act(async () => action("跟随当前模块").click());
  expect(heading()).toBe("教学目标");
});

it("tracks the actual active task when an old completed run remains in the URL", async () => {
  draft.modules[0].state.confirmation_status = "confirmed";
  draft.active_run_id = "current-run";
  await mount("?run=old-run");
  expect(
    vi.mocked(watchRun).mock.calls.every(([id]) => id === "current-run"),
  ).toBe(true);
  expect(action("重新生成").disabled).toBe(true);
  expect(action("AI 润色").disabled).toBe(true);
});

it("admits one regeneration even if the button receives two clicks before rendering", async () => {
  vi.mocked(post).mockImplementation(() => new Promise(() => {}));
  await mount();
  await act(async () => {
    action("重新生成").click();
    action("重新生成").click();
  });
  expect(post).toHaveBeenCalledTimes(1);
});

it("admits one polish session before rendering disables the action", async () => {
  vi.mocked(post).mockImplementation(() => new Promise(() => {}));
  await mount();
  await act(async () => {
    action("AI 润色").click();
    action("AI 润色").click();
  });
  expect(post).toHaveBeenCalledTimes(1);
});

it("opens the active module from task metadata before stream events arrive", async () => {
  draft.modules[0].state.confirmation_status = "confirmed";
  draft.active_run_id = "current-run";
  vi.mocked(watchRun).mockImplementation(
    async (id, _signal, onEvent, onState) => {
      emitEvent = onEvent;
      emitState = onState;
      onState({
        id,
        status: "queued",
        workflow_type: "interactive_step",
        payload: { module_id: "m2" },
      });
    },
  );
  await mount("?run=old-run");
  expect(heading()).toBe("教学目标");
  expect(nav("教材分析").disabled).toBe(false);
  expect(nav("教学目标").disabled).toBe(false);
  expect(nav("教学重点").disabled).toBe(true);
});

it("does not turn a lost task connection into permission for another generation", async () => {
  draft.modules[0].state.confirmation_status = "confirmed";
  draft.active_run_id = "current-run";
  vi.mocked(watchRun).mockRejectedValue(new Error("连接中断"));
  await mount();
  expect(action("重新生成").disabled).toBe(true);
  expect(action("AI 润色").disabled).toBe(true);
  draft.active_run_id = undefined;
  vi.mocked(watchRun).mockImplementation(
    async (id, _signal, _onEvent, onState) => {
      onState({ id, status: "completed" });
    },
  );
  await act(async () => action("重新读取").click());
  expect(action("重新生成").disabled).toBe(false);
  expect(action("AI 润色").disabled).toBe(false);
});

it("unlocks AI actions after the tracked task reaches a real terminal state", async () => {
  draft.modules[0].state.confirmation_status = "confirmed";
  draft.active_run_id = "current-run";
  await mount();
  draft.active_run_id = undefined;
  await act(async () =>
    emitState({ id: "current-run", status: "completed" } as Run),
  );
  expect(action("重新生成").disabled).toBe(false);
  expect(action("AI 润色").disabled).toBe(false);
});

it("preserves read-only history when an earlier module needs confirmation again", async () => {
  draft.modules[2].enabled = false;
  draft.modules[1].current_version_id = "v2";
  draft.modules[1].teacher_view_content = "此前完整生成的教学目标";
  draft.modules[1].state = {
    generation_status: "ready",
    confirmation_status: "confirmed",
  };
  await mount();
  expect(nav("教学目标").disabled).toBe(false);
  await act(async () => nav("教学目标").click());
  expect(heading()).toBe("教学目标");
  expect(
    host.querySelector(".rich-editor")?.getAttribute("data-readonly"),
  ).toBe("true");
  expect(action("重新生成").disabled).toBe(true);
  expect(action("AI 润色").disabled).toBe(true);
  expect(host.textContent).toContain("请先确认前面的模块");
  expect(nav("教学重点")).toBeUndefined();
  await act(async () => nav("教材分析").click());
  await act(async () => action("确认，继续下一步").click());
  await act(async () => nav("教学目标").click());
  expect(action("重新生成").disabled).toBe(false);
  expect(action("AI 润色").disabled).toBe(false);
});

it("retains a restored polish candidate without adopting it into a read-only module", async () => {
  draft.modules[1].current_version_id = "v2";
  draft.modules[1].teacher_view_content = "此前完整生成的教学目标";
  draft.modules[1].state = {
    generation_status: "ready",
    confirmation_status: "confirmed",
  };
  sessionStorage.setItem(
    "lesson-polish:d1",
    JSON.stringify({
      id: "polish-history",
      moduleId: "m2",
      base: draft.modules[1].teacher_view_content,
      selection: null,
      candidate: "待采用的润色候选",
      instruction: "精简",
    }),
  );
  await mount();
  expect(heading()).toBe("教学目标");
  expect(action("采用到编辑器").disabled).toBe(true);
  await act(async () => action("采用到编辑器").click());
  expect(host.querySelector(".rich-editor")?.textContent).toBe(
    "此前完整生成的教学目标",
  );
  expect(host.querySelector(".polish-panel")?.textContent).toContain(
    "待采用的润色候选",
  );
  expect(post).not.toHaveBeenCalled();
});
