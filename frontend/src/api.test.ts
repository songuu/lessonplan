import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, privateBlob, setToken, watchRun } from "./api";
const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify({ data }), {
    status,
    headers: { "Content-Type": "application/json" },
  });
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  setToken("");
});
describe("authenticated run recovery", () => {
  it("authenticates private previews without sending tokens to an external URL", async () => {
    setToken("synthetic-token");
    const fetcher = vi.fn().mockResolvedValue(new Response("image"));
    vi.stubGlobal("fetch", fetcher);
    await privateBlob("/api/v1/personal-lessons/file1/thumbnail");
    expect(fetcher).toHaveBeenCalledWith(
      "/api/v1/personal-lessons/file1/thumbnail",
      expect.objectContaining({
        headers: { Authorization: "Bearer synthetic-token" },
      }),
    );
    await expect(privateBlob("https://external.example/image")).rejects.toThrow(
      "文件地址无效",
    );
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it("recovers a terminal run without opening a stale SSE connection", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValue(json({ id: "r1", status: "completed" }));
    vi.stubGlobal("fetch", fetcher);
    const state = vi.fn();
    await watchRun("r1", new AbortController().signal, vi.fn(), state);
    expect(state).toHaveBeenCalledWith(
      expect.objectContaining({ status: "completed" }),
    );
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it("replays only new IDs and reconnects with the bearer and cursor", async () => {
    vi.useFakeTimers();
    setToken("synthetic-token");
    let streams = 0;
    const fetcher = vi
      .fn()
      .mockImplementation(async (url: string, options: RequestInit) => {
        if (!url.endsWith("/events"))
          return json({
            id: "r1",
            status: streams > 1 ? "completed" : "running",
          });
        streams++;
        if (streams === 2) {
          expect(options.headers).toMatchObject({
            Authorization: "Bearer synthetic-token",
            "Last-Event-ID": "1",
          });
        }
        const frames =
          streams === 1
            ? 'id: 1\nevent: content_delta\ndata: {"delta":"甲"}\n\n'
            : 'id: 1\nevent: content_delta\ndata: {"delta":"重复"}\n\nid: 2\nevent: run_completed\ndata: {}\n\n';
        return new Response(
          new ReadableStream({
            start(controller) {
              controller.enqueue(new TextEncoder().encode(frames));
              controller.close();
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        );
      });
    vi.stubGlobal("fetch", fetcher);
    const event = vi.fn();
    const reconnect = vi.fn();
    const operation = watchRun(
      "r1",
      new AbortController().signal,
      event,
      vi.fn(),
      reconnect,
    );
    await vi.advanceTimersByTimeAsync(1001);
    await operation;
    expect(event.mock.calls.map((call) => call[0].id)).toEqual(["1", "2"]);
    expect(reconnect.mock.calls.map(([value]) => value)).toEqual([
      false,
      true,
      false,
    ]);
  });
  it("recovers a terminal EOF without reporting another connection attempt", async () => {
    let opened = false;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async (url: string) => {
        if (!url.endsWith("/events"))
          return json({ id: "r1", status: opened ? "completed" : "running" });
        opened = true;
        return new Response(
          new ReadableStream({
            start(controller) {
              controller.close();
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        );
      }),
    );
    const state = vi.fn(),
      reconnect = vi.fn();
    await watchRun(
      "r1",
      new AbortController().signal,
      vi.fn(),
      state,
      reconnect,
    );
    expect(state).toHaveBeenLastCalledWith(
      expect.objectContaining({ status: "completed" }),
    );
    expect(reconnect.mock.calls.map(([value]) => value)).toEqual([false]);
  });
  it("retains controlled error context for teacher-facing conflicts", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            error: { code: "VERSION_CONFLICT", message: "模块已更新" },
            request_id: "req1",
          }),
          { status: 409 },
        ),
      ),
    );
    await expect(api("/test")).rejects.toMatchObject({
      constructor: ApiError,
      code: "VERSION_CONFLICT",
      requestId: "req1",
    });
  });
});
