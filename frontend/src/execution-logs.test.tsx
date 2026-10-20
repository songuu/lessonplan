import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./api";
import { ExecutionLogsPage } from "./ExecutionLogs";
import type { ExecutionLog, User } from "./types";

vi.mock("./api", async (original) => ({
  ...(await original<typeof import("./api")>()),
  api: vi.fn(),
}));
const admin: User = {
  id: "admin1",
  name: "管理员",
  email: "admin@example.test",
  role: "admin",
};
const entry = (
  sequence: number,
  extra: Partial<ExecutionLog> = {},
): ExecutionLog => ({
  sequence,
  event_id: `event-${sequence}`,
  run_id: "run-1",
  execution_epoch: 2,
  trace_id: "trace-1",
  span_id: `span-${sequence}`,
  parent_span_id: "root-span",
  phase: "end",
  name: "llm.request",
  run_type: "llm",
  level: "info",
  status: "completed",
  started_at: "2026-10-20T02:00:00Z",
  ended_at: "2026-10-20T02:00:01Z",
  elapsed_ms: 1000,
  metadata: {
    model_name: "test-model",
    prompt_tokens: 201,
    completion_tokens: 317,
    total_tokens: 518,
    first_token_ms: 133,
  },
  created_at: "2026-10-20T02:00:01Z",
  workflow_type: "one_click",
  ...extra,
});
let host: HTMLDivElement, root: Root;
const button = (label: string) =>
  Array.from(host.querySelectorAll<HTMLButtonElement>("button")).find(
    (item) =>
      item.textContent?.trim() === label ||
      item.getAttribute("aria-label") === label,
  )!;
async function click(label: string) {
  await act(async () => button(label).click());
}
async function mount(user = admin) {
  await act(async () =>
    root.render(
      <MemoryRouter>
        <ExecutionLogsPage user={user} />
      </MemoryRouter>,
    ),
  );
}
async function change(label: string, value: string) {
  await act(async () => {
    const element = host.querySelector<HTMLInputElement | HTMLSelectElement>(
      `[aria-label="${label}"]`,
    )!;
    const prototype =
      element instanceof HTMLSelectElement
        ? HTMLSelectElement.prototype
        : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(
      element,
      value,
    );
    element.dispatchEvent(
      new Event(element instanceof HTMLSelectElement ? "change" : "input", {
        bubbles: true,
      }),
    );
  });
}
beforeEach(() => {
  (
    globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
  ).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  vi.mocked(api).mockImplementation(async (path) =>
    path === "/admin/observability"
      ? { provider: "langfuse", enabled: false, configured: false }
      : { items: [entry(1)], next_after: 1, has_more: false },
  );
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.useRealTimers();
});
describe("admin execution logs", () => {
  it("does not request admin data for a teacher", async () => {
    await mount({ ...admin, role: "teacher" });
    expect(host.textContent).toContain("无管理端访问权限");
    expect(api).not.toHaveBeenCalled();
  });
  it("shows local execution facts even when tracing is unconfigured", async () => {
    await mount();
    expect(host.textContent).toContain("Langfuse 未配置");
    expect(host.textContent).toContain("llm.request");
    expect(host.textContent).toContain("实际模型请求");
    expect(host.textContent).toContain("批次 2");
    await click("查看日志 event-1");
    expect(host.textContent).toContain("test-model");
    expect(host.textContent).toContain("518");
    expect(host.textContent).toContain("133 ms");
    expect(host.textContent).toContain("trace-1");
  });
  it("does not expose raw metadata, secret content or untrusted links", async () => {
    vi.mocked(api).mockResolvedValue({
      items: [
        entry(1, {
          metadata: {
            model_name: "safe-model",
            error_code: "LLM_TIMEOUT",
            prompt: "PRIVATE_PROMPT",
            response_body: "PRIVATE_RESPONSE",
            api_key: "PRIVATE_KEY",
            error_message: "PRIVATE_ERROR",
            trace_url: "https://evil.test/",
          },
        }),
      ],
      next_after: 1,
      has_more: false,
    });
    await mount();
    await click("查看日志 event-1");
    expect(host.textContent).toContain("LLM_TIMEOUT");
    expect(host.textContent).not.toContain("PRIVATE_");
    expect(host.querySelector('a[href*="evil.test"]')).toBeNull();
  });
  it("copies the 32 character Langfuse trace identity while retaining the local UUID", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    vi.mocked(api).mockImplementation(async (path) =>
      path === "/admin/observability"
        ? {}
        : {
            items: [
              entry(1, { trace_id: "cb68fe9c-1c89-5eb0-b038-ecf42d08ffe2" }),
            ],
            next_after: 1,
            has_more: false,
          },
    );
    await mount();
    await click("查看日志 event-1");
    expect(host.textContent).toContain("cb68fe9c-1c89-5eb0-b038-ecf42d08ffe2");
    await click("复制 Langfuse Trace ID");
    expect(writeText).toHaveBeenCalledWith("cb68fe9c1c895eb0b038ecf42d08ffe2");
  });
  it("distinguishes interrupted observations from successful completion", async () => {
    vi.mocked(api).mockImplementation(async (path) =>
      path === "/admin/observability"
        ? {}
        : {
            items: [entry(1, { status: "interrupted", level: "warning" })],
            next_after: 1,
            has_more: false,
          },
    );
    await mount();
    expect(host.querySelector("[data-log-event]")?.textContent).toContain(
      "中断 / 结果待确认",
    );
    expect(host.querySelector("[data-log-event]")?.textContent).not.toContain(
      "已完成",
    );
    await change("执行状态", "interrupted");
    await click("应用筛选");
    expect(
      vi
        .mocked(api)
        .mock.calls.some(([path]) => path.includes("status=interrupted")),
    ).toBe(true);
  });
  it("distinguishes receipt reuse from fresh requests without showing reused token usage", async () => {
    vi.mocked(api).mockImplementation(async (path) =>
      path === "/admin/observability"
        ? {}
        : {
            items: [
              entry(1, {
                name: "receipt.replay",
                run_type: "tool",
                metadata: {
                  receipt_reused: true,
                  total_tokens: 999,
                  capture_epoch: 1,
                },
              }),
            ],
            next_after: 1,
            has_more: false,
          },
    );
    await mount();
    expect(host.textContent).toContain("回执复用");
    await click("查看日志 event-1");
    expect(host.textContent).toContain("不新增模型用量");
    expect(host.textContent).not.toContain("999");
  });
  it("loads subsequent events in order while retaining the first page", async () => {
    vi.mocked(api).mockImplementation(async (path) =>
      path === "/admin/observability"
        ? {}
        : new URLSearchParams(path.split("?")[1]).get("after") === "0"
          ? { items: [entry(1)], next_after: 1, has_more: true }
          : {
              items: [entry(3), entry(2), entry(1)],
              next_after: 3,
              has_more: false,
            },
    );
    await mount();
    await click("加载更多日志");
    const rows = host.querySelectorAll("[data-log-event]");
    expect(
      Array.from(rows).map((row) => row.getAttribute("data-log-event")),
    ).toEqual(["event-1", "event-2", "event-3"]);
    expect(
      vi.mocked(api).mock.calls.some(([path]) => path.includes("after=1")),
    ).toBe(true);
  });
  it("applies server filters and ignores a previous response arriving late", async () => {
    let finishOld: (data: unknown) => void = () => {};
    let oldSignal: AbortSignal | undefined;
    vi.mocked(api).mockImplementation((path, options) => {
      if (path === "/admin/observability") return Promise.resolve({});
      if (!path.includes("run_id=")) {
        oldSignal = options?.signal as AbortSignal;
        return new Promise((resolve) => {
          finishOld = resolve;
        });
      }
      return Promise.resolve({
        items: [entry(2, { run_id: "new-run" })],
        next_after: 2,
        has_more: false,
      });
    });
    await mount();
    await change("任务 ID", "new-run");
    await change("日志级别", "error");
    await change("执行状态", "failed");
    await change("工作流", "one_click");
    await click("应用筛选");
    const query = vi
      .mocked(api)
      .mock.calls.map(([path]) => path)
      .find((path) => path.includes("run_id="))!;
    const params = new URLSearchParams(query.split("?")[1]);
    expect(Object.fromEntries(params)).toMatchObject({
      run_id: "new-run",
      level: "error",
      status: "failed",
      workflow: "one_click",
      after: "0",
    });
    expect(oldSignal?.aborted).toBe(true);
    await act(async () =>
      finishOld({ items: [entry(1)], next_after: 1, has_more: false }),
    );
    expect(host.textContent).toContain("new-run");
    expect(host.querySelector('[data-log-event="event-1"]')).toBeNull();
  });
  it("keeps a retry action for an initial failure and a clear empty state", async () => {
    let attempts = 0;
    vi.mocked(api).mockImplementation(async (path) => {
      if (path === "/admin/observability")
        throw new Error("platform unavailable");
      if (++attempts === 1) throw new Error("日志读取暂时失败");
      return { items: [], next_after: 0, has_more: false };
    });
    await mount();
    expect(host.textContent).toContain("日志读取暂时失败");
    await click("重试日志读取");
    expect(host.textContent).toContain("暂无执行日志");
    expect(host.textContent).toContain("追踪平台状态暂时不可用");
  });
  it("only polls after explicit opt-in and aborts all requests on unmount", async () => {
    vi.useFakeTimers();
    await mount();
    const count = vi.mocked(api).mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });
    expect(api).toHaveBeenCalledTimes(count);
    const checkbox = host.querySelector<HTMLInputElement>(
      '[aria-label="自动刷新日志"]',
    )!;
    await act(async () => checkbox.click());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(api).toHaveBeenCalledTimes(count + 1);
    const signals = vi
      .mocked(api)
      .mock.calls.map(([, options]) => options?.signal as AbortSignal)
      .filter(Boolean);
    await act(async () => root.unmount());
    root = createRoot(host);
    expect(signals.every((signal) => signal.aborted)).toBe(true);
    const stopped = vi.mocked(api).mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });
    expect(api).toHaveBeenCalledTimes(stopped);
  });
  it("clears cached facts and stops polling when admin access is revoked", async () => {
    vi.useFakeTimers();
    await mount();
    vi.mocked(api).mockRejectedValue(
      new ApiError(403, "FORBIDDEN", "无管理端访问权限"),
    );
    await act(async () =>
      host
        .querySelector<HTMLInputElement>('[aria-label="自动刷新日志"]')!
        .click(),
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(host.querySelector("[data-log-event]")).toBeNull();
    const count = vi.mocked(api).mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });
    expect(api).toHaveBeenCalledTimes(count);
  });
});
