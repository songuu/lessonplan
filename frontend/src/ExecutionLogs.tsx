import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  ArrowLeft,
  Check,
  Copy,
  RefreshCw,
  Search,
  ShieldCheck,
} from "lucide-react";
import { api, ApiError } from "./api";
import type {
  ExecutionLog,
  ExecutionLogPage,
  ObservabilityStatus,
  User,
} from "./types";
import { Empty, Modal, PageHeading, Spinner } from "./ui";

const levels = { info: "信息", warning: "警告", error: "错误" };
const statuses = {
  running: "执行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  lease_lost: "执行权已转移",
  interrupted: "中断 / 结果待确认",
};
const workflows: Record<string, string> = {
  one_click: "一键生成",
  interactive_step: "结构化备课",
  interactive_finalize: "教案定稿",
  module_polish_message: "模块润色",
  reflection_polish_message: "反思润色",
  lesson_internal_review: "教案审查",
  document_parse: "教案解析",
  knowledge_document_parse: "知识入库",
  export: "教案导出",
};
const phases = { start: "开始", end: "结束", event: "事件" };
const time = (value: string | null) =>
  value && !Number.isNaN(Date.parse(value))
    ? new Date(value).toLocaleString("zh-CN", { hour12: false })
    : "—";
const errorText = (error: unknown) =>
  error instanceof Error ? error.message : "执行日志读取失败，请稍后重试";
const isReuse = (log: ExecutionLog) =>
  log.metadata.receipt_reused === true ||
  log.metadata.receipt_replayed === true ||
  log.metadata.replayed === true ||
  /receipt[._](replay|reuse)/.test(log.name);

// Metadata is an allowlist even for administrators: prompts, exception text,
// response bodies and remote URLs must not become an accidental log viewer.
const textFacts = [
  ["模型", ["model_name", "ls_model_name", "model"]],
  ["Provider", ["provider", "ls_provider"]],
  ["错误代码", ["error_code"]],
  ["调用 ID", ["call_id"]],
  ["回执 ID", ["invocation_id"]],
  ["调用意图", ["invocation_key"]],
  ["步骤", ["step_key"]],
  ["路由", ["route"]],
  ["传输结果", ["transport_outcome"]],
  ["校验结果", ["validation_outcome"]],
] as const;
const numericFacts = [
  ["尝试次数", ["attempt"]],
  ["质量尝试", ["quality_attempt"]],
  ["原始采集批次", ["capture_epoch"]],
] as const;
const usageFacts = [
  ["输入 token", ["prompt_tokens", "input_tokens"]],
  ["输出 token", ["completion_tokens", "output_tokens"]],
  ["总 token", ["total_tokens"]],
  ["首字耗时", ["first_token_ms", "ttft_ms"]],
] as const;
function safeText(value: unknown) {
  return typeof value === "string" &&
    /^[\p{L}\p{N}_.:/@+ -]{1,180}$/u.test(value)
    ? value
    : null;
}
function detailFacts(log: ExecutionLog): [string, string][] {
  const facts: [string, string][] = [];
  for (const [label, keys] of textFacts) {
    const value = keys.map((key) => safeText(log.metadata[key])).find(Boolean);
    if (value) facts.push([label, value]);
  }
  for (const [label, keys] of [
    ...numericFacts,
    ...(isReuse(log) ? [] : usageFacts),
  ]) {
    const value = keys
      .map((key) => log.metadata[key])
      .find(
        (item) =>
          typeof item === "number" && Number.isFinite(item) && item >= 0,
      );
    if (typeof value === "number")
      facts.push([label, label === "首字耗时" ? `${value} ms` : String(value)]);
  }
  const codes = log.metadata.validation_codes;
  if (Array.isArray(codes)) {
    const allowed = codes.slice(0, 20).map(safeText).filter(Boolean);
    if (allowed.length) facts.push(["校验代码", allowed.join("、")]);
  }
  return facts;
}

function useExecutionLogs(query: string, revision: number, automatic: boolean) {
  const [items, setItems] = useState<ExecutionLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [hasMore, setHasMore] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const [accessRevoked, setAccessRevoked] = useState(false);
  const request = useRef<(() => Promise<void>) | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    let cursor = 0;
    let pending = false;
    let forbidden = false;
    let initial = true;
    setItems([]);
    setError("");
    setHasMore(false);
    setUpdatedAt(null);
    setAccessRevoked(false);
    const load = async () => {
      if (pending || forbidden || controller.signal.aborted) return;
      pending = true;
      setLoading(true);
      const params = new URLSearchParams(query);
      params.set("after", String(cursor));
      params.set("limit", "50");
      try {
        const page = await api<ExecutionLogPage>(
          `/admin/execution-logs?${params}`,
          { signal: controller.signal },
        );
        if (controller.signal.aborted) return;
        // Keep previously loaded history when polling or loading more. The
        // event identity also absorbs a retried page without duplicate rows.
        const replace = initial;
        initial = false;
        setItems((previous) =>
          Array.from(
            new Map(
              [...(replace ? [] : previous), ...page.items].map((log) => [
                log.event_id,
                log,
              ]),
            ).values(),
          ).sort((a, b) => a.sequence - b.sequence),
        );
        cursor = Math.max(cursor, page.next_after);
        setHasMore(page.has_more);
        setError("");
        setUpdatedAt(new Date().toISOString());
      } catch (failure) {
        if (!controller.signal.aborted) {
          setError(errorText(failure));
          if (
            failure instanceof ApiError &&
            [401, 403].includes(failure.status)
          ) {
            forbidden = true;
            setAccessRevoked(true);
            setItems([]);
            setHasMore(false);
          }
        }
      } finally {
        pending = false;
        if (!controller.signal.aborted) setLoading(false);
      }
    };
    request.current = load;
    void load();
    return () => {
      controller.abort();
      if (request.current === load) request.current = null;
    };
  }, [query, revision]);
  useEffect(() => {
    if (!automatic || accessRevoked) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await request.current?.();
      if (active) timer = setTimeout(() => void poll(), 5000);
    };
    timer = setTimeout(() => void poll(), 5000);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [automatic, accessRevoked, query, revision]);
  return {
    items,
    loading,
    error,
    hasMore,
    updatedAt,
    accessRevoked,
    more: () => void request.current?.(),
  };
}

function LogDetail({ log, close }: { log: ExecutionLog; close: () => void }) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState("");
  const replay = isReuse(log);
  const normalizedTrace = (log.trace_id || "").replace(/-/g, "").toLowerCase();
  const langfuseTraceId = /^[a-f0-9]{32}$/.test(normalizedTrace)
    ? normalizedTrace
    : "";
  return (
    <Modal title="执行日志详情" onClose={close} wide>
      <div className="modal-body execution-log-detail">
        <div className="execution-detail-heading">
          <div>
            <span className="eyebrow">
              {phases[log.phase]} · {log.run_type}
            </span>
            <h3>{log.name}</h3>
          </div>
          <span className={`recovery-state recovery-state-${log.status}`}>
            {statuses[log.status]}
          </span>
        </div>
        {replay && (
          <p className="recovery-explanation">
            回执复用：继续处理已保存的响应，不新增模型用量。
          </p>
        )}
        <dl className="recovery-facts">
          {[
            ["任务 ID", log.run_id],
            ["执行批次", String(log.execution_epoch)],
            ["本地 Trace ID", log.trace_id || "—"],
            ["Langfuse Trace ID", langfuseTraceId || "—"],
            ["Span ID", log.span_id || "—"],
            ["父 Span ID", log.parent_span_id || "—"],
            [
              "工作流",
              workflows[log.workflow_type] || log.workflow_type || "—",
            ],
            ["开始时间", time(log.started_at)],
            ["结束时间", time(log.ended_at)],
            [
              "耗时",
              log.elapsed_ms === null ? "尚未完成" : `${log.elapsed_ms} ms`,
            ],
            ...detailFacts(log),
          ].map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
        {log.run_type === "llm" &&
          !replay &&
          log.metadata.usage_known === false && (
            <p className="recovery-explanation">
              实际模型用量未知，未按零用量统计。
            </p>
          )}
        <div className="recovery-actions">
          <button
            className="button secondary"
            disabled={!langfuseTraceId}
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(langfuseTraceId);
                setCopied(true);
                setCopyError("");
              } catch {
                setCopyError("复制失败，请选中 Langfuse Trace ID 手动复制。");
              }
            }}
          >
            {copied ? <Check size={15} /> : <Copy size={15} />}
            {copied ? "Langfuse Trace ID 已复制" : "复制 Langfuse Trace ID"}
          </button>
          <Link
            className="button secondary"
            to={`/admin/recovery?run_id=${encodeURIComponent(log.run_id)}`}
          >
            查看任务恢复
          </Link>
        </div>
        {copyError && (
          <p role="status" className="recovery-muted">
            {copyError}
          </p>
        )}
        <p className="execution-privacy-note">
          <ShieldCheck size={14} />
          仅展示运行标识与受控指标；提示词、正文和原始异常不在此展示。
        </p>
      </div>
    </Modal>
  );
}

export function ExecutionLogsPage({ user }: { user: User }) {
  if (user.role !== "admin")
    return (
      <div className="error-banner" role="alert">
        无管理端访问权限
      </div>
    );
  return <ExecutionLogsWorkspace key={user.id} />;
}
function ExecutionLogsWorkspace() {
  const [filters, setFilters] = useState({
    run_id: "",
    level: "",
    status: "",
    workflow: "",
  });
  const [query, setQuery] = useState("");
  const [revision, setRevision] = useState(0);
  const [automatic, setAutomatic] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [platform, setPlatform] = useState<ObservabilityStatus | null>(null);
  const [platformError, setPlatformError] = useState(false);
  const logs = useExecutionLogs(query, revision, automatic);
  useEffect(() => {
    const controller = new AbortController();
    setPlatformError(false);
    api<ObservabilityStatus>("/admin/observability", {
      signal: controller.signal,
    })
      .then((value) => {
        if (!controller.signal.aborted) setPlatform(value);
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setPlatform(null);
          setPlatformError(true);
        }
      });
    return () => controller.abort();
  }, [revision]);
  useEffect(() => {
    setSelected(null);
  }, [query, revision]);
  const log = logs.accessRevoked
    ? null
    : logs.items.find((item) => item.event_id === selected);
  const platformLabel = platformError
    ? "追踪平台状态暂时不可用"
    : !platform
      ? "正在读取追踪平台状态"
      : platform.configured === false
        ? "Langfuse 未配置"
        : platform.enabled === false
          ? "Langfuse 追踪已关闭"
          : platform.enabled === true && platform.configured === true
            ? "Langfuse 已配置"
            : "Langfuse 状态待确认";
  return (
    <div className="page recovery-page execution-logs-page">
      <PageHeading
        eyebrow="EXECUTION OBSERVATORY"
        title="后端执行日志"
        description="按时间追踪任务步骤、实际模型请求与恢复行为。"
        action={
          <div className="recovery-actions">
            <Link className="button secondary" to="/admin">
              <ArrowLeft size={16} />
              管理控制台
            </Link>
            <button
              className="button primary"
              onClick={() => setRevision((value) => value + 1)}
            >
              <RefreshCw size={16} />
              刷新日志
            </button>
          </div>
        }
      />
      <div className="execution-observability-status" aria-label="监控状态">
        <span
          className={`execution-platform-dot ${platform?.enabled && platform?.configured && !platformError ? "is-enabled" : ""}`}
        />
        <div>
          <b>{platformLabel}</b>
          <p>本地执行日志独立保存。平台配置状态不代表追踪已经成功送达。</p>
        </div>
        {platform?.project && safeText(platform.project) && (
          <span className="execution-project">
            项目 {safeText(platform.project)}
          </span>
        )}
      </div>
      <div className="recovery-live-bar">
        <span>
          <Activity size={15} />
          按事件顺序显示
        </span>
        <label>
          <input
            aria-label="自动刷新日志"
            type="checkbox"
            checked={automatic}
            disabled={logs.accessRevoked}
            onChange={(event) => setAutomatic(event.target.checked)}
          />
          自动刷新（每 5 秒）
        </label>
        <span>
          {automatic && !logs.accessRevoked ? "自动刷新已开启" : "手动刷新"}
        </span>
        <small>
          {logs.updatedAt ? `最近更新 ${time(logs.updatedAt)}` : "尚未取得日志"}
        </small>
      </div>
      <section className="recovery-panel">
        <div className="recovery-section-heading">
          <div>
            <Activity size={19} />
            <h2>执行时间线</h2>
          </div>
          <span>已加载 {logs.items.length} 条</span>
        </div>
        <form
          className="execution-log-filters"
          onSubmit={(event) => {
            event.preventDefault();
            const params = new URLSearchParams();
            for (const [key, value] of Object.entries(filters))
              if (value.trim()) params.set(key, value.trim());
            const next = params.toString();
            if (next === query) setRevision((value) => value + 1);
            else setQuery(next);
          }}
        >
          <label className="execution-log-run-filter">
            任务 ID
            <div>
              <Search size={15} />
              <input
                aria-label="任务 ID"
                value={filters.run_id}
                onChange={(event) =>
                  setFilters({ ...filters, run_id: event.target.value })
                }
                placeholder="输入完整任务 ID"
                maxLength={128}
              />
            </div>
          </label>
          <label>
            日志级别
            <select
              aria-label="日志级别"
              value={filters.level}
              onChange={(event) =>
                setFilters({ ...filters, level: event.target.value })
              }
            >
              <option value="">全部级别</option>
              {Object.entries(levels).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label>
            执行状态
            <select
              aria-label="执行状态"
              value={filters.status}
              onChange={(event) =>
                setFilters({ ...filters, status: event.target.value })
              }
            >
              <option value="">全部状态</option>
              {Object.entries(statuses).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label>
            工作流
            <select
              aria-label="工作流"
              value={filters.workflow}
              onChange={(event) =>
                setFilters({ ...filters, workflow: event.target.value })
              }
            >
              <option value="">全部工作流</option>
              {Object.entries(workflows).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <button className="button secondary" type="submit">
            应用筛选
          </button>
        </form>
        {logs.error && (
          <div className="error-banner" role="alert">
            {logs.error}
            <button
              className="text-button"
              onClick={() => setRevision((value) => value + 1)}
              aria-label="重试日志读取"
            >
              重试
            </button>
          </div>
        )}
        {logs.loading && !logs.items.length ? (
          <Spinner label="正在读取执行日志" />
        ) : !logs.items.length && !logs.error ? (
          <Empty title="暂无执行日志">
            当前筛选下没有记录。新任务开始后，可刷新查看执行过程。
          </Empty>
        ) : logs.items.length > 0 ? (
          <>
            <ol className="execution-log-timeline" aria-label="执行日志时间线">
              {logs.items.map((item) => (
                <li key={item.event_id} data-log-event={item.event_id}>
                  <span
                    className={`execution-log-level execution-log-level-${item.level}`}
                    aria-label={levels[item.level]}
                  />
                  <div className="execution-log-time">
                    <time dateTime={item.created_at}>
                      {time(item.created_at)}
                    </time>
                    <small>
                      #{item.sequence} · {phases[item.phase]}
                    </small>
                  </div>
                  <div className="execution-log-summary">
                    <button
                      className="text-button"
                      aria-label={`查看日志 ${item.event_id}`}
                      onClick={() => setSelected(item.event_id)}
                    >
                      {item.name}
                    </button>
                    <p>
                      <code>{item.run_id}</code>
                      <span>批次 {item.execution_epoch}</span>
                      <span>
                        {workflows[item.workflow_type] || item.workflow_type}
                      </span>
                    </p>
                  </div>
                  <div className="execution-log-outcome">
                    <span
                      className={`recovery-state recovery-state-${item.status}`}
                    >
                      {statuses[item.status]}
                    </span>
                    <small>
                      {isReuse(item)
                        ? "回执复用"
                        : item.run_type === "llm"
                          ? "实际模型请求"
                          : "执行步骤"}
                      {item.elapsed_ms !== null && ` · ${item.elapsed_ms} ms`}
                    </small>
                  </div>
                </li>
              ))}
            </ol>
            <div className="execution-log-paging">
              <span>
                {logs.hasMore
                  ? "还有更多日志，按顺序继续加载"
                  : "已加载到最新记录"}
              </span>
              <button
                className="button secondary"
                disabled={logs.loading || !logs.hasMore}
                onClick={logs.more}
              >
                {logs.loading ? "正在加载…" : "加载更多日志"}
              </button>
            </div>
          </>
        ) : null}
      </section>
      {log && (
        <LogDetail
          key={log.event_id}
          log={log}
          close={() => setSelected(null)}
        />
      )}
    </div>
  );
}
