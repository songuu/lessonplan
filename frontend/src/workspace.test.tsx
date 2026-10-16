import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import type { Editor } from "@tiptap/core";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, patch, post } from "./api";
import { LessonPage } from "./Lesson";
import { AdminPage, LibraryPage } from "./Pages";
import type { Draft } from "./types";
vi.mock("./api", () => ({
  api: vi.fn(),
  patch: vi.fn(),
  post: vi.fn(),
  track: vi.fn(),
  download: vi.fn(),
  watchRun: vi.fn().mockResolvedValue(undefined),
}));
const module = (id: string, name: string) => ({
  module_id: id,
  module_type: "custom_module",
  display_name: name,
  module_summary: "",
  enabled: true,
  is_custom: true,
  teacher_view_content: `${name}正式正文`,
  current_version_id: `v-${id}`,
  state: {
    generation_status: "ready",
    confirmation_status: "unconfirmed",
    freshness: "fresh",
  },
});
let draft: Draft;
let root: Root;
let host: HTMLDivElement;
const editor = () =>
  (host.querySelector(".rich-editor") as HTMLElement & { editor: Editor })
    .editor;
const button = (name: string) =>
  Array.from(host.querySelectorAll<HTMLButtonElement>("button")).find(
    (b) =>
      b.textContent?.trim() === name || b.getAttribute("aria-label") === name,
  )!;
async function click(name: string) {
  await act(async () => {
    button(name).click();
  });
}
async function mount(node: React.ReactNode) {
  await act(async () => {
    root.render(node);
  });
}
async function lesson() {
  await mount(
    <MemoryRouter initialEntries={["/lessons/d1"]}>
      <Routes>
        <Route path="/lessons/:id" element={<LessonPage />} />
      </Routes>
    </MemoryRouter>,
  );
}
beforeEach(() => {
  (
    globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
  ).IS_REACT_ACT_ENVIRONMENT = true;
  sessionStorage.clear();
  vi.clearAllMocks();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  draft = {
    id: "d1",
    topic: "合成验收课题",
    school_stage: "primary",
    grade: "grade_4",
    subject: "chinese",
    lesson_type: "new_lesson",
    total_duration_min: 40,
    status: "ready",
    generation_mode: "one_click",
    content_version: 1,
    module_config_version_id: "cfg1",
    modules: [module("a", "模块甲"), module("b", "模块乙")],
  };
  vi.mocked(api).mockImplementation(async (path) => {
    if (path === "/lesson-drafts/d1") return structuredClone(draft);
    if (path.endsWith("/post-lesson-reflection"))
      return { content: "", version: 0 };
    return [];
  });
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
});
describe("teacher working-copy lifecycle", () => {
  it("restores an unconfirmed teacher outline after reopening the lesson", async () => {
    draft.generation_mode = "interactive_step";
    draft.modules[0] = {
      ...draft.modules[0],
      module_type: "teaching_process",
      state: {
        ...draft.modules[0].state,
        workflow_stage: "outline",
        outline: [{ stage_id: "s1", title: "正式大纲", duration_min: 40 }],
      },
    };
    sessionStorage.setItem(
      "lesson-outline:d1:a",
      JSON.stringify({
        outline: [
          { stage_id: "s1", title: "教师尚未确认的新环节", duration_min: 43 },
        ],
      }),
    );
    await lesson();
    expect(
      host.querySelector<HTMLInputElement>('input[placeholder="课堂环节名称"]')
        ?.value,
    ).toBe("教师尚未确认的新环节");
    expect(host.textContent).toContain("大纲有未确认修改");
    expect(sessionStorage.getItem("lesson-outline:d1:a")).toContain(
      "教师尚未确认的新环节",
    );
  });
  it("locks editor and module navigation during a delayed save", async () => {
    let complete!: () => void;
    vi.mocked(patch).mockImplementation(
      () =>
        new Promise((resolve) => {
          complete = () => resolve(undefined);
        }),
    );
    await lesson();
    await act(async () => {
      editor().commands.setContent("甲的修改正文", { contentType: "markdown" });
    });
    await click("保存正文");
    expect(editor().isEditable).toBe(false);
    const switchButton = Array.from(
      host.querySelectorAll<HTMLButtonElement>(
        'nav[aria-label="教学模块"] button',
      ),
    )[1];
    expect(switchButton.disabled).toBe(true);
    draft = {
      ...draft,
      content_version: 2,
      modules: [
        {
          ...draft.modules[0],
          teacher_view_content: "甲的修改正文",
          current_version_id: "v-a-2",
        },
        draft.modules[1],
      ],
    };
    await act(async () => complete());
    expect(editor().getMarkdown()).toBe("甲的修改正文");
    expect(editor().isEditable).toBe(true);
  });
  it("starts a separate undo history when changing modules", async () => {
    await lesson();
    await act(async () => {
      editor().commands.setContent("甲的未保存修改", {
        contentType: "markdown",
      });
    });
    const original = editor();
    await act(async () => {
      Array.from(
        host.querySelectorAll<HTMLButtonElement>(
          'nav[aria-label="教学模块"] button',
        ),
      )[1].click();
    });
    expect(editor()).not.toBe(original);
    await act(async () => {
      editor().commands.undo();
    });
    expect(editor().getMarkdown()).toBe("模块乙正式正文");
    expect(sessionStorage.getItem("lesson-copy:d1:a")).toContain(
      "甲的未保存修改",
    );
  });
  it("restores the next module copy after structured confirmation", async () => {
    draft.generation_mode = "interactive_step";
    sessionStorage.setItem(
      "lesson-copy:d1:b",
      JSON.stringify({
        content: "乙的未保存备课想法",
        moduleVersion: "v-b",
        contentVersion: 1,
        configVersion: "cfg1",
      }),
    );
    vi.mocked(post).mockImplementation(async () => {
      draft = {
        ...draft,
        content_version: 2,
        modules: [
          {
            ...draft.modules[0],
            state: {
              ...draft.modules[0].state,
              confirmation_status: "confirmed",
            },
          },
          draft.modules[1],
        ],
      };
      return structuredClone(draft);
    });
    await lesson();
    await click("确认，继续下一步");
    expect(editor().getMarkdown()).toBe("乙的未保存备课想法");
    expect(sessionStorage.getItem("lesson-copy:d1:b")).toContain(
      "乙的未保存备课想法",
    );
  });
  it("retains a dirty reflection when its dialog is reopened", async () => {
    await lesson();
    await click("课后反思");
    await act(async () => {
      const reflectionEditor = (
        host.querySelector(".modal .rich-editor") as HTMLElement & {
          editor: Editor;
        }
      ).editor;
      reflectionEditor.commands.setContent("真实课堂观察，尚未保存", {
        contentType: "markdown",
      });
    });
    await act(async () => {
      host
        .querySelector<HTMLButtonElement>('.modal button[aria-label="关闭"]')!
        .click();
    });
    await click("课后反思");
    expect(host.querySelector(".modal .rich-editor")?.textContent).toContain(
      "真实课堂观察，尚未保存",
    );
    expect(
      vi
        .mocked(api)
        .mock.calls.filter(([path]) =>
          path.endsWith("/post-lesson-reflection"),
        ),
    ).toHaveLength(1);
  });
});
it("ignores an older admin response after a newer tab resolves", async () => {
  let resolveOverview!: (value: unknown) => void;
  let resolveUsers!: (value: unknown) => void;
  vi.mocked(api).mockImplementation(
    (path) =>
      new Promise((resolve) => {
        if (path.endsWith("/overview")) resolveOverview = resolve;
        else resolveUsers = resolve;
      }),
  );
  await mount(
    <MemoryRouter>
      <AdminPage />
    </MemoryRouter>,
  );
  await click("用户管理");
  await act(async () =>
    resolveUsers([
      { id: "u1", name: "当前用户列表", email: "test@example.test" },
    ]),
  );
  await act(async () => resolveOverview({ users: 999 }));
  expect(host.textContent).toContain("当前用户列表");
  expect(host.textContent).not.toContain("999");
});
it("renders parsed library records whose style summary is structured metadata", async () => {
  vi.mocked(api).mockResolvedValue([
    {
      id: "document1",
      filename: "实际导出教案.docx",
      status: "ready",
      style_summary: { heading_count: 7, paragraph_count: 25 },
    },
  ]);
  await mount(
    <MemoryRouter>
      <LibraryPage />
    </MemoryRouter>,
  );
  expect(host.textContent).toContain("实际导出教案.docx");
  expect(host.textContent).toContain("已完成");
});
