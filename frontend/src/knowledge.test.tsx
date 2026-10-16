import { act } from "react";
import type { ReactNode } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, patch, post, watchRun } from "./api";
import { KnowledgePage } from "./Knowledge";
import { NewLesson } from "./Pages";
import { LessonPage } from "./Lesson";
import type { Draft, StreamEvent } from "./types";
vi.mock("./api", () => ({
  api: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  track: vi.fn(),
  download: vi.fn(),
  privateBlob: vi.fn(),
  watchRun: vi.fn().mockResolvedValue(undefined),
}));
let host: HTMLDivElement, root: Root;
const bases = [
  { id: "b1", title: "课程资料", description: "教学参考", documents_count: 1 },
  { id: "b2", title: "实验材料", description: "", documents_count: 0 },
];
const document = {
  id: "doc1",
  base_id: "b1",
  filename: "课程标准.pdf",
  status: "ready",
  chunk_count: 11,
};
const chunk = {
  id: "c1",
  document_id: "doc1",
  base_id: "b1",
  filename: "课程标准.pdf",
  base_name: "课程资料",
  ordinal: 0,
  text: "课程目标应以可观察的学习表现表述。",
  content_hash: "hash1",
  score: 1.2,
};
const button = (name: string) =>
  Array.from(host.querySelectorAll<HTMLButtonElement>("button")).find(
    (b) =>
      b.textContent?.trim() === name || b.getAttribute("aria-label") === name,
  )!;
async function click(name: string) {
  await act(async () => button(name).click());
}
async function mount(node: ReactNode) {
  await act(async () => root.render(<MemoryRouter>{node}</MemoryRouter>));
}
async function fill(selector: string, value: string) {
  await act(async () => {
    const input = host.querySelector<HTMLInputElement | HTMLTextAreaElement>(
      selector,
    )!;
    const proto =
      input instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
beforeEach(() => {
  (
    globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
  ).IS_REACT_ACT_ENVIRONMENT = true;
  sessionStorage.clear();
  vi.clearAllMocks();
  host = window.document.createElement("div");
  window.document.body.append(host);
  root = createRoot(host);
  vi.mocked(api).mockImplementation(async (path) => {
    if (path === "/knowledge-bases") return bases;
    if (path === "/knowledge-bases/b1/documents") return [document];
    if (path.endsWith("/documents")) return [];
    if (path === "/knowledge-documents/doc1") return document;
    if (path.includes("/chunks")) return { items: [chunk], total: 11 };
    if (path === "/lesson-dictionaries") return {};
    if (path === "/me/lesson-module-config")
      return {
        modules: [
          {
            module_type: "teaching_objectives",
            display_name: "教学目标",
            enabled: true,
            is_custom: false,
            module_summary: "",
          },
        ],
      };
    return [];
  });
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.useRealTimers();
});
describe("independent private knowledge workspace", () => {
  it("creates, edits and deletes a distinct library without touching the personal lesson store", async () => {
    let records = [...bases];
    vi.mocked(api).mockImplementation(async (path, options) => {
      if (path === "/knowledge-bases") return records;
      if (options?.method === "DELETE" && path === "/knowledge-bases/b3") {
        records = records.filter((item) => item.id !== "b3");
        return { deleted: true };
      }
      return [];
    });
    vi.mocked(post).mockImplementation(async () => {
      const item = {
        id: "b3",
        title: "数学依据",
        description: "教师资料",
        documents_count: 0,
      };
      records = [...records, item];
      return item;
    });
    vi.mocked(patch).mockImplementation(async () => {
      const item = {
        ...records.find((item) => item.id === "b3")!,
        title: "数学课程依据",
      };
      records = records.map((record) => (record.id === "b3" ? item : record));
      return item;
    });
    await mount(<KnowledgePage />);
    await click("新建知识库");
    await fill('input[aria-label="知识库名称"]', "数学依据");
    await fill('textarea[aria-label="知识库说明"]', "教师资料");
    await act(async () =>
      host
        .querySelector('form[aria-label="知识库资料"]')!
        .dispatchEvent(
          new Event("submit", { bubbles: true, cancelable: true }),
        ),
    );
    expect(post).toHaveBeenCalledWith("/knowledge-bases", {
      title: "数学依据",
      description: "教师资料",
    });
    expect(host.textContent).toContain("数学依据");
    await click("编辑知识库");
    await fill('input[aria-label="知识库名称"]', "数学课程依据");
    await act(async () =>
      host
        .querySelector('form[aria-label="知识库资料"]')!
        .dispatchEvent(
          new Event("submit", { bubbles: true, cancelable: true }),
        ),
    );
    expect(patch).toHaveBeenCalledWith("/knowledge-bases/b3", {
      title: "数学课程依据",
      description: "教师资料",
    });
    await click("删除知识库");
    await click("确认删除知识库");
    expect(host.textContent).not.toContain("数学课程依据");
    expect(
      vi
        .mocked(api)
        .mock.calls.some(([path]) => path.startsWith("/personal-lessons")),
    ).toBe(false);
  });
  it("loads documents and pages through their real source fragments", async () => {
    await mount(<KnowledgePage />);
    expect(host.textContent).toContain("课程标准.pdf");
    await click("查看片段");
    expect(host.textContent).toContain(chunk.text);
    await click("下一页");
    expect(api).toHaveBeenCalledWith(
      "/knowledge-documents/doc1/chunks?offset=10&limit=10",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });
  it("returns source-backed lexical search results and an explicit empty result", async () => {
    let searches = 0;
    vi.mocked(api).mockImplementation(async (path, options) => {
      if (options?.method === "POST" && path.endsWith("/search"))
        return {
          items: searches++ === 0 ? [chunk] : [],
          method: "lexical_bm25",
        };
      if (path === "/knowledge-bases") return bases;
      if (path.endsWith("/documents")) return [document];
      return [];
    });
    await mount(<KnowledgePage />);
    await fill('input[aria-label="知识检索问题"]', "不存在的教学概念");
    await act(async () =>
      host
        .querySelector('form[aria-label="知识检索测试"]')!
        .dispatchEvent(
          new Event("submit", { bubbles: true, cancelable: true }),
        ),
    );
    expect(host.textContent).toContain(chunk.text);
    expect(host.textContent).toContain("课程标准.pdf");
    expect(host.textContent).toContain("关键词相关性检索");
    await act(async () =>
      host
        .querySelector<HTMLFormElement>('form[aria-label="知识检索测试"]')!
        .dispatchEvent(
          new Event("submit", { bubbles: true, cancelable: true }),
        ),
    );
    expect(host.textContent).toContain("没有找到相关片段");
    expect(api).toHaveBeenCalledWith(
      "/knowledge-bases/b1/search",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ query: "不存在的教学概念", top_k: 5 }),
      }),
    );
  });
  it("starts parsing a selected file and permits rebuilding a failed document", async () => {
    vi.mocked(post).mockResolvedValue({
      ...document,
      status: "processing",
      run_id: "parse1",
    });
    vi.mocked(api).mockImplementation(async (path) =>
      path === "/knowledge-bases"
        ? bases
        : path.endsWith("/documents")
          ? [{ ...document, status: "failed", error_message: "解析未完成" }]
          : [],
    );
    await mount(<KnowledgePage />);
    await click("重新解析");
    expect(post).toHaveBeenCalledWith("/knowledge-documents/doc1/reindex");
    const file = new File(["# 课程资料"], "课程资料.md", {
      type: "text/markdown",
    });
    await act(async () => {
      const input = host.querySelector<HTMLInputElement>('input[type="file"]')!;
      Object.defineProperty(input, "files", {
        value: [file],
        configurable: true,
      });
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(
      vi
        .mocked(api)
        .mock.calls.some(
          ([path, options]) =>
            path === "/knowledge-bases/b1/documents" &&
            options?.method === "POST" &&
            options.body instanceof FormData,
        ),
    ).toBe(true);
  });
  it("keeps a newer selected library when an older document request resolves late", async () => {
    let old!: (value: unknown) => void;
    vi.mocked(api).mockImplementation((path) =>
      path === "/knowledge-bases"
        ? Promise.resolve(bases)
        : path === "/knowledge-bases/b1/documents"
          ? new Promise((resolve) => {
              old = resolve;
            })
          : Promise.resolve([
              {
                ...document,
                id: "doc2",
                base_id: "b2",
                filename: "当前材料.pdf",
              },
            ]),
    );
    await mount(<KnowledgePage />);
    await click("选择知识库 实验材料");
    await act(async () => old([document]));
    expect(host.textContent).toContain("当前材料.pdf");
    expect(host.textContent).not.toContain("课程标准.pdf");
  });
  it("polls an in-progress parse to readiness and deletes the real document", async () => {
    vi.useFakeTimers();
    let reads = 0,
      deleted = false;
    vi.mocked(api).mockImplementation(async (path, options) => {
      if (path === "/knowledge-bases") return bases;
      if (
        path === "/knowledge-documents/doc1" &&
        options?.method === "DELETE"
      ) {
        deleted = true;
        return { deleted: true };
      }
      if (path.endsWith("/documents"))
        return deleted
          ? []
          : [{ ...document, status: reads++ === 0 ? "processing" : "ready" }];
      return [];
    });
    await mount(<KnowledgePage />);
    expect(host.textContent).toContain("正在解析");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2501);
    });
    expect(host.textContent).toContain("可检索");
    await click("删除资料 课程标准.pdf");
    await click("确认删除资料");
    expect(api).toHaveBeenCalledWith("/knowledge-documents/doc1", {
      method: "DELETE",
    });
    expect(host.textContent).not.toContain("课程标准.pdf");
  });
  it("opens the correct source page for a zero-based ordinal at a page boundary", async () => {
    const boundary = { ...chunk, ordinal: 10 };
    vi.mocked(api).mockImplementation(async (path, options) => {
      if (path === "/knowledge-bases") return bases;
      if (path.endsWith("/documents")) return [document];
      if (options?.method === "POST")
        return { items: [boundary], method: "lexical_bm25" };
      if (path === "/knowledge-documents/doc1") return document;
      if (path.includes("/chunks")) return { items: [boundary], total: 11 };
      return [];
    });
    await mount(<KnowledgePage />);
    await fill('input[aria-label="知识检索问题"]', "教学目标");
    await act(async () =>
      host
        .querySelector('form[aria-label="知识检索测试"]')!
        .dispatchEvent(
          new Event("submit", { bubbles: true, cancelable: true }),
        ),
    );
    await click("查看来源");
    expect(api).toHaveBeenCalledWith(
      "/knowledge-documents/doc1/chunks?offset=10&limit=10",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(host.textContent).toContain("片段 11");
  });
});
describe("lesson knowledge references", () => {
  async function mountGeneration(
    moduleTypes = ["custom_module", "custom_module"],
  ) {
    let eventSink!: (event: StreamEvent) => void;
    const draft: Draft = {
      id: "d1",
      topic: "分数",
      school_stage: "primary",
      grade: "grade_4",
      subject: "math",
      lesson_type: "new_lesson",
      total_duration_min: 40,
      status: "generating",
      content_version: 1,
      module_config_version_id: "cfg",
      modules: moduleTypes.map((moduleType, i) => ({
        module_id: String.fromCharCode(97 + i),
        module_type: moduleType,
        display_name: `模块${i + 1}`,
        enabled: true,
        is_custom: moduleType === "custom_module",
        module_summary: "",
        teacher_view_content: "",
        current_version_id: null,
        state: { generation_status: "pending" },
      })),
      knowledge_context_snapshot: [chunk],
    };
    vi.mocked(api).mockImplementation(async () => draft);
    vi.mocked(watchRun).mockImplementation(
      async (_id, _signal, onEvent, onState) => {
        eventSink = onEvent;
        onState({ id: "r1", status: "running" });
      },
    );
    await act(async () =>
      root.render(
        <MemoryRouter initialEntries={["/lessons/d1?run=r1"]}>
          <Routes>
            <Route path="/lessons/:id" element={<LessonPage />} />
          </Routes>
        </MemoryRouter>,
      ),
    );
    return (event: string, data: Record<string, unknown>) =>
      eventSink({ id: "1", event, data });
  }

  it("uses all libraries by default and sends an explicit empty array when disabled", async () => {
    vi.mocked(post).mockResolvedValue({ draft_id: "d1", run_id: "r1" });
    await mount(
      <NewLesson
        user={{
          id: "u1",
          name: "教师",
          email: "teacher@example.test",
          role: "teacher",
        }}
      />,
    );
    await fill('input[placeholder^="例如：观潮"]', "分数的认识");
    expect(
      Array.from(
        host.querySelectorAll<HTMLInputElement>(
          ".knowledge-base-options input",
        ),
      ).every((input) => input.checked),
    ).toBe(true);
    await act(async () =>
      host
        .querySelector("form")!
        .dispatchEvent(
          new Event("submit", { bubbles: true, cancelable: true }),
        ),
    );
    expect(post).toHaveBeenCalledWith(
      "/lesson-drafts/generations/one-click",
      expect.objectContaining({ knowledge_base_ids: null }),
    );
    vi.mocked(post).mockClear();
    await click("不使用知识资料");
    await act(async () =>
      host
        .querySelector("form")!
        .dispatchEvent(
          new Event("submit", { bubbles: true, cancelable: true }),
        ),
    );
    expect(post).toHaveBeenCalledWith(
      "/lesson-drafts/generations/one-click",
      expect.objectContaining({ knowledge_base_ids: [] }),
    );
    await click("全部选用");
    await act(async () =>
      host
        .querySelector<HTMLInputElement>(".knowledge-base-options input")!
        .click(),
    );
    vi.mocked(post).mockClear();
    await act(async () =>
      host
        .querySelector("form")!
        .dispatchEvent(
          new Event("submit", { bubbles: true, cancelable: true }),
        ),
    );
    expect(post).toHaveBeenCalledWith(
      "/lesson-drafts/generations/one-click",
      expect.objectContaining({ knowledge_base_ids: ["b2"] }),
    );
  });
  it("clears only the retried module stream and preserves another completed module", async () => {
    const send = await mountGeneration();
    await act(async () => {
      send("module_started", { module_id: "a" });
      send("content_delta", { module_id: "a", delta: "第一个已完成模块" });
      send("module_done", { module_id: "a", is_temporary: false });
      send("module_started", { module_id: "b" });
      send("content_delta", { module_id: "b", delta: "第二个废弃片段" });
      send("module_progress", {
        phase: "quality_retry_started",
        module_id: "b",
        attempt: 2,
        max_attempts: 3,
      });
      send("module_progress", {
        phase: "generation_attempt_reset",
        scope: "module",
        module_id: "b",
      });
    });
    expect(host.textContent).not.toContain("第二个废弃片段");
    expect(
      host.querySelector(".module-editor-heading .status")?.textContent,
    ).toBe("正在重试");
    await click("01模块1");
    expect(host.textContent).toContain("第一个已完成模块");
    expect(host.textContent).toContain("第 2/3 次");
    expect(host.querySelector<HTMLProgressElement>("progress")?.value).toBe(1);
    expect(host.textContent).toContain("课程标准.pdf");
    expect(
      host.querySelector('nav[aria-label="教学模块"]')?.textContent,
    ).not.toContain("重试");
    await act(async () => {
      send("module_progress", { phase: "worker_recovering" });
    });
    expect(host.textContent).toContain("正在恢复备课任务");
    await act(async () => {
      send("module_progress", {
        phase: "checkpoint_restored",
        module_id: "a",
        checkpoint_key: "module:a",
      });
      send("content_delta", {
        module_id: "a",
        replace_previous: true,
        delta: "已恢复的已保存模块",
      });
    });
    expect(host.textContent).toContain("已恢复的已保存模块");
    expect(host.textContent).not.toContain("第一个已完成模块");
    await act(async () => {
      send("module_progress", {
        phase: "generation_attempt_reset",
        scope: "all",
      });
    });
    expect(host.textContent).not.toContain("已恢复的已保存模块");
    expect(host.querySelector<HTMLProgressElement>("progress")?.value).toBe(0);
  });
  it("keeps dispatch failures waiting for automatic retry and resumes on publication", async () => {
    const send = await mountGeneration();
    await act(async () =>
      send("module_progress", {
        phase: "dispatch_retry_scheduled",
        attempt: 1,
      }),
    );
    expect(host.textContent).toContain("正在等待自动重试");
    expect(host.textContent).not.toContain("生成失败");
    await act(async () =>
      send("module_progress", { phase: "dispatch_published", attempt: 2 }),
    );
    expect(host.textContent).toContain("任务已重新投递");
  });
  it("keeps an activity checkpoint generating until the entire module finishes", async () => {
    const send = await mountGeneration(["teaching_process"]);
    await act(async () => {
      send("structured_step_started", { module_id: "a", stage: "activities" });
      send("module_progress", {
        phase: "checkpoint_restored",
        module_id: "a",
        stage_id: "opening",
        checkpoint_key: "activity:a:opening",
      });
      send("content_delta", {
        module_id: "a",
        replace_previous: true,
        delta: "已恢复的导入环节",
      });
    });
    expect(host.textContent).toContain("已恢复的导入环节");
    expect(host.querySelector<HTMLProgressElement>("progress")?.value).toBe(0);
    expect(
      host.querySelector(".module-editor-heading .status")?.textContent,
    ).toBe("正在生成");
    expect(host.textContent).toContain("已恢复课堂环节");
    await act(async () => {
      send("stage_done", { module_id: "a", stage_id: "opening" });
      send("stage_started", {
        module_id: "a",
        stage_id: "inquiry",
        title: "自主探究",
        stage_index: 2,
        stage_count: 2,
      });
      send("content_delta", { module_id: "a", delta: "继续生成探究活动" });
    });
    expect(host.textContent).toContain("自主探究（2/2）");
    expect(host.textContent).toContain("已恢复的导入环节继续生成探究活动");
    expect(host.querySelector<HTMLProgressElement>("progress")?.value).toBe(0);
    await act(async () => {
      send("module_done", { module_id: "a", is_temporary: true });
    });
    expect(host.querySelector<HTMLProgressElement>("progress")?.value).toBe(1);
  });
  it("retains retry counts and the active classroom stage after a scoped reset", async () => {
    const send = await mountGeneration(["teaching_process"]);
    await act(async () => {
      send("structured_step_started", { module_id: "a", stage: "activities" });
      send("stage_started", {
        module_id: "a",
        stage_id: "inquiry",
        title: "自主探究",
        stage_index: 2,
        stage_count: 2,
      });
      send("content_delta", { module_id: "a", delta: "废弃的探究活动" });
      send("module_progress", {
        phase: "quality_retry_started",
        module_id: "a",
        attempt: 2,
        max_attempts: 3,
      });
      send("module_progress", {
        phase: "generation_attempt_reset",
        scope: "module",
        module_id: "a",
      });
    });
    expect(host.textContent).not.toContain("废弃的探究活动");
    expect(host.textContent).toContain("第 2/3 次");
    expect(host.textContent).toContain("自主探究（2/2）");
    expect(
      host.querySelector(".module-editor-heading .status")?.textContent,
    ).toBe("正在重试");
    expect(host.querySelector<HTMLProgressElement>("progress")?.value).toBe(0);
  });
  it("retains a whole-generation retry notice while clearing all abandoned streams", async () => {
    const send = await mountGeneration();
    await act(async () => {
      send("module_started", { module_id: "a" });
      send("content_delta", { module_id: "a", delta: "整次废弃的正文" });
      send("module_done", { module_id: "a", is_temporary: true });
      send("module_progress", {
        phase: "quality_retry_started",
        attempt: 3,
        max_attempts: 3,
      });
      send("module_progress", {
        phase: "generation_attempt_reset",
        scope: "all",
      });
    });
    expect(host.textContent).not.toContain("整次废弃的正文");
    expect(host.textContent).toContain("第 3/3 次");
    expect(host.querySelector<HTMLProgressElement>("progress")?.value).toBe(0);
  });
});
