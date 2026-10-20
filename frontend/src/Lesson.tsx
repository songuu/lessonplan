import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import {
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  BookOpen,
  Check,
  CheckCheck,
  ChevronRight,
  Clock3,
  Download,
  Eye,
  FileClock,
  FileText,
  History,
  MessageSquare,
  Plus,
  RotateCcw,
  Save,
  Send,
  Sparkles,
  Square,
  Trash2,
  X,
} from "lucide-react";
import { api, download, patch, post, track, watchRun } from "./api";
import { applySelectionCandidate, renderMarkdown } from "./content";
import { displayValue } from "./catalog";
import { RichEditor } from "./Editor";
import type { Selection } from "./Editor";
import type {
  Candidate,
  Draft,
  LessonModule,
  OutlineStage,
  Run,
} from "./types";
import { terminal } from "./types";
import { Empty, Modal, Spinner, Status, useNotify } from "./ui";

type Version = {
  id: string;
  version_no?: number;
  source?: string;
  created_at?: string;
  teacher_view_content: string;
};
type EditBase = {
  contentVersion: number;
  moduleVersion: string | null;
  configVersion: string;
};
type Polish = {
  id: string;
  moduleId?: string;
  base: string;
  selection: Selection | null;
  candidate: string;
  instruction: string;
};
type LiveModulePhase =
  "generating" | "generated" | "checking" | "completed" | "retrying";
const liveModuleLabels: Record<LiveModulePhase, string> = {
  generating: "正在生成",
  generated: "正文已生成",
  checking: "正在检查",
  completed: "已保存",
  retrying: "正在重试",
};
const message = (e: unknown) =>
  e instanceof Error ? e.message : "操作未成功，请重试";
function textCandidate(value: unknown): string {
  if (typeof value === "string") return value;
  if (value && typeof value === "object") {
    const c = value as Candidate;
    return c.replacement_text || c.teacher_view_content || c.content || "";
  }
  return "";
}
function contentOfEvent(data: Record<string, unknown>) {
  return String(
    data.delta ?? data.content ?? data.text ?? data.teacher_view_content ?? "",
  );
}
function stateLabel(module: LessonModule) {
  const state = module.state || {};
  if (state.freshness === "stale" || state.freshness === "unknown")
    return state.freshness;
  if (state.confirmation_status === "confirmed") return "confirmed";
  if (
    state.generation_status === "ready" &&
    state.confirmation_status === "unconfirmed"
  )
    return "awaiting_confirmation";
  return state.generation_status || "pending";
}

export function LessonPage() {
  const { id = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const notify = useNotify();
  const [draft, setDraft] = useState<Draft | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [working, setWorking] = useState("");
  const [base, setBase] = useState("");
  const [editBase, setEditBase] = useState<EditBase | null>(null);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [polishInstruction, setPolishInstruction] = useState("");
  const [busy, setBusy] = useState(false);
  const [runId, setRunId] = useState(params.get("run") || "");
  const runIdRef = useRef(runId);
  runIdRef.current = runId;
  const completedRunIdRef = useRef("");
  const [run, setRun] = useState<Run | null>(null);
  const [terminalReadbackRunId, setTerminalReadbackRunId] = useState("");
  const [watchRevision, setWatchRevision] = useState(0);
  const aiActionPending = useRef(false);
  const autoGenerationAttempts = useRef(new Set<string>());
  const [reconnecting, setReconnecting] = useState(false);
  const [streams, setStreams] = useState<Record<string, string>>({});
  const [progress, setProgress] = useState("正在准备教学设计");
  const [activeStage, setActiveStage] = useState("");
  const activeStageModule = useRef("");
  const [activeModuleId, setActiveModuleId] = useState("");
  const [modulePhases, setModulePhases] = useState<
    Record<string, LiveModulePhase>
  >({});
  const [followGeneration, setFollowGeneration] = useState(true);
  const followGenerationRef = useRef(true);
  const [runKind, setRunKind] = useState<
    "generation" | "polish" | "reflection"
  >("generation");
  const [versions, setVersions] = useState<Version[] | null>(null);
  const [polish, setPolish] = useState<Polish | null>(null);
  const [polishInput, setPolishInput] = useState("");
  const [preview, setPreview] = useState<string | null>(null);
  const [exportState, setExportState] = useState<{
    id: string;
    format: string;
    status: string;
  } | null>(null);
  const [outline, setOutline] = useState<OutlineStage[]>([]);
  const [outlineDirty, setOutlineDirty] = useState(false);
  const [reflectionOpen, setReflectionOpen] = useState(false);
  const [reflection, setReflection] = useState("");
  const [reflectionBase, setReflectionBase] = useState("");
  const [reflectionVersion, setReflectionVersion] = useState<
    number | string | undefined
  >();
  const [reflectionCandidate, setReflectionCandidate] = useState("");
  const [reflectionLoaded, setReflectionLoaded] = useState(false);
  const module = draft?.modules?.find((m) => m.module_id === selectedId);
  const dirty = working !== base;
  // A bookmarked terminal Run must not unlock a different active task on this draft.
  const running =
    (!!runId && (!run || run.id !== runId || !terminal(run.status))) ||
    (!!draft?.active_run_id && draft.active_run_id !== run?.id);
  const interactive = ["interactive", "interactive_step"].includes(
    draft?.generation_mode || "",
  );
  const enabled = draft?.modules.filter((m) => m.enabled) || [];
  const selectedIndex = enabled.findIndex(
    (item) => item.module_id === selectedId,
  );
  const nextModule =
    selectedIndex >= 0 ? enabled[selectedIndex + 1] : undefined;
  const moduleConfirmed = module?.state?.confirmation_status === "confirmed";
  const moduleReadOnly =
    interactive &&
    selectedIndex >= 0 &&
    enabled
      .slice(0, selectedIndex)
      .some((item) => item.state?.confirmation_status !== "confirmed");
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const selectedRef = useRef(selectedId);
  selectedRef.current = selectedId;
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  const polishRef = useRef(polish);
  polishRef.current = polish;
  const kindRef = useRef(runKind);
  kindRef.current = runKind;
  const hydrateEditor = useCallback(
    (detail: Draft, target: string, force = false) => {
      const m = detail.modules?.find((m) => m.module_id === target);
      if (!m) return;
      if (dirtyRef.current && !force) return;
      const official = m.teacher_view_content || "";
      let text = official;
      let restoredBase: EditBase | null = null;
      try {
        const copy = JSON.parse(
          sessionStorage.getItem(`lesson-copy:${id}:${target}`) || "null",
        );
        if (!force && copy && typeof copy.content === "string") {
          text = copy.content;
          restoredBase = {
            contentVersion:
              copy.moduleVersion === m.current_version_id
                ? detail.content_version
                : (copy.contentVersion ?? detail.content_version),
            moduleVersion: copy.moduleVersion,
            configVersion:
              copy.configVersion ?? detail.module_config_version_id,
          };
          if (copy.moduleVersion !== m.current_version_id) {
            setError(
              "服务端已有新版本，已保留本地工作副本。请复制需要的文字，或恢复已保存正文后重新修改。",
            );
          }
        }
      } catch {
        /* A malformed local copy cannot replace authoritative content. */
      }
      setWorking(text);
      setBase(official);
      setEditBase(
        restoredBase || {
          contentVersion: detail.content_version,
          moduleVersion: m.current_version_id,
          configVersion: detail.module_config_version_id,
        },
      );
      setPolishInstruction("");
      setSelection(null);
    },
    [id],
  );
  const focusGeneratingModule = useCallback(
    (target: string) => {
      if (
        !followGenerationRef.current ||
        dirtyRef.current ||
        !draftRef.current ||
        !target
      )
        return;
      if (selectedRef.current !== target) {
        selectedRef.current = target;
        setSelectedId(target);
        setOutline([]);
        setOutlineDirty(false);
        hydrateEditor(draftRef.current, target);
      }
    },
    [hydrateEditor],
  );
  const load = useCallback(
    async (force = false, expectedRunId?: string) => {
      try {
        const detail = await api<Draft>(`/lesson-drafts/${id}`);
        if (expectedRunId && expectedRunId !== runIdRef.current) return detail;
        setDraft(detail);
        setError("");
        if (detail.active_run_id && detail.active_run_id !== runIdRef.current) {
          runIdRef.current = detail.active_run_id;
          setRunId(detail.active_run_id);
          setRun(null);
        }
        const target =
          selectedRef.current ||
          detail.modules?.find((m) => m.enabled)?.module_id ||
          detail.modules?.[0]?.module_id ||
          "";
        if (!selectedRef.current) setSelectedId(target);
        hydrateEditor(detail, target, force);
        // Every successful readback (including a manual retry) must finish handoff.
        if (runIdRef.current && completedRunIdRef.current === runIdRef.current)
          setStreams({});
        return detail;
      } catch (e) {
        setError(message(e));
        throw e;
      } finally {
        setLoading(false);
      }
    },
    [id, hydrateEditor],
  );
  useEffect(() => {
    setLoading(true);
    setDraft(null);
    setRunId(params.get("run") || "");
    setRunKind("generation");
    setPolish(null);
    setReflectionLoaded(false);
    setReflection("");
    setReflectionBase("");
    setReflectionVersion(undefined);
    setReflectionCandidate("");
    setSelectedId("");
    selectedRef.current = "";
    dirtyRef.current = false;
    void load()
      .then((detail) => {
        try {
          const saved = JSON.parse(
            sessionStorage.getItem(`lesson-polish:${id}`) || "null",
          ) as Polish | null;
          if (
            saved?.id &&
            saved.moduleId &&
            detail.modules.some((m) => m.module_id === saved.moduleId)
          ) {
            setPolish(saved);
            polishRef.current = saved;
            kindRef.current = "polish";
            setRunKind("polish");
            selectedRef.current = saved.moduleId;
            setSelectedId(saved.moduleId);
            hydrateEditor(detail, saved.moduleId);
          }
        } catch {
          /* A malformed temporary session cannot replace a formal module. */
        }
        if (
          !params.get("run") &&
          (detail.active_run_id || detail.latest_run_id)
        )
          setRunId(detail.active_run_id || detail.latest_run_id || "");
      })
      .catch(() => {});
  }, [id, load]);
  useEffect(() => {
    if (!draft) return;
    if (polish)
      sessionStorage.setItem(`lesson-polish:${id}`, JSON.stringify(polish));
    else sessionStorage.removeItem(`lesson-polish:${id}`);
  }, [id, polish, draft]);
  useEffect(() => {
    if (!selectedId || !editBase) return;
    const key = `lesson-copy:${id}:${selectedId}`;
    if (dirty)
      sessionStorage.setItem(
        key,
        JSON.stringify({
          content: working,
          ...editBase,
        }),
      );
    else sessionStorage.removeItem(key);
  }, [id, selectedId, working, dirty, editBase]);
  useEffect(() => {
    const before = (e: BeforeUnloadEvent) => {
      if (dirty || outlineDirty || reflection !== reflectionBase) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", before);
    return () => window.removeEventListener("beforeunload", before);
  }, [dirty, outlineDirty, reflection, reflectionBase]);
  const finishRef = useRef<(r: Run) => Promise<void>>(async () => {});
  finishRef.current = async (latest) => {
    if (kindRef.current === "polish" && polishRef.current) {
      try {
        const session = await api<{
          candidate?: unknown;
          working_content?: string;
        }>(`/module-polish-sessions/${polishRef.current.id}`);
        const candidate = textCandidate(
          session.candidate || latest.result?.candidate,
        );
        setPolish((p) => (p ? { ...p, candidate } : null));
      } catch (e) {
        notify(message(e), true);
      }
    } else if (kindRef.current === "reflection") {
      setReflectionCandidate(
        textCandidate(latest.result?.candidate || latest.result?.content),
      );
    }
    try {
      await load(false, latest.id);
      if (latest.id === runIdRef.current) setTerminalReadbackRunId(latest.id);
    } catch {
      /* The received preview survives a failed formal-version readback. */
    }
    if (latest.status === "failed")
      notify(
        typeof latest.error === "string"
          ? latest.error
          : latest.error?.message ||
              latest.error_message ||
              "任务失败，已保留现有正文。",
        true,
      );
    else if (latest.status === "cancelled")
      notify("任务已取消，现有正文已保留。");
  };
  useEffect(() => {
    if (!runId || !draft) return;
    const controller = new AbortController();
    let finished = false;
    let outage = false;
    let pendingRetry: { moduleId: string | null; message: string } | null =
      null;
    setRun(null);
    setTerminalReadbackRunId("");
    completedRunIdRef.current = "";
    setStreams({});
    setModulePhases({});
    setActiveModuleId("");
    setActiveStage("");
    activeStageModule.current = "";
    setProgress("正在准备教学设计");
    setFollowGeneration(true);
    followGenerationRef.current = true;
    setReconnecting(false);
    watchRun(
      runId,
      controller.signal,
      (event) => {
        if (controller.signal.aborted || runIdRef.current !== runId) return;
        const data = event.data;
        const moduleId = String(data.module_id || selectedRef.current);
        const modules =
          draftRef.current?.modules.filter((m) => m.enabled) || [];
        const index = modules.findIndex((m) => m.module_id === moduleId);
        const name =
          modules[index]?.display_name ||
          String(data.display_name || data.module_name || "教学模块");
        const position = index >= 0 ? `（${index + 1}/${modules.length}）` : "";
        if (event.event === "content_delta") {
          const content = contentOfEvent(data);
          setStreams((s) => ({
            ...s,
            [moduleId]: data.replace_previous
              ? content
              : (s[moduleId] || "") + content,
          }));
        }
        if (event.event === "stage_started") {
          activeStageModule.current = moduleId;
          const order =
            data.stage_index && data.stage_count
              ? `（${data.stage_index}/${data.stage_count}）`
              : "";
          setActiveStage(String(data.title || data.stage_id || "") + order);
        }
        if (event.event === "stage_done") {
          setActiveStage("");
          activeStageModule.current = "";
        }
        if (
          event.event === "module_started" ||
          event.event === "structured_step_started"
        ) {
          pendingRetry = null;
          const stage =
            data.stage === "outline"
              ? " · 教学大纲"
              : data.stage === "activities"
                ? " · 课堂活动"
                : "";
          setProgress(`正在生成${name}${position}${stage}`);
          if (kindRef.current === "generation") {
            setActiveModuleId(moduleId);
            setModulePhases((p) => ({ ...p, [moduleId]: "generating" }));
            if (event.event === "module_started")
              setStreams((s) => ({ ...s, [moduleId]: "" }));
            focusGeneratingModule(moduleId);
          }
        }
        if (event.event === "module_done" && kindRef.current === "generation") {
          pendingRetry = null;
          setModulePhases((p) => ({
            ...p,
            [moduleId]: data.is_temporary === false ? "completed" : "generated",
          }));
        }
        if (event.event === "module_progress") {
          const labels: Record<string, string> = {
            quality_check_and_repair_started: "正在检查与完善本次内容",
            blueprint_generation_started: "正在梳理教学设计",
            blueprint_reused: "继续已有教学思路",
            structured_module_generation_started: "正在生成当前模块",
            module_generation_pending: "正在准备完整教案",
            blueprint_generation_done: "教学思路已就绪，正在起草正文",
            quality_check_and_repair_done: "检查完成，正在保存正式版本",
            markdown_stream_fallback_to_json: "正在重新整理模块内容",
            direct_generation_fallback_to_batches: "正在按模块重新生成",
            worker_recovering: "正在恢复备课任务，已保存的内容会继续保留",
            checkpoint_restored: "已恢复完成的教学内容，继续准备剩余部分",
            llm_response_replayed: "已复用保存的模型响应，继续校验与保存",
            dispatch_retry_scheduled:
              "暂时无法投递任务，正在等待自动重试，已保存的内容会保留",
            dispatch_published: "任务已投递，正在等待开始生成",
          };
          const code = String(
            data.phase || data.status || data.stage || data.message || "",
          );
          if (
            code !== "quality_retry_started" &&
            code !== "generation_attempt_reset"
          )
            pendingRetry = null;
          if (labels[code]) setProgress(labels[code]);
          if (code === "generation_attempt_reset") {
            // The runtime announces the new attempt before invalidating its old stream.
            const retry =
              pendingRetry &&
              (data.scope === "module"
                ? pendingRetry.moduleId === moduleId
                : !pendingRetry.moduleId)
                ? pendingRetry
                : null;
            pendingRetry = null;
            setProgress(
              retry?.message || "正在重新生成，已清除本次废弃的临时内容",
            );
            if (data.scope === "module" && data.module_id) {
              setStreams((previous) => {
                const next = { ...previous };
                delete next[moduleId];
                return next;
              });
              setModulePhases((previous) => {
                if (retry) return { ...previous, [moduleId]: "retrying" };
                const next = { ...previous };
                delete next[moduleId];
                return next;
              });
              if (!retry)
                setActiveModuleId((current) =>
                  current === moduleId ? "" : current,
                );
              if (!retry && activeStageModule.current === moduleId) {
                setActiveStage("");
                activeStageModule.current = "";
              }
            } else if (data.scope === "all" || !data.scope) {
              setStreams({});
              setModulePhases({});
              setActiveModuleId("");
              setActiveStage("");
              activeStageModule.current = "";
            }
          }
          if (code === "quality_retry_started") {
            const attempt = Number(data.attempt),
              maximum = Number(data.max_attempts);
            const count =
              Number.isFinite(attempt) && Number.isFinite(maximum)
                ? `（第 ${attempt}/${maximum} 次）`
                : "";
            const notice = `正在重新检查与生成${data.module_id ? name : "本次内容"}${count}`;
            pendingRetry = {
              moduleId: data.module_id ? moduleId : null,
              message: notice,
            };
            setProgress(notice);
            if (data.module_id) {
              setModulePhases((previous) => ({
                ...previous,
                [moduleId]: "retrying",
              }));
              setActiveModuleId(moduleId);
              focusGeneratingModule(moduleId);
            }
          }
          if (
            code === "checkpoint_restored" &&
            data.module_id &&
            kindRef.current === "generation"
          ) {
            if (data.checkpoint_key === `module:${moduleId}`) {
              setModulePhases((previous) => ({
                ...previous,
                [moduleId]: "completed",
              }));
            } else if (
              data.stage_id &&
              data.checkpoint_key === `activity:${moduleId}:${data.stage_id}`
            ) {
              setProgress(`已恢复课堂环节，继续生成${name}${position}`);
              const restoredStage = modules[index]?.state?.outline?.find(
                (stage) => stage.stage_id === data.stage_id,
              );
              activeStageModule.current = moduleId;
              setActiveStage(restoredStage?.title || "已恢复的课堂环节");
              setActiveModuleId(moduleId);
              setModulePhases((previous) => ({
                ...previous,
                [moduleId]: "generating",
              }));
            }
          }
          if (
            code === "quality_check_and_repair_started" &&
            data.module_id &&
            kindRef.current === "generation"
          ) {
            setProgress(`正在检查${name}${position}`);
            setActiveModuleId(moduleId);
            setModulePhases((p) => ({ ...p, [moduleId]: "checking" }));
            focusGeneratingModule(moduleId);
          }
        }
        if (
          event.event === "style_context_resolved" &&
          data.status === "used" &&
          data.effective === true &&
          Number(data.reference_count) > 0
        )
          setProgress("已参考你的个人教案风格");
        if (Array.isArray(data.outline))
          setOutline(data.outline as OutlineStage[]);
      },
      (latest) => {
        if (controller.signal.aborted || runIdRef.current !== runId) return;
        setRun(latest);
        if (latest.workflow_type) {
          const kind =
            latest.workflow_type === "module_polish_message"
              ? "polish"
              : latest.workflow_type === "reflection_polish_message"
                ? "reflection"
                : "generation";
          kindRef.current = kind;
          setRunKind(kind);
          const target = latest.payload?.module_id;
          if (
            kind === "generation" &&
            !terminal(latest.status) &&
            typeof target === "string"
          ) {
            setActiveModuleId(target);
            focusGeneratingModule(target);
          }
        }
        if (terminal(latest.status) && !finished) {
          finished = true;
          if (latest.status === "completed")
            completedRunIdRef.current = latest.id;
          setActiveStage("");
          void finishRef.current(latest);
        }
      },
      (value) => {
        setReconnecting(value);
        if (!value && outage) {
          track("sse_reconnected", { draft_id: id, run_id: runId });
        }
        outage = value;
      },
    ).catch((e) => {
      if (!controller.signal.aborted && runIdRef.current === runId) {
        setError(`${message(e)}。当前任务状态尚未确认，请重新读取后再操作。`);
        track("sse_failed", { draft_id: id, run_id: runId });
      }
    });
    return () => controller.abort();
  }, [runId, draft?.id, watchRevision]);
  useEffect(() => {
    if (!module || outlineDirty) return;
    const canonical = (
      module as LessonModule & {
        canonical_content?: { outline?: OutlineStage[] };
      }
    ).canonical_content;
    const stages = module.state?.outline || canonical?.outline;
    if (module.state?.workflow_stage === "outline") {
      try {
        const copy = JSON.parse(
          sessionStorage.getItem(`lesson-outline:${id}:${module.module_id}`) ||
            "null",
        );
        if (Array.isArray(copy?.outline)) {
          setOutline(copy.outline);
          setOutlineDirty(true);
          return;
        }
      } catch {
        /* Preserve the authoritative outline when a local copy cannot be read. */
      }
    }
    if (stages) setOutline(stages);
  }, [id, module, outlineDirty]);
  useEffect(() => {
    if (!module || module.state?.workflow_stage !== "outline") return;
    const key = `lesson-outline:${id}:${module.module_id}`;
    if (outlineDirty) sessionStorage.setItem(key, JSON.stringify({ outline }));
    else sessionStorage.removeItem(key);
  }, [id, module, outline, outlineDirty]);
  useEffect(() => {
    if (
      !exportState ||
      terminal(exportState.status) ||
      ["ready", "success"].includes(exportState.status)
    )
      return;
    const timer = setTimeout(async () => {
      try {
        const record = await api<{
          id: string;
          status: string;
          error?: string;
        }>(`/lesson-exports/${exportState.id}`);
        setExportState({ ...exportState, status: record.status });
        if (record.status === "failed")
          notify(record.error || "导出失败，请重试。", true);
      } catch (e) {
        notify(message(e), true);
        setExportState({ ...exportState, status: "failed" });
      }
    }, 1800);
    return () => clearTimeout(timer);
  }, [exportState, notify]);
  const startRun = (
    result: { run_id: string },
    kind: "generation" | "polish" | "reflection" = "generation",
  ) => {
    setRunKind(kind);
    kindRef.current = kind;
    setRunId(result.run_id);
    runIdRef.current = result.run_id;
    completedRunIdRef.current = "";
    setRun(null);
    setParams({ run: result.run_id }, { replace: true });
  };
  const moduleNavigationReason = (target: string) => {
    if (busy) return "正在提交操作，请稍候";
    if (running && runKind === "polish") return "请等待当前润色完成";
    if (!interactive) return "";
    const index = enabled.findIndex((item) => item.module_id === target);
    const frontier = enabled.findIndex(
      (item) => item.state?.confirmation_status !== "confirmed",
    );
    // Existing complete content remains readable when an upstream edit relocks progression.
    if (
      index >= 0 &&
      enabled[index].current_version_id &&
      !["outline", "outline_ready"].includes(
        enabled[index].state?.workflow_stage || "",
      )
    )
      return "";
    if (index < 0 || (frontier >= 0 && index > frontier))
      return "请先确认前面的模块，再继续下一步";
    if (
      running &&
      target !== activeModuleId &&
      target !== selectedId &&
      !enabled[index].current_version_id
    )
      return "当前任务执行中，可回看已生成的模块";
    return "";
  };
  const chooseModule = (target: string) => {
    if (!draft || moduleNavigationReason(target)) return;
    if (running && runKind === "generation") {
      setFollowGeneration(false);
      followGenerationRef.current = false;
    }
    selectedRef.current = target;
    setSelectedId(target);
    dirtyRef.current = false;
    hydrateEditor(draft, target);
    setPolish(null);
    setOutlineDirty(false);
    setOutline([]);
  };
  const beginAiAction = () => {
    // React may batch two clicks before disabled renders; admit one request synchronously.
    if (aiActionPending.current || busy || running || moduleReadOnly)
      return false;
    aiActionPending.current = true;
    setBusy(true);
    return true;
  };
  const endAiAction = () => {
    aiActionPending.current = false;
    setBusy(false);
  };
  const versionPayload = () => ({
    expected_content_version: editBase?.contentVersion,
    expected_current_module_version_id: editBase?.moduleVersion,
    expected_module_config_version_id: editBase?.configVersion,
  });
  const save = async (content = working) => {
    if (!draft || !module || busy || running || moduleReadOnly) return;
    setBusy(true);
    try {
      await patch(`/lesson-drafts/${id}/modules/${module.module_id}`, {
        teacher_view_content: content,
        source: polishInstruction ? "ai_polish" : "manual_edit",
        teacher_instruction: polishInstruction || undefined,
        ...versionPayload(),
      });
      dirtyRef.current = false;
      sessionStorage.removeItem(`lesson-copy:${id}:${selectedId}`);
      await load(true);
      setPolish(null);
      notify("已保存为正式版本");
      track("module_saved", { draft_id: id, module_id: module.module_id });
    } catch (e) {
      notify(message(e), true);
    } finally {
      setBusy(false);
    }
  };
  const generate = async (stage?: string) => {
    if (!module || !beginAiAction()) return;
    try {
      const result = await post<{ run_id: string }>(
        `/lesson-drafts/${id}/interactive/step`,
        {
          module_id: module.module_id,
          ...(stage ? { stage } : {}),
          ...versionPayload(),
        },
      );
      sessionStorage.removeItem(`lesson-outline:${id}:${module.module_id}`);
      setOutlineDirty(false);
      startRun(result);
    } catch (e) {
      notify(message(e), true);
    } finally {
      endAiAction();
    }
  };
  useEffect(() => {
    if (
      loading ||
      !interactive ||
      !draft ||
      ["failed", "failed_retryable", "cancelled"].includes(draft.status) ||
      !module?.enabled ||
      module.current_version_id ||
      module.state?.generation_status !== "pending" ||
      module.state?.last_error ||
      draft.active_run_id ||
      running ||
      busy ||
      aiActionPending.current ||
      moduleReadOnly ||
      dirty ||
      working ||
      outlineDirty ||
      polish ||
      error
    )
      return;
    // A terminal event can precede the authoritative version/state readback.
    if (runId && terminalReadbackRunId !== runId) return;
    if (
      run &&
      ["failed", "cancelled"].includes(run.status) &&
      (!run.payload?.module_id || run.payload.module_id === selectedId)
    )
      return;
    const attemptKey = `${id}:${selectedId}`;
    if (autoGenerationAttempts.current.has(attemptKey)) return;
    // Record before submitting: rerenders and failed startup must not repeat AI requests.
    autoGenerationAttempts.current.add(attemptKey);
    void generate(
      module.module_type === "teaching_process" ? "outline" : undefined,
    );
  }, [
    loading,
    interactive,
    draft,
    module,
    running,
    busy,
    moduleReadOnly,
    dirty,
    working,
    outlineDirty,
    polish,
    error,
    run,
    runId,
    terminalReadbackRunId,
    id,
    selectedId,
    generate,
  ]);
  const confirm = async (isOutline = false) => {
    if (!module || busy || running || moduleReadOnly) return;
    // Revisited confirmed modules only need navigation, without another write.
    if (!isOutline && moduleConfirmed) {
      if (!dirty && nextModule) chooseModule(nextModule.module_id);
      return;
    }
    setBusy(true);
    try {
      await post(`/lesson-drafts/${id}/interactive/confirm`, {
        module_id: module.module_id,
        ...versionPayload(),
        ...(isOutline ? { outline } : {}),
      });
      if (isOutline)
        sessionStorage.removeItem(`lesson-outline:${id}:${module.module_id}`);
      setOutlineDirty(false);
      const detail = await load(true);
      notify(isOutline ? "大纲已确认，可以生成课堂活动。" : "模块已确认");
      track("module_confirmed", { draft_id: id, module_id: module.module_id });
      if (!isOutline && detail) {
        const index = detail.modules.findIndex(
          (m) => m.module_id === module.module_id,
        );
        const next = detail.modules
          .slice(index + 1)
          .find(
            (m) => m.enabled && m.state?.confirmation_status !== "confirmed",
          );
        if (next) {
          selectedRef.current = next.module_id;
          setSelectedId(next.module_id);
          dirtyRef.current = false;
          hydrateEditor(detail, next.module_id);
        }
      }
    } catch (e) {
      notify(message(e), true);
    } finally {
      setBusy(false);
    }
  };
  const beginPolish = async () => {
    if (!module || !beginAiAction()) return;
    try {
      const session = await post<{ id: string }>("/module-polish-sessions", {
        draft_id: id,
        module_id: module.module_id,
        scope: selection ? "selection" : "module",
        ...(selection
          ? {
              selection_start: Array.from(working.slice(0, selection.start))
                .length,
              selection_end: Array.from(working.slice(0, selection.end)).length,
            }
          : {}),
        ...versionPayload(),
      });
      setPolish({
        id: session.id,
        moduleId: selectedId,
        base: working,
        selection,
        candidate: "",
        instruction: "",
      });
      setPolishInput("");
    } catch (e) {
      notify(message(e), true);
    } finally {
      endAiAction();
    }
  };
  const requestPolish = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!polish || !polishInput.trim() || !beginAiAction()) return;
    try {
      const result = await post<{ run_id: string }>(
        `/module-polish-sessions/${polish.id}/messages`,
        { instruction: polishInput, working_content: polish.base },
      );
      setPolish({ ...polish, instruction: polishInput, candidate: "" });
      startRun(result, "polish");
      track("polish_started", {
        draft_id: id,
        module_id: selectedId,
        run_id: result.run_id,
      });
    } catch (e) {
      notify(message(e), true);
    } finally {
      endAiAction();
    }
  };
  const adoptPolish = () => {
    if (!polish?.candidate || busy || running || moduleReadOnly) return;
    try {
      const value = polish.selection
        ? applySelectionCandidate(
            polish.base,
            working,
            polish.selection,
            polish.candidate,
          )
        : working === polish.base
          ? polish.candidate
          : (() => {
              throw new Error("工作副本已变化，请重新润色。");
            })();
      setWorking(value);
      setPolishInstruction(polish.instruction);
      track("polish_applied", { draft_id: id, module_id: selectedId });
      setPolish(null);
      notify("候选已放入编辑器，请检查并保存。");
    } catch (e) {
      notify(message(e), true);
    }
  };
  const makePreview = async () => {
    if (dirty) {
      notify("完整预览使用已保存版本，请先保存当前工作副本。", true);
      return;
    }
    setBusy(true);
    try {
      const data = await post<{
        id: string;
        content?: string;
        teacher_view_content?: string;
        modules?: LessonModule[];
      }>(`/lesson-drafts/${id}/preview`);
      const snapshotContent =
        data.teacher_view_content ||
        data.content ||
        data.modules
          ?.map((m) => `## ${m.display_name}\n\n${m.teacher_view_content}`)
          .join("\n\n") ||
        draft?.modules
          .filter((m) => m.enabled)
          .map((m) => `## ${m.display_name}\n\n${m.teacher_view_content}`)
          .join("\n\n") ||
        "";
      // The dialog already provides the title page; keep the immutable module body.
      const heading = snapshotContent.indexOf("\n## ");
      setPreview(
        snapshotContent.startsWith(`# ${draft?.topic}\n`) && heading >= 0
          ? snapshotContent.slice(heading + 1)
          : snapshotContent,
      );
      track("preview_opened", { draft_id: id });
    } catch (e) {
      notify(message(e), true);
    } finally {
      setBusy(false);
    }
  };
  const exportLesson = async (format: "docx" | "pdf") => {
    if (dirty) {
      notify("请先保存工作副本，再导出正式教案。", true);
      return;
    }
    setBusy(true);
    try {
      const record = await post<{ id: string; status: string }>(
        `/lesson-drafts/${id}/exports`,
        { format },
      );
      setExportState({ ...record, format });
      track("export_started", { draft_id: id, format });
    } catch (e) {
      notify(message(e), true);
    } finally {
      setBusy(false);
    }
  };
  const openReflection = async () => {
    setReflectionOpen(true);
    if (reflectionLoaded) return;
    setBusy(true);
    try {
      const data = await api<{
        content?: string;
        teacher_view_content?: string;
        version?: number;
        content_version?: number;
      }>(`/lesson-drafts/${id}/post-lesson-reflection`);
      const text = data.content || data.teacher_view_content || "";
      setReflection(text);
      setReflectionBase(text);
      setReflectionVersion(data.content_version ?? data.version);
      setReflectionLoaded(true);
    } catch (e) {
      notify(message(e), true);
    } finally {
      setBusy(false);
    }
  };
  if (loading)
    return (
      <div className="page">
        <Spinner label="正在打开教学设计" />
      </div>
    );
  if (!draft)
    return (
      <div className="page">
        <div className="error-banner">{error || "教案不存在或无权访问"}</div>
        <Link to="/history" className="button secondary">
          返回教案历史
        </Link>
      </div>
    );
  const confirmed = enabled.filter(
    (m) => m.state?.confirmation_status === "confirmed",
  ).length;
  const isProcess = module?.module_type === "teaching_process";
  const phase = module?.state?.workflow_stage || "";
  const outlineConfirmed = !!module?.state?.outline_confirmed;
  const outlinePhase =
    isProcess &&
    (phase === "outline" ||
      phase === "outline_ready" ||
      (outline.length > 0 &&
        !["activities", "activities_ready", "complete", "completed"].includes(
          phase,
        )));
  const previewable = ["ready", "completed"].includes(draft.status);
  const stream = streams[selectedId];
  const generatedCount = enabled.filter((m) => {
    const phase = modulePhases[m.module_id];
    return phase
      ? ["generated", "checking", "completed"].includes(phase)
      : interactive && !!m.current_version_id;
  }).length;
  const selectedLivePhase =
    running && runKind === "generation" ? modulePhases[selectedId] : undefined;
  return (
    <div className="lesson-page">
      <div className="lesson-heading">
        <div className="lesson-breadcrumb">
          <Link to="/history">
            <ArrowLeft size={15} />
            我的教案
          </Link>
          <ChevronRight size={14} />
          <span>{interactive ? "结构化备课" : "教案编辑"}</span>
        </div>
        <div className="lesson-heading-main">
          <div>
            <div className="lesson-title-row">
              <h1>{draft.topic}</h1>
              <Status value={draft.status} />
            </div>
            <p>
              {displayValue(draft.school_stage)} · {displayValue(draft.grade)} ·{" "}
              {displayValue(draft.subject)}
              <span>／</span>
              <Clock3 size={14} /> {draft.total_duration_min} 分钟
            </p>
          </div>
          <div className="lesson-heading-actions">
            <button
              className="button secondary"
              onClick={openReflection}
              disabled={busy || running}
            >
              <MessageSquare size={16} />
              课后反思
            </button>
            <button
              className="button secondary"
              onClick={makePreview}
              disabled={busy || !previewable}
            >
              <Eye size={16} />
              完整预览
            </button>
            <div className="export-menu">
              <button
                className="button primary"
                onClick={() => void exportLesson("docx")}
                disabled={busy || !previewable}
              >
                <Download size={16} />
                导出 Word
              </button>
              <button
                className="button primary icon-only"
                aria-label="导出 PDF"
                title="导出 PDF"
                onClick={() => void exportLesson("pdf")}
                disabled={busy || !previewable}
              >
                <FileText size={16} />
              </button>
            </div>
          </div>
        </div>
      </div>
      {!!draft.knowledge_context_snapshot?.length && (
        <details className="knowledge-references">
          <summary>
            <BookOpen size={16} />
            本教案参考来源{" "}
            <span>{draft.knowledge_context_snapshot.length} 条</span>
          </summary>
          <p>以下为创建时选用的资料片段；后续资料变更不会改写这些引用。</p>
          <div>
            {draft.knowledge_context_snapshot.map((reference, index) => (
              <article key={`${reference.id}-${index}`}>
                <header>
                  <b>{reference.filename}</b>
                  <span>
                    {reference.base_name} · 片段 {reference.ordinal + 1}
                  </span>
                </header>
                <p>{reference.text}</p>
              </article>
            ))}
          </div>
        </details>
      )}
      {error && (
        <div className="error-banner" role="alert">
          {error}
          <button
            className="text-button"
            onClick={() =>
              void load()
                .then(() => setWatchRevision((value) => value + 1))
                .catch(() => {})
            }
          >
            重新读取
          </button>
        </div>
      )}
      {running && (
        <div className="generation-banner" role="status">
          <span className="pulse-dot" />
          <div>
            <b>
              {reconnecting
                ? "连接正在恢复，已保留收到的内容"
                : runKind === "polish"
                  ? "正在打磨润色候选"
                  : runKind === "reflection"
                    ? "正在整理反思候选"
                    : progress}
            </b>
            <small>
              {activeStage
                ? `当前环节：${activeStage}`
                : "生成内容仅作临时展示，完成后以正式版本为准。"}
              {interactive &&
                runKind === "generation" &&
                " 可回看已生成模块；任务结束后可重新生成或润色。"}
            </small>
            {runKind === "generation" && (
              <div className="generation-count">
                <progress
                  max={enabled.length}
                  value={generatedCount}
                  aria-label="模块生成进度"
                />
                <span>
                  已生成 {generatedCount}/{enabled.length} 个模块
                </span>
              </div>
            )}
          </div>
          {runKind === "generation" &&
            activeModuleId &&
            (!followGeneration || selectedId !== activeModuleId) && (
              <button
                className="button secondary small"
                onClick={() => {
                  chooseModule(activeModuleId);
                  setFollowGeneration(true);
                  followGenerationRef.current = true;
                }}
              >
                跟随当前模块
              </button>
            )}
          <button
            className="button secondary small"
            onClick={async () => {
              try {
                await post(`/generation-runs/${runId}/cancel`);
                notify("取消请求已提交");
              } catch (e) {
                notify(message(e), true);
              }
            }}
          >
            <Square size={13} />
            取消任务
          </button>
        </div>
      )}
      {run && ["failed", "cancelled"].includes(run.status) && (
        <div className="run-failure">
          <Status value={run.status} />
          <span>
            {typeof run.error === "string"
              ? run.error
              : run.error?.message ||
                "此任务已停止。可以查看现有正文并重新生成。"}
          </span>
        </div>
      )}
      {exportState && (
        <div className="export-status">
          <FileClock size={18} />
          <span>
            {["ready", "completed", "success"].includes(exportState.status)
              ? `${exportState.format.toUpperCase()} 文件已准备好`
              : exportState.status === "failed"
                ? "文件导出失败"
                : "正在排版导出文件…"}
          </span>
          {["ready", "completed", "success"].includes(exportState.status) && (
            <button
              className="text-button"
              onClick={() =>
                download(
                  `/lesson-exports/${exportState.id}/download`,
                  `${draft.topic}.${exportState.format}`,
                ).catch((e) => notify(message(e), true))
              }
            >
              下载文件 <Download size={14} />
            </button>
          )}
          <button
            className="icon-button"
            aria-label="关闭导出状态"
            onClick={() => setExportState(null)}
          >
            <X size={15} />
          </button>
        </div>
      )}
      <div className="lesson-workspace">
        <aside className="module-sidebar">
          <div className="module-sidebar-heading">
            <span>教学设计目录</span>
            <small>
              {interactive
                ? `${confirmed}/${enabled.length} 已确认`
                : `${enabled.length} 个模块`}
            </small>
          </div>
          <nav aria-label="教学模块">
            {enabled.map((m, i) => (
              <button
                key={m.module_id}
                disabled={!!moduleNavigationReason(m.module_id)}
                title={moduleNavigationReason(m.module_id) || undefined}
                className={selectedId === m.module_id ? "selected" : ""}
                aria-current={selectedId === m.module_id ? "step" : undefined}
                onClick={() => chooseModule(m.module_id)}
              >
                <span
                  className={`module-number ${m.state?.confirmation_status === "confirmed" ? "done" : ""}`}
                >
                  {m.state?.confirmation_status === "confirmed" ? (
                    <Check size={13} />
                  ) : (
                    String(i + 1).padStart(2, "0")
                  )}
                </span>
                <span>{m.display_name}</span>
                {selectedId === m.module_id && <ChevronRight size={14} />}
              </button>
            ))}
          </nav>
          {interactive && (
            <div className="module-finalize">
              <p>逐步确认，连成完整的一堂课。</p>
              <button
                className="button primary"
                disabled={
                  busy || running || confirmed !== enabled.length || dirty
                }
                onClick={async () => {
                  setBusy(true);
                  try {
                    const result = await post<{ run_id?: string }>(
                      `/lesson-drafts/${id}/interactive/finalize`,
                    );
                    if (result.run_id) startRun({ run_id: result.run_id });
                    else await load();
                    notify("教案已完成");
                  } catch (e) {
                    notify(message(e), true);
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                <CheckCheck size={16} />
                完成整份教案
              </button>
            </div>
          )}
        </aside>
        <section className="module-main">
          {module ? (
            <>
              <div className="module-editor-heading">
                <div>
                  <span className="eyebrow">
                    MODULE{" "}
                    {String(
                      enabled.findIndex(
                        (m) => m.module_id === module.module_id,
                      ) + 1,
                    ).padStart(2, "0")}
                  </span>
                  <h2>{module.display_name}</h2>
                </div>
                <div>
                  {selectedLivePhase ? (
                    <span className="status status-running">
                      {liveModuleLabels[selectedLivePhase]}
                    </span>
                  ) : (
                    <Status value={stateLabel(module)} />
                  )}
                  <button
                    className="icon-button"
                    title="历史版本"
                    aria-label="查看模块历史版本"
                    disabled={busy}
                    onClick={async () => {
                      setBusy(true);
                      try {
                        setVersions(
                          await api<Version[]>(
                            `/lesson-drafts/${id}/modules/${module.module_id}/versions`,
                          ),
                        );
                      } catch (e) {
                        notify(message(e), true);
                      } finally {
                        setBusy(false);
                      }
                    }}
                  >
                    <History size={18} />
                  </button>
                </div>
              </div>
              {["stale", "unknown"].includes(module.state?.freshness || "") && (
                <div className="notice">
                  上游内容已修改，请检查本模块是否需要调整。正文已保留，系统不会自动改写。
                </div>
              )}
              {moduleReadOnly && (
                <div className="notice">
                  当前仅回看已生成内容。请先确认前面的模块，再编辑、重新生成或润色本模块。
                </div>
              )}
              {stream !== undefined && runKind === "generation" ? (
                <div className="stream-content">
                  <div className="stream-label">
                    {running && <span className="pulse-dot" />}
                    {running
                      ? "AI 正在起草 · 临时内容"
                      : run?.status === "completed"
                        ? "正在读取正式版本"
                        : "生成已停止 · 临时内容未保存"}
                    {!running && run?.status !== "completed" && (
                      <button
                        className="text-button"
                        onClick={() =>
                          setStreams((s) => {
                            const next = { ...s };
                            delete next[selectedId];
                            return next;
                          })
                        }
                      >
                        查看已保存正文
                      </button>
                    )}
                  </div>
                  {stream ? (
                    <div
                      className="markdown"
                      dangerouslySetInnerHTML={{
                        __html: renderMarkdown(stream),
                      }}
                    />
                  ) : (
                    <p className="stream-waiting">
                      正在等待正文，收到内容后会实时显示…
                    </p>
                  )}
                </div>
              ) : outlinePhase ? (
                <OutlineEditor
                  value={outline}
                  onChange={(value) => {
                    setOutline(value);
                    setOutlineDirty(true);
                  }}
                  disabled={running || busy || moduleReadOnly}
                  total={draft.total_duration_min}
                />
              ) : !working && !module.current_version_id ? (
                busy || (running && runKind === "generation") ? (
                  <Spinner label="正在启动模块生成" />
                ) : (
                  <Empty
                    title={
                      isProcess ? "先梳理课堂环节" : "让这个模块的思路成形"
                    }
                    action={
                      <button
                        className="button primary"
                        disabled={busy || running || moduleReadOnly}
                        onClick={() =>
                          void generate(isProcess ? "outline" : undefined)
                        }
                      >
                        <Sparkles size={17} />
                        {autoGenerationAttempts.current.has(
                          `${id}:${selectedId}`,
                        )
                          ? "重试生成"
                          : isProcess
                            ? "生成教学大纲"
                            : "生成当前模块"}
                      </button>
                    }
                  >
                    生成后可以继续编辑、润色，再确认教学设计。
                  </Empty>
                )
              ) : (
                <RichEditor
                  key={`${id}:${selectedId}:${module.current_version_id || "draft"}`}
                  value={working}
                  onChange={setWorking}
                  onSelection={setSelection}
                  readonly={
                    busy ||
                    moduleReadOnly ||
                    (running && runKind === "generation")
                  }
                />
              )}
              <div className="module-actions">
                <div className="save-indicator">
                  <i className={dirty || outlineDirty ? "unsaved" : ""} />
                  {outlineDirty
                    ? "大纲有未确认修改"
                    : dirty
                      ? "有未保存的工作副本"
                      : "已与正式版本同步"}
                  {selection && (
                    <span>已选择 {selection.text.length} 字符</span>
                  )}
                </div>
                <div>
                  {dirty && (
                    <button
                      className="text-button"
                      disabled={busy || running}
                      onClick={() => {
                        dirtyRef.current = false;
                        hydrateEditor(draft, selectedId, true);
                        setError("");
                        setPolish(null);
                      }}
                    >
                      恢复已保存正文
                    </button>
                  )}
                  <button
                    className="button secondary small"
                    disabled={
                      busy ||
                      running ||
                      moduleReadOnly ||
                      !working ||
                      outlinePhase
                    }
                    onClick={beginPolish}
                  >
                    <Sparkles size={15} />
                    {selection ? "润色选区" : "AI 润色"}
                  </button>
                  <button
                    className="button primary small"
                    disabled={
                      busy ||
                      running ||
                      moduleReadOnly ||
                      !dirty ||
                      outlinePhase
                    }
                    onClick={() => void save()}
                  >
                    <Save size={15} />
                    保存正文
                  </button>
                </div>
              </div>
              {interactive && (
                <div className="interactive-actions">
                  <span>
                    {outlinePhase
                      ? "确认大纲后，按你的课堂环节生成详细活动。"
                      : "确认当前模块，让下一步教学设计有据可依。"}
                  </span>
                  <div>
                    {module.current_version_id && (
                      <button
                        className="button secondary small"
                        disabled={busy || running || moduleReadOnly || dirty}
                        onClick={() =>
                          void generate(
                            isProcess
                              ? outlineConfirmed
                                ? "activities"
                                : "outline"
                              : undefined,
                          )
                        }
                      >
                        <RotateCcw size={14} />
                        重新生成
                      </button>
                    )}
                    {outlinePhase && (!outlineConfirmed || outlineDirty) ? (
                      <button
                        className="button primary small"
                        disabled={
                          busy ||
                          running ||
                          moduleReadOnly ||
                          !outline.length ||
                          outline.some(
                            (s) => !s.title.trim() || s.duration_min <= 0,
                          )
                        }
                        onClick={() => void confirm(true)}
                      >
                        确认大纲 <Check size={15} />
                      </button>
                    ) : outlineConfirmed && isProcess && phase === "outline" ? (
                      <button
                        className="button primary small"
                        disabled={busy || running || moduleReadOnly}
                        onClick={() => void generate("activities")}
                      >
                        生成课堂活动 <ArrowRight size={15} />
                      </button>
                    ) : (
                      <button
                        className="button primary small"
                        disabled={
                          busy ||
                          running ||
                          moduleReadOnly ||
                          dirty ||
                          !module.current_version_id ||
                          (moduleConfirmed &&
                            (!nextModule ||
                              !!moduleNavigationReason(nextModule.module_id)))
                        }
                        onClick={() => void confirm()}
                      >
                        {moduleConfirmed
                          ? nextModule
                            ? "继续下一步"
                            : "当前模块已确认"
                          : "确认，继续下一步"}
                        <ArrowRight size={15} />
                      </button>
                    )}
                  </div>
                </div>
              )}
              {!interactive &&
                ([
                  "failed",
                  "failed_retryable",
                  "cancelled",
                  "ready_partial",
                ].includes(draft.status) ||
                  (!!run && ["failed", "cancelled"].includes(run.status))) && (
                  <div className="interactive-actions">
                    <span>保留已生成内容，按模块补充未完成部分。</span>
                    <button
                      className="button secondary small"
                      disabled={busy || running || dirty}
                      onClick={async () => {
                        setBusy(true);
                        try {
                          startRun(
                            await post<{ run_id: string }>(
                              `/lesson-drafts/${id}/generations/one-click`,
                            ),
                          );
                        } catch (e) {
                          notify(message(e), true);
                        } finally {
                          setBusy(false);
                        }
                      }}
                    >
                      <RotateCcw size={14} />
                      重新生成整份教案
                    </button>
                  </div>
                )}
            </>
          ) : (
            <Empty title="暂无启用模块" />
          )}
        </section>
      </div>
      {polish && (
        <div className="polish-panel">
          <header>
            <span>
              <Sparkles size={18} />
              {polish.selection ? "选区润色" : "模块润色"}
              <small>候选经你采用后才进入编辑器</small>
            </span>
            <button
              className="icon-button"
              aria-label="关闭润色"
              disabled={running && runKind === "polish"}
              onClick={() => setPolish(null)}
            >
              <X size={18} />
            </button>
          </header>
          <div className="polish-body">
            {polish.selection && (
              <blockquote>{polish.selection.text}</blockquote>
            )}
            {polish.instruction && (
              <div className="polish-instruction">{polish.instruction}</div>
            )}
            {running && runKind === "polish" ? (
              streams[selectedId] ? (
                <div
                  className="markdown"
                  dangerouslySetInnerHTML={{
                    __html: renderMarkdown(streams[selectedId]),
                  }}
                />
              ) : (
                <Spinner label="正在构思润色候选" />
              )
            ) : polish.candidate ? (
              <>
                <div className="candidate-label">润色候选 · 尚未保存</div>
                <div
                  className="markdown"
                  dangerouslySetInnerHTML={{
                    __html: renderMarkdown(polish.candidate),
                  }}
                />
                <div className="candidate-actions">
                  <button
                    className="button secondary small"
                    onClick={() => setPolish(null)}
                  >
                    放弃候选
                  </button>
                  <button
                    className="button primary small"
                    disabled={busy || running || moduleReadOnly}
                    onClick={adoptPolish}
                  >
                    <Check size={14} />
                    采用到编辑器
                  </button>
                </div>
              </>
            ) : (
              <p className="muted">
                告诉 AI 你希望怎样调整，例如：让课堂提问更有层次，保留原有结构。
              </p>
            )}
          </div>
          <form className="polish-input" onSubmit={requestPolish}>
            <textarea
              value={polishInput}
              onChange={(e) => setPolishInput(e.target.value)}
              placeholder="输入你的修改想法……"
              aria-label="润色要求"
              rows={3}
              required
              maxLength={4000}
            />
            <button
              className="button primary small"
              disabled={
                busy || running || moduleReadOnly || !polishInput.trim()
              }
            >
              <Send size={15} />
              发送要求
            </button>
          </form>
        </div>
      )}
      {versions && (
        <Modal
          title={`${module?.display_name} · 历史版本`}
          onClose={() => setVersions(null)}
          wide
        >
          <div className="modal-body">
            {!versions.length ? (
              <Empty title="还没有正式版本" />
            ) : (
              versions.map((v) => (
                <article className="version-item" key={v.id}>
                  <header>
                    <span>
                      <b>版本 {v.version_no || ""}</b> ·{" "}
                      {v.source || "保存版本"}
                    </span>
                    <span>
                      {v.created_at
                        ? new Date(v.created_at).toLocaleString("zh-CN")
                        : ""}
                    </span>
                    <button
                      className="button secondary small"
                      disabled={
                        busy ||
                        running ||
                        moduleReadOnly ||
                        v.id === module?.current_version_id
                      }
                      onClick={async () => {
                        setBusy(true);
                        try {
                          await post(
                            `/lesson-drafts/${id}/modules/${selectedId}/restore`,
                            { version_id: v.id, ...versionPayload() },
                          );
                          setVersions(null);
                          dirtyRef.current = false;
                          await load(true);
                          notify("历史版本已恢复，新增了一条正式版本。");
                          track("version_restored", {
                            draft_id: id,
                            module_id: selectedId,
                          });
                        } catch (e) {
                          notify(message(e), true);
                        } finally {
                          setBusy(false);
                        }
                      }}
                    >
                      <RotateCcw size={14} />
                      恢复此版本
                    </button>
                  </header>
                  <div
                    className="markdown version-content"
                    dangerouslySetInnerHTML={{
                      __html: renderMarkdown(v.teacher_view_content),
                    }}
                  />
                </article>
              ))
            )}
          </div>
        </Modal>
      )}
      {preview !== null && (
        <Modal title="完整教案预览" onClose={() => setPreview(null)} wide>
          <div className="preview-sheet">
            <div className="preview-title">
              <span>TEACHING PLAN</span>
              <h1>{draft.topic}</h1>
              <p>
                {displayValue(draft.grade)} · {displayValue(draft.subject)} ·{" "}
                {draft.total_duration_min} 分钟
              </p>
            </div>
            <div
              className="markdown"
              dangerouslySetInnerHTML={{ __html: renderMarkdown(preview) }}
            />
          </div>
          <div className="modal-actions preview-actions">
            <button
              className="button secondary"
              disabled={busy}
              onClick={() => void exportLesson("pdf")}
            >
              导出 PDF
            </button>
            <button
              className="button primary"
              disabled={busy}
              onClick={() => void exportLesson("docx")}
            >
              <Download size={16} />
              导出 Word
            </button>
          </div>
        </Modal>
      )}
      {reflectionOpen && (
        <Modal
          title="课后教学反思"
          onClose={() => setReflectionOpen(false)}
          wide
        >
          <div className="modal-body">
            <p className="notice">
              记录实际课堂中的观察、收获与调整。课后反思独立保存，不改变课前教案。
            </p>
            <RichEditor
              key={`reflection:${id}:${reflectionVersion ?? 0}`}
              value={reflection}
              onChange={setReflection}
              readonly={busy || running}
            />
            {reflectionCandidate && (
              <div className="reflection-candidate">
                <span className="candidate-label">反思润色候选 · 尚未保存</span>
                <div
                  className="markdown"
                  dangerouslySetInnerHTML={{
                    __html: renderMarkdown(reflectionCandidate),
                  }}
                />
                <button
                  className="button secondary small"
                  onClick={() => {
                    setReflection(reflectionCandidate);
                    setReflectionCandidate("");
                  }}
                >
                  采用到反思编辑器
                </button>
                <button
                  className="text-button"
                  onClick={() => setReflectionCandidate("")}
                >
                  放弃
                </button>
              </div>
            )}
            <div className="modal-actions">
              <button
                className="button secondary"
                disabled={busy || running || !reflection.trim()}
                onClick={async () => {
                  setBusy(true);
                  try {
                    const result = await post<{ run_id: string }>(
                      `/lesson-drafts/${id}/post-lesson-reflection/polish`,
                      {
                        instruction:
                          "保留真实课堂事实，梳理反思层次，明确可执行的改进措施。",
                        working_content: reflection,
                      },
                    );
                    startRun(result, "reflection");
                    track("polish_started", {
                      draft_id: id,
                      run_id: result.run_id,
                    });
                  } catch (e) {
                    notify(message(e), true);
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                <Sparkles size={16} />
                AI 整理反思
              </button>
              <button
                className="button primary"
                disabled={busy || running || reflection === reflectionBase}
                onClick={async () => {
                  setBusy(true);
                  try {
                    const result = await api<{
                      content_version?: number;
                      version?: number;
                    }>(`/lesson-drafts/${id}/post-lesson-reflection`, {
                      method: "PUT",
                      body: JSON.stringify({
                        content: reflection,
                        expected_version: reflectionVersion ?? 0,
                      }),
                    });
                    setReflectionBase(reflection);
                    setReflectionVersion(
                      result.content_version ?? result.version,
                    );
                    notify("课后反思已独立保存");
                    track("reflection_saved", { draft_id: id });
                  } catch (e) {
                    notify(message(e), true);
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                <Save size={16} />
                保存课后反思
              </button>
            </div>
            {running && runKind === "reflection" && (
              <Spinner label="正在整理反思，正文会保留" />
            )}
          </div>
        </Modal>
      )}
    </div>
  );
}

function OutlineEditor({
  value,
  onChange,
  disabled,
  total,
}: {
  value: OutlineStage[];
  onChange: (v: OutlineStage[]) => void;
  disabled: boolean;
  total: number;
}) {
  const sum = value.reduce((n, s) => n + Number(s.duration_min || 0), 0);
  const update = (index: number, patch: Partial<OutlineStage>) =>
    onChange(value.map((s, i) => (i === index ? { ...s, ...patch } : s)));
  const move = (index: number, offset: number) => {
    const stages = [...value];
    [stages[index], stages[index + offset]] = [
      stages[index + offset],
      stages[index],
    ];
    onChange(stages);
  };
  return (
    <div className="outline-editor">
      <div className="outline-intro">
        <span className="eyebrow">STEP 1 / 教学过程大纲</span>
        <h3>先安排课堂的节奏</h3>
        <p>增删、调整环节，再按确认后的大纲生成课堂活动。</p>
      </div>
      {value.length ? (
        value.map((stage, index) => (
          <div className="outline-stage" key={stage.stage_id}>
            <span className="outline-stage-number">
              {String(index + 1).padStart(2, "0")}
            </span>
            <div>
              <label>
                <span className="visually-hidden">环节 {index + 1} 标题</span>
                <input
                  value={stage.title}
                  onChange={(e) => update(index, { title: e.target.value })}
                  disabled={disabled}
                  placeholder="课堂环节名称"
                  maxLength={100}
                />
              </label>
              <input
                value={stage.objective || ""}
                onChange={(e) => update(index, { objective: e.target.value })}
                disabled={disabled}
                placeholder="这个环节希望学生做什么？"
                aria-label={`环节 ${index + 1} 说明`}
                className="outline-summary"
              />
            </div>
            <label className="duration-input">
              <input
                type="number"
                min={1}
                max={240}
                value={stage.duration_min}
                onChange={(e) =>
                  update(index, { duration_min: Number(e.target.value) })
                }
                disabled={disabled}
                aria-label={`环节 ${index + 1} 分钟`}
              />
              <span>分钟</span>
            </label>
            <div className="outline-stage-actions">
              <button
                className="icon-button"
                disabled={disabled || index === 0}
                onClick={() => move(index, -1)}
                aria-label="上移环节"
              >
                <ArrowUp size={14} />
              </button>
              <button
                className="icon-button"
                disabled={disabled || index === value.length - 1}
                onClick={() => move(index, 1)}
                aria-label="下移环节"
              >
                <ArrowDown size={14} />
              </button>
              <button
                className="icon-button danger-text"
                disabled={disabled}
                onClick={() => onChange(value.filter((_, i) => i !== index))}
                aria-label="删除环节"
              >
                <Trash2 size={14} />
              </button>
            </div>
          </div>
        ))
      ) : (
        <Empty title="还没有课堂环节">生成大纲，或直接添加你的课堂安排。</Empty>
      )}
      <button
        className="add-outline"
        disabled={disabled}
        onClick={() =>
          onChange([
            ...value,
            {
              stage_id: crypto.randomUUID(),
              title: "",
              duration_min: 5,
              objective: "",
            },
          ])
        }
      >
        <Plus size={16} />
        添加课堂环节
      </button>
      <div className="outline-total">
        <span>
          已安排 <b>{value.length}</b> 个环节
        </span>
        <span>
          总计 <b>{sum}</b> 分钟
          {sum !== total && `（原计划 ${total} 分钟，以确认大纲为准）`}
        </span>
      </div>
    </div>
  );
}
