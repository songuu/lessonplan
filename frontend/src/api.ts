import type { Run, StreamEvent } from "./types";
const API_BASE = import.meta.env.VITE_API_BASE || "/api/v1";
let token = sessionStorage.getItem("lesson_token") || "";
export function setToken(value: string) {
  token = value;
  if (value) sessionStorage.setItem("lesson_token", value);
  else sessionStorage.removeItem("lesson_token");
}
export function getToken() {
  return token;
}
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public requestId?: string,
  ) {
    super(message);
  }
}
export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (options.body && !(options.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  if (options.method && options.method !== "GET")
    headers.set(
      "Idempotency-Key",
      headers.get("Idempotency-Key") || crypto.randomUUID(),
    );
  const response = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (!response.ok) {
    let body;
    try {
      body = await response.json();
    } catch {
      body = {};
    }
    const error = new ApiError(
      response.status,
      body.error?.code || "HTTP_ERROR",
      body.error?.message ||
        body.detail?.message ||
        (typeof body.detail === "string"
          ? body.detail
          : `请求失败 (${response.status})`),
      body.request_id,
    );
    if (response.status === 401)
      window.dispatchEvent(new Event("lesson:unauthorized"));
    throw error;
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()).data as T;
}
export const post = <T>(path: string, body: unknown = {}) =>
  api<T>(path, { method: "POST", body: JSON.stringify(body) });
export const patch = <T>(path: string, body: unknown) =>
  api<T>(path, { method: "PATCH", body: JSON.stringify(body) });
export function track(
  event_name: string,
  properties: Record<string, string | number | boolean> = {},
) {
  // Analytics failure must never discard a teacher's successful business action.
  void post("/analytics/events", { event_name, properties }).catch(() => {});
}
export async function privateBlob(path: string, signal?: AbortSignal) {
  const relative = path.startsWith("/api/v1/")
    ? path.slice("/api/v1".length)
    : path;
  if (
    !relative.startsWith("/") ||
    relative.startsWith("//") ||
    relative.includes("://")
  )
    throw new Error("文件地址无效");
  const response = await fetch(`${API_BASE}${relative}`, {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  });
  if (!response.ok) throw new Error(`文件读取失败 (${response.status})`);
  return response.blob();
}
export async function download(path: string, filename: string) {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) throw new Error("下载失败，请重试");
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// SSE frames may be split at arbitrary UTF-8 and CRLF boundaries by a proxy.
export function parseFrame(frame: string): StreamEvent | null {
  let id = "",
    event = "message";
  const values: string[] = [];
  for (const line of frame.split(/\r?\n/)) {
    if (line.startsWith("id:")) id = line.slice(3).trim();
    else if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) values.push(line.slice(5).trimStart());
  }
  if (!values.length) return null;
  try {
    const data = JSON.parse(values.join("\n"));
    return {
      id,
      event: event === "message" ? data.type || data.event || event : event,
      data: data.payload ? { ...data, ...data.payload } : data,
    };
  } catch {
    return null;
  }
}
export async function watchRun(
  runId: string,
  signal: AbortSignal,
  onEvent: (event: StreamEvent) => void,
  onState: (run: Run) => void,
  onReconnect?: (reconnecting: boolean) => void,
) {
  let lastId = "",
    failures = 0;
  while (!signal.aborted) {
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    const connection = new AbortController();
    const abort = () => connection.abort();
    signal.addEventListener("abort", abort, { once: true });
    let silence: ReturnType<typeof setTimeout> | undefined;
    const armTimeout = () => {
      clearTimeout(silence);
      silence = setTimeout(() => connection.abort(), 50000);
    };
    try {
      const recovered = await api<Run>(`/generation-runs/${runId}`, { signal });
      onState(recovered);
      if (["completed", "failed", "cancelled"].includes(recovered.status))
        return;
      const response = await fetch(
        `${API_BASE}/generation-runs/${runId}/events`,
        {
          headers: {
            Authorization: `Bearer ${token}`,
            ...(lastId ? { "Last-Event-ID": lastId } : {}),
          },
          signal: connection.signal,
        },
      );
      if (!response.ok || !response.body) throw new Error("实时连接中断");
      onReconnect?.(false);
      failures = 0;
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      armTimeout();
      while (!signal.aborted) {
        const { done, value } = await reader.read();
        if (done) break;
        armTimeout();
        buffer += decoder.decode(value, { stream: true });
        let match: RegExpExecArray | null;
        while ((match = /\r?\n\r?\n/.exec(buffer))) {
          const frame = buffer.slice(0, match.index);
          buffer = buffer.slice(match.index + match[0].length);
          const event = parseFrame(frame);
          if (!event) continue;
          if (event.id && lastId && Number(event.id) <= Number(lastId))
            continue;
          if (event.id) lastId = event.id;
          onEvent(event);
          if (
            ["run_completed", "run_failed", "run_cancelled"].includes(
              event.event,
            )
          ) {
            const run = await api<Run>(`/generation-runs/${runId}`, { signal });
            onState(run);
            return;
          }
        }
      }
      if (signal.aborted) return;
      // A proxy can end a valid SSE response without throwing. Query the Run
      // before announcing reconnection so an already completed task stays quiet.
      const afterEof = await api<Run>(`/generation-runs/${runId}`, { signal });
      onState(afterEof);
      if (["completed", "failed", "cancelled"].includes(afterEof.status))
        return;
      onReconnect?.(true);
    } catch (error) {
      if (signal.aborted) return;
      if (error instanceof ApiError && [401, 403, 404].includes(error.status))
        throw error;
      failures++;
      onReconnect?.(true);
    } finally {
      clearTimeout(silence);
      signal.removeEventListener("abort", abort);
      await reader?.cancel().catch(() => {});
    }
    await new Promise<void>((resolve) => {
      const done = () => {
        clearTimeout(timer);
        signal.removeEventListener("abort", done);
        resolve();
      };
      const timer = setTimeout(
        done,
        Math.min(1000 * 2 ** Math.min(failures, 4), 10000),
      );
      signal.addEventListener("abort", done, { once: true });
    });
  }
}
