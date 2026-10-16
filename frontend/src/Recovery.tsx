import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  CheckCheck,
  ChevronLeft,
  ChevronRight,
  Clock3,
  RefreshCw,
  Search,
  ShieldCheck,
  X,
} from "lucide-react";
import { api, ApiError } from "./api";
import type {
  RecoveryAlert,
  RecoveryDetail,
  RecoveryEvent,
  RecoveryOverview,
  RecoveryPageResult,
  RecoveryRun,
  User,
} from "./types";
import { Empty, Modal, PageHeading, Spinner } from "./ui";

const PAGE_SIZE = 20;
const eventLabels: Record<string, string> = {
  dispatch_retry_scheduled: "投递失败，已安排自动重试",
  dispatch_published: "任务已投递",
  worker_recovering: "接管过期任务",
  checkpoint_restored: "恢复已保存断点",
  quality_retry_started: "质量校验重试",
  run_completed: "任务已完成",
  run_cancelled: "任务已取消",
  run_failed: "任务失败",
};
const statusLabels: Record<string, string> = {
  queued: "排队中",
  running: "执行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  pending: "等待重投",
  dispatching: "正在投递",
  checking: "正在对账",
  published: "已投递",
  exhausted: "预算耗尽",
  superseded: "已被新批次替代",
  open: "待处理",
  acknowledged: "已确认",
  resolved: "已关闭",
};
const time = (value?: string | null) =>
  value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—";
const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : "请求未完成，请稍后重试";
function State({ value }: { value: string }) {
  return (
    <span className={`recovery-state recovery-state-${value}`}>
      {statusLabels[value] || value}
    </span>
  );
}

// Schedule only after completion. Each scope owns its controller, so an old
// filter, detail or poll can never overwrite a newer view or survive unmount.
function useLiveResource<T>(
  path: string | null,
  automatic: boolean,
  revision: number,
) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const previousPath = useRef<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    if (previousPath.current !== path) {
      setData(null);
      setError("");
      previousPath.current = path;
    }
    if (!path) {
      setLoading(false);
      return;
    }
    const load = async () => {
      let permissionRevoked = false;
      setLoading(true);
      try {
        const value = await api<T>(path, { signal: controller.signal });
        if (!controller.signal.aborted) {
          setData(value);
          setError("");
        }
      } catch (failure) {
        if (!controller.signal.aborted) {
          setError(errorMessage(failure));
          if (
            failure instanceof ApiError &&
            [401, 403].includes(failure.status)
          ) {
            setData(null);
            permissionRevoked = true;
          }
        }
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
          if (automatic && !permissionRevoked)
            timer = setTimeout(() => void load(), 5000);
        }
      }
    };
    void load();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [path, automatic, revision]);
  return { data, error, loading };
}
function Paging({
  label,
  offset,
  total,
  change,
}: {
  label: string;
  offset: number;
  total: number;
  change: (value: number) => void;
}) {
  return (
    <div className="recovery-paging">
      <span>
        共 {total} 条 · 第 {Math.floor(offset / PAGE_SIZE) + 1} 页
      </span>
      <button
        className="icon-button"
        aria-label={`${label}上一页`}
        disabled={!offset}
        onClick={() => change(Math.max(0, offset - PAGE_SIZE))}
      >
        <ChevronLeft size={17} />
      </button>
      <button
        className="icon-button"
        aria-label={`${label}下一页`}
        disabled={offset + PAGE_SIZE >= total}
        onClick={() => change(offset + PAGE_SIZE)}
      >
        <ChevronRight size={17} />
      </button>
    </div>
  );
}
type Operation =
  | { kind: "retry" | "cancel"; run: RecoveryRun }
  | { kind: "acknowledge" | "resolve"; alert: RecoveryAlert };
const operationLabels = {
  retry: "立即重投",
  cancel: "取消任务",
  acknowledge: "确认告警",
  resolve: "关闭告警",
};

export function RecoveryPage({ user }: { user: User }) {
  if (user.role !== "admin")
    return (
      <div className="error-banner" role="alert">
        无管理端访问权限
      </div>
    );
  return <RecoveryWorkspace key={user.id} userId={user.id} />;
}
function RecoveryWorkspace({ userId }: { userId: string }) {
  const [automatic, setAutomatic] = useState(true);
  const [revision, setRevision] = useState(0);
  const [status, setStatus] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [attention, setAttention] = useState(false);
  const [offset, setOffset] = useState(0);
  const [alertStatus, setAlertStatus] = useState("open");
  const [alertOffset, setAlertOffset] = useState(0);
  const [selected, setSelected] = useState("");
  const [eventCursors, setEventCursors] = useState([0]);
  const [operation, setOperation] = useState<Operation | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const actionController = useRef<AbortController | null>(null);
  const poll = automatic && !operation;
  const query = new URLSearchParams({
    search,
    offset: String(offset),
    limit: String(PAGE_SIZE),
    attention_only: String(attention),
  });
  if (status) query.set("status", status);
  const alertQuery = new URLSearchParams({
    offset: String(alertOffset),
    limit: String(PAGE_SIZE),
  });
  if (alertStatus) alertQuery.set("status", alertStatus);
  const overview = useLiveResource<RecoveryOverview>(
    "/admin/recovery-overview",
    poll,
    revision,
  );
  const runs = useLiveResource<RecoveryPageResult<RecoveryRun>>(
    `/admin/recovery-runs?${query}`,
    poll,
    revision,
  );
  const alerts = useLiveResource<RecoveryPageResult<RecoveryAlert>>(
    `/admin/recovery-alerts?${alertQuery}`,
    poll,
    revision,
  );
  const after = eventCursors[eventCursors.length - 1];
  const detail = useLiveResource<RecoveryDetail>(
    selected
      ? `/admin/recovery-runs/${selected}?after=${after}&limit=50`
      : null,
    poll,
    revision,
  );
  useEffect(() => () => actionController.current?.abort(), []);
  const choose = (id: string) => {
    setSelected(id);
    setEventCursors([0]);
  };
  const startOperation = (value: Operation) => {
    setOperation(value);
    setReason("");
    setActionError("");
    setNotice("");
  };
  const execute = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!operation || busy || reason.trim().length < 4) return;
    const controller = new AbortController();
    actionController.current?.abort();
    actionController.current = controller;
    setBusy(true);
    setActionError("");
    const body =
      "run" in operation
        ? operation.kind === "retry"
          ? {
              expected_epoch: operation.run.execution_epoch,
              expected_attempts: operation.run.dispatch?.attempts ?? 0,
              reason: reason.trim(),
            }
          : {
              expected_epoch: operation.run.execution_epoch,
              reason: reason.trim(),
            }
        : { reason: reason.trim() };
    const path =
      "run" in operation
        ? `/admin/recovery-runs/${operation.run.id}/${operation.kind}`
        : `/admin/recovery-alerts/${operation.alert.id}/${operation.kind}`;
    try {
      await api(path, {
        method: "POST",
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      setNotice(`${operationLabels[operation.kind]}已提交，请查看最新记录。`);
      setOperation(null);
      setRevision((value) => value + 1);
    } catch (failure) {
      if (controller.signal.aborted) return;
      if (failure instanceof ApiError && failure.status === 409) {
        setOperation(null);
        setNotice(`${errorMessage(failure)}，已刷新，请重新核对后操作。`);
        setRevision((value) => value + 1);
      } else setActionError(errorMessage(failure));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  };
  const summary = detail.data?.run;
  const tiles: [keyof RecoveryOverview, string][] = [
    ["queued", "排队任务"],
    ["running", "正在执行"],
    ["retry_pending", "等待重投"],
    ["expired_leases", "租约过期"],
    ["open_alerts", "待处理告警"],
    ["failed", "失败任务"],
  ];
  return (
    <div className="page recovery-page">
      <PageHeading
        eyebrow="RECOVERY OBSERVATORY"
        title="任务恢复与告警"
        description="查看投递、执行与断点事实，让每一次恢复都有迹可循。"
        action={
          <button
            className="button secondary"
            onClick={() => setRevision((value) => value + 1)}
          >
            <RefreshCw size={16} />
            刷新数据
          </button>
        }
      />
      <div className="recovery-live-bar">
        <span>
          <ShieldCheck size={16} />
          管理员工作空间
        </span>
        <label>
          <input
            type="checkbox"
            checked={automatic}
            onChange={(event) => setAutomatic(event.target.checked)}
          />
          每 5 秒自动刷新
        </label>
        {runs.data?.server_time && (
          <small>数据时间 {time(runs.data.server_time)}</small>
        )}
      </div>
      {notice && (
        <div className="recovery-notice" role="status">
          {notice}
        </div>
      )}
      {overview.error && (
        <div className="error-banner" role="alert">
          概览：{overview.error}
        </div>
      )}
      <div className="recovery-metrics">
        {tiles.map(([key, title]) => (
          <article
            key={key}
            className={
              key === "open_alerts" || key === "expired_leases"
                ? "recovery-metric-attention"
                : ""
            }
          >
            <span>{title}</span>
            <b>{overview.data ? overview.data[key] : "—"}</b>
            <small>
              {key === "retry_pending"
                ? "遵循现有预算自动调度"
                : key === "expired_leases"
                  ? "等待接管与断点恢复"
                  : key === "open_alerts"
                    ? "需要核对运行事实"
                    : "来自任务持久化状态"}
            </small>
          </article>
        ))}
      </div>
      <section className="recovery-panel" aria-label="任务列表">
        <div className="recovery-section-heading">
          <div>
            <Activity size={19} />
            <h2>生成任务</h2>
          </div>
          <span>按创建时间查看</span>
        </div>
        <form
          className="recovery-filters"
          aria-label="任务筛选"
          onSubmit={(event) => {
            event.preventDefault();
            setSearch(searchInput.trim());
            setOffset(0);
          }}
        >
          <label>
            任务状态
            <select
              aria-label="任务状态"
              value={status}
              onChange={(event) => {
                setStatus(event.target.value);
                setOffset(0);
              }}
            >
              <option value="">全部状态</option>
              {["queued", "running", "completed", "failed", "cancelled"].map(
                (value) => (
                  <option key={value} value={value}>
                    {statusLabels[value]}
                  </option>
                ),
              )}
            </select>
          </label>
          <label className="recovery-search">
            课题或任务 ID
            <div>
              <Search size={16} />
              <input
                aria-label="搜索恢复任务"
                value={searchInput}
                onChange={(event) => setSearchInput(event.target.value)}
                placeholder="搜索课题或任务 ID"
              />
            </div>
          </label>
          <button className="button secondary" type="submit">
            查询
          </button>
          <label className="recovery-check">
            <input
              type="checkbox"
              checked={attention}
              onChange={(event) => {
                setAttention(event.target.checked);
                setOffset(0);
              }}
            />
            仅需关注
          </label>
        </form>
        {runs.error && (
          <div className="error-banner" role="alert">
            任务：{runs.error}
          </div>
        )}
        {runs.loading && !runs.data ? (
          <Spinner label="正在读取恢复任务" />
        ) : runs.data && !runs.data.items.length ? (
          <Empty title="没有符合条件的任务">调整筛选条件后查看。</Empty>
        ) : (
          runs.data && (
            <div className="table-scroll">
              <table className="recovery-table">
                <thead>
                  <tr>
                    <th>课题 / 任务</th>
                    <th>状态</th>
                    <th>投递</th>
                    <th>执行批次 / 恢复</th>
                    <th>模块断点</th>
                    <th>创建时间</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.data.items.map((run) => (
                    <tr
                      key={run.id}
                      className={run.id === selected ? "is-selected" : ""}
                    >
                      <td>
                        <b>{run.topic || run.workflow_type}</b>
                        <code>{run.id}</code>
                      </td>
                      <td>
                        <State value={run.status} />
                        {run.error_code && <small>{run.error_code}</small>}
                      </td>
                      <td>
                        {run.dispatch ? (
                          <>
                            <State value={run.dispatch.status} />
                            <small>
                              尝试 {run.dispatch.attempts} 次 · 下次{" "}
                              {time(run.dispatch.next_attempt_at)}
                            </small>
                          </>
                        ) : (
                          "—"
                        )}
                      </td>
                      <td>
                        <b>{run.execution_epoch}</b> / {run.recovery_count} 次
                      </td>
                      <td>{run.checkpoint_count}</td>
                      <td>{time(run.created_at)}</td>
                      <td>
                        <button
                          className="text-button"
                          aria-label={`查看任务 ${run.id}`}
                          onClick={() => choose(run.id)}
                        >
                          查看详情 <ArrowRight size={14} />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        )}
        <Paging
          label="任务"
          offset={offset}
          total={runs.data?.total || 0}
          change={setOffset}
        />
      </section>
      {selected && (
        <section
          className="recovery-panel recovery-detail"
          aria-label="任务恢复详情"
        >
          <div className="recovery-section-heading">
            <div>
              <Clock3 size={19} />
              <h2>任务恢复详情</h2>
            </div>
            <button
              className="icon-button"
              aria-label="关闭任务详情"
              onClick={() => choose("")}
            >
              <X size={18} />
            </button>
          </div>
          {detail.error && (
            <div className="error-banner" role="alert">
              详情：{detail.error}
            </div>
          )}
          {detail.loading && !detail.data ? (
            <Spinner label="正在读取断点与执行记录" />
          ) : (
            detail.data &&
            summary && (
              <>
                <div className="recovery-detail-top">
                  <div>
                    <h3>{summary.topic || summary.workflow_type}</h3>
                    <code>{summary.id}</code>
                    <p>
                      <State value={summary.status} />
                      执行批次 {summary.execution_epoch} · 已恢复{" "}
                      {summary.recovery_count} 次
                    </p>
                  </div>
                  <div className="recovery-actions">
                    <button
                      className="button secondary"
                      disabled={!summary.can_retry}
                      onClick={() =>
                        startOperation({ kind: "retry", run: summary })
                      }
                    >
                      立即重投
                    </button>
                    <button
                      className="button secondary"
                      disabled={!summary.can_cancel}
                      onClick={() =>
                        startOperation({ kind: "cancel", run: summary })
                      }
                    >
                      取消任务
                    </button>
                    {summary.draft_id && summary.user_id === userId && (
                      <Link
                        className="button secondary"
                        to={`/lessons/${summary.draft_id}`}
                      >
                        {["completed", "failed", "cancelled"].includes(
                          summary.status,
                        )
                          ? "前往教案重新生成"
                          : "查看教案"}
                        <ArrowRight size={15} />
                      </Link>
                    )}
                  </div>
                </div>
                <p className="recovery-explanation">
                  重投沿用累计预算与有效断点。有效执行中的任务不能抢占。
                  {summary.user_id === userId
                    ? "已结束任务请前往教案发起新的生成。"
                    : "其他教师的正文保持私有；如需重新生成，请联系教案拥有者。"}
                </p>
                <dl className="recovery-facts">
                  <div>
                    <dt>最近心跳</dt>
                    <dd>{time(summary.heartbeat_at)}</dd>
                  </div>
                  <div>
                    <dt>租约到期</dt>
                    <dd>{time(summary.lease_expires_at)}</dd>
                  </div>
                  <div>
                    <dt>开始 / 结束</dt>
                    <dd>
                      {time(summary.started_at)}
                      <br />
                      {time(summary.finished_at)}
                    </dd>
                  </div>
                  <div>
                    <dt>投递状态 / 次数</dt>
                    <dd>
                      {summary.dispatch
                        ? `${statusLabels[summary.dispatch.status] || summary.dispatch.status} / ${summary.dispatch.attempts}`
                        : "—"}
                    </dd>
                  </div>
                  <div>
                    <dt>下次投递 / 最后尝试</dt>
                    <dd>
                      {time(summary.dispatch?.next_attempt_at)}
                      <br />
                      {time(summary.dispatch?.last_attempt_at)}
                    </dd>
                  </div>
                  <div>
                    <dt>投递截止 / 最后错误</dt>
                    <dd>
                      {time(summary.dispatch?.deadline_at)}
                      <br />
                      {summary.dispatch?.last_error_code || "—"}
                    </dd>
                  </div>
                </dl>
                {detail.data.budget && (
                  <div className="recovery-budget">
                    <h3>累计执行预算</h3>
                    <p>
                      模型调用{" "}
                      <b>
                        {detail.data.budget.calls_started} /{" "}
                        {detail.data.budget.max_calls}
                      </b>
                      <span>
                        累计耗时{" "}
                        <b>
                          {Math.round(detail.data.budget.elapsed_seconds)} /{" "}
                          {detail.data.budget.run_timeout_seconds} 秒
                        </b>
                      </span>
                      <span>
                        每单元额外质量修正{" "}
                        <b>{detail.data.budget.quality_retry_limit} 次</b>
                      </span>
                    </p>
                    {Object.entries(detail.data.budget.attempt_counts).length >
                      0 && (
                      <div className="recovery-attempts">
                        {Object.entries(detail.data.budget.attempt_counts).map(
                          ([key, count]) => (
                            <span key={key}>
                              <code>{key}</code> × {count}
                            </span>
                          ),
                        )}
                      </div>
                    )}
                  </div>
                )}
                <h3 className="recovery-subheading">
                  已保存断点 <span>{detail.data.checkpoints.length}</span>
                </h3>
                {detail.data.checkpoints.length ? (
                  <div className="table-scroll">
                    <table className="recovery-table recovery-checkpoint-table">
                      <thead>
                        <tr>
                          <th>断点 / ID</th>
                          <th>输入指纹</th>
                          <th>内容指纹</th>
                          <th>保存时间</th>
                        </tr>
                      </thead>
                      <tbody>
                        {detail.data.checkpoints.map((checkpoint) => (
                          <tr key={checkpoint.id}>
                            <td>
                              <b>{checkpoint.key}</b>
                              <code>{checkpoint.id}</code>
                            </td>
                            <td>
                              <code>{checkpoint.input_fingerprint}</code>
                            </td>
                            <td>
                              <code>{checkpoint.content_hash}</code>
                            </td>
                            <td>{time(checkpoint.created_at)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <p className="recovery-muted">尚无持久化断点。</p>
                )}
                <h3 className="recovery-subheading">
                  模型调用记录 <span>{detail.data.calls.length}</span>
                </h3>
                {detail.data.calls.length ? (
                  <div className="table-scroll">
                    <table className="recovery-table">
                      <thead>
                        <tr>
                          <th>调用 / 模型</th>
                          <th>执行结果</th>
                          <th>校验结果</th>
                          <th>执行批次</th>
                          <th>时间</th>
                        </tr>
                      </thead>
                      <tbody>
                        {detail.data.calls.map((call) => (
                          <tr key={call.id}>
                            <td>
                              <b>{call.model_name || "待确定"}</b>
                              <code>{call.call_id || call.id}</code>
                              <small>{call.provider || "—"}</small>
                            </td>
                            <td>{call.call_outcome || call.status || "—"}</td>
                            <td>
                              {call.validation_outcome || "—"}
                              <small>
                                {call.validation_codes?.join(" · ")}
                              </small>
                            </td>
                            <td>{call.execution_epoch ?? "—"}</td>
                            <td>{time(call.created_at)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <p className="recovery-muted">
                    尚无模型调用。排队任务不会消耗模型预算。
                  </p>
                )}
                <h3 className="recovery-subheading">
                  持久化事件 <span>{detail.data.event_total}</span>
                </h3>
                <ol className="recovery-timeline">
                  {detail.data.events.map((event) => (
                    <EventItem key={event.sequence} event={event} />
                  ))}
                </ol>
                {!detail.data.events.length && (
                  <p className="recovery-muted">当前页暂无事件。</p>
                )}
                <div className="recovery-event-paging">
                  <button
                    className="button secondary"
                    disabled={eventCursors.length === 1}
                    onClick={() =>
                      setEventCursors((values) => values.slice(0, -1))
                    }
                  >
                    上一页事件
                  </button>
                  <span>
                    序号 {detail.data.events[0]?.sequence ?? "—"} —{" "}
                    {detail.data.events.at(-1)?.sequence ?? "—"}
                  </span>
                  <button
                    className="button secondary"
                    disabled={detail.data.events.length < 50}
                    onClick={() =>
                      setEventCursors((values) => [
                        ...values,
                        detail.data!.events.at(-1)!.sequence,
                      ])
                    }
                  >
                    后续事件
                  </button>
                </div>
                {detail.data.alerts.length > 0 && (
                  <p className="recovery-explanation">
                    关联告警：
                    {detail.data.alerts
                      .map(
                        (alert) =>
                          `${alert.code}（${statusLabels[alert.status]}）`,
                      )
                      .join("、")}
                  </p>
                )}
              </>
            )
          )}
        </section>
      )}
      <section className="recovery-panel" aria-label="恢复告警">
        <div className="recovery-section-heading">
          <div>
            <AlertTriangle size={19} />
            <h2>恢复告警</h2>
          </div>
          <label>
            告警状态
            <select
              aria-label="告警状态"
              value={alertStatus}
              onChange={(event) => {
                setAlertStatus(event.target.value);
                setAlertOffset(0);
              }}
            >
              <option value="">全部状态</option>
              <option value="open">待处理</option>
              <option value="acknowledged">已确认</option>
              <option value="resolved">已关闭</option>
            </select>
          </label>
        </div>
        <p className="recovery-explanation">
          同一异常累计记录发生次数；条件恢复后自动关闭。确认表示已接手处理，关闭告警不会改变任务状态。
        </p>
        {alerts.error && (
          <div className="error-banner" role="alert">
            告警：{alerts.error}
          </div>
        )}
        {alerts.loading && !alerts.data ? (
          <Spinner label="正在读取告警" />
        ) : alerts.data && !alerts.data.items.length ? (
          <Empty title="当前没有此类告警">
            恢复过程中的新异常会记录在这里。
          </Empty>
        ) : (
          alerts.data && (
            <div className="table-scroll">
              <table className="recovery-table">
                <thead>
                  <tr>
                    <th>告警 / 任务</th>
                    <th>状态 / 级别</th>
                    <th>次数</th>
                    <th>首次 / 最近发生</th>
                    <th>确认 / 关闭时间</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {alerts.data.items.map((alert) => (
                    <tr key={alert.id}>
                      <td>
                        <b>{alert.code}</b>
                        <p>{alert.message}</p>
                        {alert.run_id && (
                          <button
                            className="text-button recovery-run-link"
                            aria-label={`查看告警任务 ${alert.run_id}`}
                            onClick={() => choose(alert.run_id!)}
                          >
                            {alert.run_id}
                          </button>
                        )}
                      </td>
                      <td>
                        <State value={alert.status} />
                        <small>{alert.severity}</small>
                      </td>
                      <td>{alert.occurrence_count}</td>
                      <td>
                        {time(alert.first_seen_at)}
                        <small>{time(alert.last_seen_at)}</small>
                      </td>
                      <td>
                        {time(alert.acknowledged_at)}
                        <small>{time(alert.resolved_at)}</small>
                      </td>
                      <td>
                        <div className="recovery-alert-actions">
                          <button
                            className="text-button"
                            aria-label={`确认告警 ${alert.id}`}
                            disabled={alert.status !== "open"}
                            onClick={() =>
                              startOperation({ kind: "acknowledge", alert })
                            }
                          >
                            <CheckCheck size={15} />
                            确认
                          </button>
                          <button
                            className="text-button"
                            aria-label={`关闭告警 ${alert.id}`}
                            disabled={alert.status === "resolved"}
                            onClick={() =>
                              startOperation({ kind: "resolve", alert })
                            }
                          >
                            关闭
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        )}
        <Paging
          label="告警"
          offset={alertOffset}
          total={alerts.data?.total || 0}
          change={setAlertOffset}
        />
      </section>
      {operation && (
        <Modal
          title={operationLabels[operation.kind]}
          onClose={() => {
            if (!busy) setOperation(null);
          }}
        >
          <form
            className="recovery-operation"
            onSubmit={(event) => void execute(event)}
          >
            <p>
              {"run" in operation
                ? `${operation.run.topic || operation.run.id} · 执行批次 ${operation.run.execution_epoch}`
                : `${operation.alert.code} · 累计 ${operation.alert.occurrence_count} 次`}
            </p>
            <p className="recovery-explanation">
              {"run" in operation
                ? operation.kind === "retry"
                  ? "立即安排一次符合当前预算的重投，不会重置调用和恢复次数。"
                  : "终止当前任务并撤销执行权，之后不能恢复此任务。"
                : operation.kind === "acknowledge"
                  ? "记录你已接手处理此告警，异常仍会持续监测。"
                  : "记录关闭原因并保留告警历史；若异常再次发生，会重新打开。"}
            </p>
            <label>
              操作原因
              <textarea
                aria-label="操作原因"
                autoComplete="off"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                minLength={4}
                maxLength={300}
                required
                placeholder="记录排查结论或操作依据，至少 4 个字"
              />
            </label>
            {actionError && (
              <div className="error-banner" role="alert">
                {actionError}
              </div>
            )}
            <div className="modal-actions">
              <button
                className="button secondary"
                type="button"
                disabled={busy}
                onClick={() => setOperation(null)}
              >
                返回
              </button>
              <button
                className="button primary"
                disabled={busy || reason.trim().length < 4}
              >
                {busy
                  ? "正在提交…"
                  : operation.kind === "acknowledge"
                    ? "确认告警"
                    : `确认${operationLabels[operation.kind]}`}
              </button>
            </div>
          </form>
        </Modal>
      )}
    </div>
  );
}
function EventItem({ event }: { event: RecoveryEvent }) {
  event = { ...event, ...event.payload };
  return (
    <li>
      <span className="recovery-event-sequence">#{event.sequence}</span>
      <div>
        <b>
          {eventLabels[event.phase || event.event_type] ||
            event.phase ||
            event.event_type}
        </b>
        <code>
          {event.event_type}
          {event.phase ? ` · ${event.phase}` : ""}
        </code>
        <p>
          {[
            event.module_id && `模块 ${event.module_id}`,
            event.checkpoint_key && `断点 ${event.checkpoint_key}`,
            event.attempt !== undefined &&
              `尝试 ${event.attempt}${event.max_attempts ? ` / ${event.max_attempts}` : ""}`,
            event.execution_epoch !== undefined &&
              `执行批次 ${event.execution_epoch}`,
            event.code,
          ]
            .filter(Boolean)
            .join(" · ")}
        </p>
      </div>
      <time>{time(event.created_at)}</time>
    </li>
  );
}
