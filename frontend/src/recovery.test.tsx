import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./api";
import { RecoveryPage } from "./Recovery";
import type { RecoveryRun, User } from "./types";
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
const run: RecoveryRun = {
  id: "r1",
  draft_id: "d1",
  topic: "长方形",
  user_id: "u1",
  workflow_type: "one_click",
  status: "queued",
  error_code: null,
  execution_epoch: 2,
  recovery_count: 1,
  heartbeat_at: null,
  lease_expires_at: null,
  created_at: "2026-10-16T02:00:00Z",
  started_at: null,
  finished_at: null,
  checkpoint_count: 2,
  dispatch: {
    id: "o1",
    status: "pending",
    attempts: 3,
    next_attempt_at: "2026-10-16T02:01:00Z",
    last_attempt_at: null,
    last_error_code: "QUEUE_UNAVAILABLE",
    deadline_at: "2026-10-16T02:05:00Z",
  },
  can_retry: true,
  can_cancel: true,
};
const alert = {
  id: "a1",
  run_id: "r1",
  code: "DISPATCH_RETRY",
  severity: "warning",
  message: "等待自动重试",
  status: "open",
  occurrence_count: 3,
  first_seen_at: run.created_at,
  last_seen_at: run.created_at,
  acknowledged_at: null,
  acknowledged_by: null,
  resolved_at: null,
  metadata_json: {},
};
let host: HTMLDivElement, root: Root;
const button = (text: string) =>
  Array.from(host.querySelectorAll<HTMLButtonElement>("button")).find(
    (b) =>
      b.textContent?.trim() === text || b.getAttribute("aria-label") === text,
  )!;
async function click(text: string) {
  await act(async () => button(text).click());
}
async function mount(user = admin, entry = "/admin/recovery") {
  await act(async () =>
    root.render(
      <MemoryRouter initialEntries={[entry]}>
        <RecoveryPage user={user} />
      </MemoryRouter>,
    ),
  );
}
async function value(label: string, text: string) {
  await act(async () => {
    const el = host.querySelector<
      HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement
    >(`[aria-label="${label}"]`)!;
    const proto =
      el instanceof HTMLSelectElement
        ? HTMLSelectElement.prototype
        : el instanceof HTMLTextAreaElement
          ? HTMLTextAreaElement.prototype
          : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, text);
    el.dispatchEvent(
      new Event(el instanceof HTMLSelectElement ? "change" : "input", {
        bubbles: true,
      }),
    );
  });
}
function fixtures(path: string) {
  if (path === "/admin/recovery-overview")
    return {
      queued: 1,
      running: 0,
      retry_pending: 1,
      expired_leases: 0,
      open_alerts: 1,
      failed: 0,
    };
  if (path.startsWith("/admin/recovery-runs?"))
    return {
      items: [run],
      total: 21,
      offset: 0,
      limit: 20,
      server_time: run.created_at,
    };
  if (path.startsWith("/admin/recovery-runs/r1?"))
    return {
      run,
      checkpoints: [
        {
          id: "c1",
          key: "module:m1",
          created_at: run.created_at,
          input_fingerprint: "abc",
          content_hash: "def",
        },
      ],
      calls: [],
      events: [
        {
          sequence: 1,
          event_type: "module_progress",
          created_at: run.created_at,
          payload: { phase: "dispatch_retry_scheduled", attempt: 3 },
        },
      ],
      event_total: 1,
      alerts: [alert],
    };
  if (path.startsWith("/admin/recovery-alerts?"))
    return { items: [alert], total: 1, offset: 0, limit: 20 };
  return {};
}
beforeEach(() => {
  (
    globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
  ).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  vi.mocked(api).mockImplementation(async (path) => fixtures(path));
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.useRealTimers();
});
describe("recovery operations", () => {
  it("opens the originating task when linked from execution logs", async () => {
    await mount(admin, "/admin/recovery?run_id=r1");
    expect(vi.mocked(api).mock.calls.some(([path]) =>
      path.startsWith("/admin/recovery-runs/r1?"),
    )).toBe(true);
    expect(host.textContent).toContain("模型调用回执");
  });
  it("supports older task details without model invocation receipts", async () => {
    await mount();
    await click("查看任务 r1");
    expect(host.textContent).toContain("模型调用回执");
    expect(host.textContent).toContain("尚无持久化模型回执");
    expect(host.querySelector('[aria-label="模型调用回执"]')).toBeNull();
  });
  it("shows persisted and uncertain invocation receipts without exposing response bodies", async () => {
    const receipt = {
      id: "receipt1",
      invocation_key: "invocation-key-1",
      step_key: "one_click:batch1",
      attempt: 1,
      call_id: "model-call-1",
      call_index: 2,
      route: "primary",
      status: "response_saved",
      request_hash: "request-fingerprint-1",
      response_hash: "response-fingerprint-1",
      capture_epoch: 3,
      created_at: run.created_at,
      response_saved_at: "2026-10-16T02:00:10Z",
      applied_at: null,
      error_code: null,
      provider_request_id: "provider-job-1",
      model_name: "configured-model",
      prompt_tokens: 201,
      completion_tokens: 317,
      total_tokens: 518,
      response_body: "PRIVATE_RAW_MODEL_RESPONSE",
      messages: [{ content: "PRIVATE_TEACHER_PROMPT" }],
    };
    vi.mocked(api).mockImplementation(async (path) => {
      const result = fixtures(path);
      if (path.startsWith("/admin/recovery-runs/r1?"))
        return {
          ...(result as object),
          invocations: [
            receipt,
            {
              ...receipt,
              id: "receipt2",
              invocation_key: "invocation-key-2",
              status: "outcome_unknown",
              response_hash: null,
              response_saved_at: null,
              provider_request_id: null,
              prompt_tokens: null,
              completion_tokens: null,
              total_tokens: null,
              error_code: "LLM_OUTCOME_UNKNOWN",
            },
          ],
        };
      return result;
    });
    await mount();
    await click("查看任务 r1");
    const table = host.querySelector('[aria-label="模型调用回执"]');
    expect(table).not.toBeNull();
    expect(table?.textContent).toContain("响应已保存");
    expect(table?.textContent).toContain("结果未知");
    expect(table?.textContent).toContain("one_click:batch1");
    expect(table?.textContent).toContain("model-call-1");
    expect(table?.textContent).toContain("invocation-key-1");
    expect(table?.textContent).toContain("provider-job-1");
    expect(table?.textContent).toContain("request-fingerprint-1");
    expect(table?.textContent).toContain("response-fingerprint-1");
    expect(table?.textContent).toContain("第 1 次尝试");
    expect(table?.textContent).toContain("批次 3");
    expect(table?.textContent).toContain("输入 201");
    expect(table?.textContent).toContain("输出 317");
    expect(table?.textContent).toContain("总计 518");
    expect(table?.textContent).toContain("尚未取得实际用量");
    expect(table?.textContent).toContain("LLM_OUTCOME_UNKNOWN");
    expect(host.textContent).toContain("不会自动重新调用模型");
    expect(host.textContent).toContain(
      "当前通用模型接口不支持查询 Provider 结果",
    );
    expect(host.textContent).not.toContain("PRIVATE_RAW_MODEL_RESPONSE");
    expect(host.textContent).not.toContain("PRIVATE_TEACHER_PROMPT");
    expect(host.textContent).toContain("原始响应保存不代表模块已通过校验");
  });
  it("does not route administrators into other teachers private lessons", async () => {
    await mount();
    await click("查看任务 r1");
    expect(host.querySelector('a[href="/lessons/d1"]')).toBeNull();
    expect(host.textContent).toContain("联系教案拥有者");
  });
  it("omits empty status filters accepted only as absent by the API", async () => {
    await mount();
    const initial = vi
      .mocked(api)
      .mock.calls.find(([path]) =>
        path.startsWith("/admin/recovery-runs?"),
      )![0];
    expect(
      new URL(initial, "http://localhost").searchParams.has("status"),
    ).toBe(false);
    await value("告警状态", "");
    const latest = vi
      .mocked(api)
      .mock.calls.filter(([path]) => path.startsWith("/admin/recovery-alerts?"))
      .at(-1)![0];
    expect(new URL(latest, "http://localhost").searchParams.has("status")).toBe(
      false,
    );
  });
  it("blocks teacher access before issuing global management requests", async () => {
    await mount({ ...admin, role: "teacher" });
    expect(host.textContent).toContain("无管理端访问权限");
    expect(api).not.toHaveBeenCalled();
  });
  it("shows recovery facts, filters and paginates without reading teacher content", async () => {
    await mount();
    expect(host.textContent).toContain("任务恢复与告警");
    expect(host.textContent).toContain("长方形");
    await click("查看任务 r1");
    expect(host.textContent).toContain("module:m1");
    expect(host.textContent).toContain("abc");
    expect(host.textContent).toContain("dispatch_retry_scheduled");
    await value("任务状态", "failed");
    expect(api).toHaveBeenCalledWith(
      expect.stringContaining("status=failed"),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    await click("任务下一页");
    expect(api).toHaveBeenCalledWith(
      expect.stringContaining("offset=20"),
      expect.anything(),
    );
  });
  it("requires an operation reason and submits the observed epoch and attempt budget", async () => {
    await mount();
    await click("查看任务 r1");
    await click("立即重投");
    expect(button("确认立即重投").disabled).toBe(true);
    await value("操作原因", "排查 Redis 后主动重投");
    await click("确认立即重投");
    expect(api).toHaveBeenCalledWith(
      "/admin/recovery-runs/r1/retry",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          expected_epoch: 2,
          expected_attempts: 3,
          reason: "排查 Redis 后主动重投",
        }),
        signal: expect.any(AbortSignal),
      }),
    );
  });
  it("refreshes a conflicting operation without silently retrying stale commands", async () => {
    vi.mocked(api).mockImplementation(async (path, options) => {
      if (options?.method === "POST")
        throw new ApiError(409, "STALE_RUN", "任务状态已变化");
      return fixtures(path);
    });
    await mount();
    await click("查看任务 r1");
    await click("取消任务");
    await value("操作原因", "教师申请停止生成");
    await click("确认取消任务");
    expect(host.textContent).toContain("任务状态已变化");
    expect(
      vi
        .mocked(api)
        .mock.calls.filter(([, options]) => options?.method === "POST"),
    ).toHaveLength(1);
    expect(host.querySelector('[role="dialog"]')).toBeNull();
  });
  it("acknowledges alerts with a reason and preserves the alert lifecycle", async () => {
    await mount();
    await click("确认告警 a1");
    await value("操作原因", "已开始检查队列");
    await click("确认告警");
    expect(api).toHaveBeenCalledWith(
      "/admin/recovery-alerts/a1/acknowledge",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ reason: "已开始检查队列" }),
      }),
    );
  });
  it("polls after completion without overlapping requests and can be paused", async () => {
    vi.useFakeTimers();
    await mount();
    const count = vi.mocked(api).mock.calls.length;
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(vi.mocked(api).mock.calls.length).toBeGreaterThan(count);
    await act(async () =>
      host
        .querySelector<HTMLInputElement>(
          '.recovery-live-bar input[type="checkbox"]',
        )!
        .click(),
    );
    const paused = vi.mocked(api).mock.calls.length;
    await act(async () => vi.advanceTimersByTimeAsync(15000));
    expect(vi.mocked(api).mock.calls.length).toBe(paused);
  });
  it("aborts superseded filters and ignores late responses", async () => {
    let old!: (value: unknown) => void;
    let oldSignal: AbortSignal | undefined;
    vi.mocked(api).mockImplementation((path, options) => {
      if (
        path.startsWith("/admin/recovery-runs?") &&
        !path.includes("status=failed")
      ) {
        oldSignal = options?.signal as AbortSignal;
        return new Promise((resolve) => {
          old = resolve;
        });
      }
      if (path.includes("status=failed"))
        return Promise.resolve({
          items: [{ ...run, id: "new", topic: "当前任务" }],
          total: 1,
        });
      return Promise.resolve(fixtures(path));
    });
    await mount();
    await value("任务状态", "failed");
    expect(oldSignal?.aborted).toBe(true);
    await act(async () => old({ items: [run], total: 1 }));
    expect(host.textContent).toContain("当前任务");
    expect(host.textContent).not.toContain("长方形");
  });
  it("aborts requests on unmount and does not keep polling", async () => {
    vi.useFakeTimers();
    const signals: AbortSignal[] = [];
    vi.mocked(api).mockImplementation((_path, options) => {
      signals.push(options?.signal as AbortSignal);
      return new Promise(() => {});
    });
    await mount();
    await act(async () => root.render(<div />));
    expect(signals.every((signal) => signal.aborted)).toBe(true);
    const count = vi.mocked(api).mock.calls.length;
    await act(async () => vi.advanceTimersByTimeAsync(20000));
    expect(vi.mocked(api).mock.calls.length).toBe(count);
  });
  it("leaves valid running leases and terminal tasks without a retry action", async () => {
    vi.mocked(api).mockImplementation(async (path) => {
      const result = fixtures(path);
      if (path.startsWith("/admin/recovery-runs/r1?"))
        return {
          ...(result as object),
          run: {
            ...run,
            status: "completed",
            user_id: admin.id,
            can_retry: false,
            can_cancel: false,
          },
        };
      return result;
    });
    await mount();
    await click("查看任务 r1");
    expect(button("立即重投").disabled).toBe(true);
    expect(button("取消任务").disabled).toBe(true);
    expect(
      host.querySelector<HTMLAnchorElement>('a[href="/lessons/d1"]')
        ?.textContent,
    ).toContain("重新生成");
  });
  it("does not create another poll while a request remains in flight", async () => {
    vi.useFakeTimers();
    vi.mocked(api).mockImplementation(() => new Promise(() => {}));
    await mount();
    const count = vi.mocked(api).mock.calls.length;
    await act(async () => vi.advanceTimersByTimeAsync(20000));
    expect(vi.mocked(api).mock.calls.length).toBe(count);
  });
  it("shows authorization failures without retaining a protected task table", async () => {
    vi.mocked(api).mockRejectedValue(
      new ApiError(403, "FORBIDDEN", "需要管理员权限"),
    );
    await mount();
    expect(host.textContent).toContain("需要管理员权限");
    expect(host.textContent).not.toContain("长方形");
  });
  it("aborts an outstanding management mutation when the page unmounts", async () => {
    let mutationSignal: AbortSignal | undefined;
    vi.mocked(api).mockImplementation((path, options) => {
      if (options?.method === "POST") {
        mutationSignal = options.signal as AbortSignal;
        return new Promise(() => {});
      }
      return Promise.resolve(fixtures(path));
    });
    await mount();
    await click("查看任务 r1");
    await click("立即重投");
    await value("操作原因", "故障已排查完成");
    await click("确认立即重投");
    expect(mutationSignal?.aborted).toBe(false);
    await act(async () => root.render(<div />));
    expect(mutationSignal?.aborted).toBe(true);
  });
});
