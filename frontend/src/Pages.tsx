import { useCallback, useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  ArrowRight,
  ArrowUpRight,
  BookOpen,
  BookText,
  Check,
  ChevronDown,
  Download,
  FileText,
  FolderOpen,
  Layers3,
  Plus,
  Search,
  SlidersHorizontal,
  Sparkles,
  Trash2,
  Upload,
  Waypoints,
  X,
} from "lucide-react";
import { api, download, patch, post, privateBlob, track } from "./api";
import { displayValue } from "./catalog";
import type { Draft, KnowledgeBase, ModuleConfig, User } from "./types";
import {
  Empty,
  formatDate,
  Modal,
  NewLessonLink,
  PageHeading,
  Spinner,
  Status,
  useNotify,
} from "./ui";

export const DEFAULT_MODULES: ModuleConfig[] = [
  ["textbook_analysis", "教材分析"],
  ["teaching_objectives", "教学目标"],
  ["key_difficult_points", "教学重难点"],
  ["teaching_methods", "教学方法"],
  ["teaching_preparation", "教学准备"],
  ["teaching_process", "教学过程"],
  ["blackboard_design", "板书设计"],
].map(([module_type, display_name]) => ({
  module_type,
  display_name,
  enabled: true,
  is_custom: false,
  module_summary: "",
}));
const PRESET_MODULES = [
  ["student_analysis", "学情分析"],
  ["classroom_practice", "课堂练习"],
  ["assessment_task", "评价设计"],
  ["homework_design", "作业设计"],
  ["design_intent", "整体设计思路"],
];
function errorMessage(e: unknown) {
  return e instanceof Error ? e.message : "请求未成功，请重试";
}
export function useDrafts() {
  const [items, setItems] = useState<Draft[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const reload = useCallback(async () => {
    setError("");
    try {
      const data = await api<Draft[]>("/lesson-drafts");
      setItems(data);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void reload();
  }, [reload]);
  return { items, loading, error, reload };
}
function ErrorBox({ message, retry }: { message: string; retry?: () => void }) {
  return (
    <div className="error-banner" role="alert">
      {message}
      {retry && (
        <button className="text-button" onClick={retry}>
          重试
        </button>
      )}
    </div>
  );
}
function LessonCard({
  draft,
  onDelete,
}: {
  draft: Draft;
  onDelete?: () => void;
}) {
  return (
    <article className="lesson-card">
      <div className="lesson-card-top">
        <span className="subject-icon">
          <BookText size={23} />
        </span>
        <Status value={draft.status} />
      </div>
      <Link to={`/lessons/${draft.id}`} className="lesson-title">
        {draft.topic}
      </Link>
      <div className="lesson-meta">
        <span>{displayValue(draft.grade)}</span>
        <span>{displayValue(draft.subject)}</span>
        <span>{draft.total_duration_min} 分钟</span>
      </div>
      <div className="lesson-card-bottom">
        <span>{formatDate(draft.updated_at || draft.created_at)}</span>
        <div>
          {onDelete && (
            <button
              className="icon-button danger-text"
              title="删除教案"
              aria-label={`删除${draft.topic}`}
              onClick={onDelete}
            >
              <Trash2 size={15} />
            </button>
          )}
          <Link
            to={`/lessons/${draft.id}`}
            className="card-arrow"
            aria-label={`打开${draft.topic}`}
          >
            <ArrowUpRight size={19} />
          </Link>
        </div>
      </div>
    </article>
  );
}
export function Dashboard({ user }: { user: User }) {
  const { items, loading, error, reload } = useDrafts();
  const hour = new Date().getHours();
  const greeting =
    hour < 11
      ? "上午好"
      : hour < 14
        ? "中午好"
        : hour < 18
          ? "下午好"
          : "晚上好";
  return (
    <div className="page dashboard">
      <PageHeading
        eyebrow="YOUR TEACHING STUDIO"
        title={`${greeting}，${user.name || "老师"}`}
        description="每一份认真准备，都会在课堂上发光。"
        action={
          <span className="today-date">
            {new Date().toLocaleDateString("zh-CN", {
              month: "long",
              day: "numeric",
              weekday: "long",
            })}
          </span>
        }
      />
      <section className="welcome-banner">
        <div>
          <span className="welcome-kicker">
            <span /> 从灵感，到一堂好课
          </span>
          <h2>
            备课有思路，
            <br />
            教学更从容。
          </h2>
          <p>
            与你一起梳理教学目标、设计课堂环节，
            <br />
            让好的教学想法，自然生长。
          </p>
          <Link to="/new" className="button cream">
            开始准备新教案 <ArrowRight size={17} />
          </Link>
        </div>
        <div className="banner-art" aria-hidden="true">
          <div className="art-grid" />
          <div className="art-sun" />
          <div className="art-paper">
            <span>TEACHING NOTES</span>
            <h3>
              让每一个想法
              <br />
              找到课堂的形状。
            </h3>
            <div className="art-lines">
              <i />
              <i />
              <i />
            </div>
            <span className="art-paper-footer">
              LESSON ATELIER <BookOpen size={17} />
            </span>
          </div>
          <div className="art-sprig">✳</div>
          <div className="art-label">灵感正在生长</div>
        </div>
      </section>
      <div className="entry-grid">
        <Link to="/new?mode=one-click" className="entry-card">
          <span className="entry-icon warm">
            <Sparkles size={21} />
          </span>
          <div>
            <h3>一键生成</h3>
            <p>输入课题，完整教案一气呵成</p>
          </div>
          <ArrowUpRight size={20} />
        </Link>
        <Link to="/new?mode=interactive" className="entry-card">
          <span className="entry-icon green">
            <Waypoints size={21} />
          </span>
          <div>
            <h3>结构化备课</h3>
            <p>分模块推敲，循序打磨教学设计</p>
          </div>
          <ArrowUpRight size={20} />
        </Link>
        <Link to="/library" className="entry-card">
          <span className="entry-icon sand">
            <FolderOpen size={21} />
          </span>
          <div>
            <h3>个人教案库</h3>
            <p>收藏教学积累，延续你的风格</p>
          </div>
          <ArrowUpRight size={20} />
        </Link>
      </div>
      <section className="recent-section">
        <div className="section-heading">
          <div>
            <h2>
              最近的教学灵感 <span>RECENT LESSONS</span>
            </h2>
            <p>接着上次的思路，继续准备。</p>
          </div>
          <Link to="/history" className="text-link">
            全部教案 <ArrowRight size={15} />
          </Link>
        </div>
        {loading ? (
          <Spinner />
        ) : error ? (
          <ErrorBox message={error} retry={reload} />
        ) : items.length ? (
          <div className="lesson-grid">
            {items.slice(0, 6).map((d) => (
              <LessonCard draft={d} key={d.id} />
            ))}
          </div>
        ) : (
          <Empty title="你的第一份教案，从这里开始" action={<NewLessonLink />}>
            输入一个想教的课题，把课堂构想变成完整的教学设计。
          </Empty>
        )}
      </section>
    </div>
  );
}

type Dictionary = {
  school_stages?: unknown;
  grades?: unknown;
  subjects?: unknown;
  lesson_types?: unknown;
  modules?: ModuleConfig[];
  [key: string]: unknown;
};
function options(
  value: unknown,
  fallback: string[],
): { value: string; label: string }[] {
  if (Array.isArray(value))
    return value.map((v) =>
      typeof v === "string"
        ? { value: v, label: v }
        : {
            value: String(v.value || v.code || v.id || v.name),
            label: String(
              v.label || v.name || v.display_name || v.value || v.code,
            ),
          },
    );
  if (value && typeof value === "object")
    return Object.entries(value).map(([value, label]) => ({
      value,
      label: String(label),
    }));
  return fallback.map((v) => ({ value: v, label: v }));
}
function SelectField({
  label,
  value,
  onChange,
  choices,
  required = true,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  choices: { value: string; label: string }[];
  required?: boolean;
}) {
  return (
    <label>
      {label}
      <span className="select-wrap">
        <select
          value={value}
          onChange={(e) => onChange(e.target.value)}
          required={required}
        >
          {!choices.some((c) => c.value === value) && (
            <option value={value}>{displayValue(value) || "暂不设置"}</option>
          )}
          {choices.map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
        </select>
        <ChevronDown size={15} />
      </span>
    </label>
  );
}
export function ModuleChooser({
  value,
  onChange,
}: {
  value: ModuleConfig[];
  onChange: (modules: ModuleConfig[]) => void;
}) {
  const [add, setAdd] = useState(false);
  const [name, setName] = useState("");
  const [summary, setSummary] = useState("");
  const [error, setError] = useState("");
  const save = () => {
    const normalized = name.trim();
    if (!normalized || normalized.length > 20) {
      setError("请输入 1 至 20 字的模块名称");
      return;
    }
    if (value.some((m) => m.display_name === normalized)) {
      setError("模块名称已存在");
      return;
    }
    const preset = PRESET_MODULES.find(([, label]) => label === normalized);
    onChange([
      ...value,
      {
        module_type: preset?.[0] || "custom_module",
        display_name: normalized,
        module_summary: summary,
        enabled: true,
        is_custom: !preset,
      },
    ]);
    setAdd(false);
    setName("");
    setSummary("");
    setError("");
  };
  return (
    <div className="module-chooser">
      <div className="module-checks">
        {value.map((m, i) => (
          <div
            key={`${m.module_type}-${i}`}
            className={`module-check ${m.enabled ? "selected" : ""}`}
          >
            <label>
              <input
                type="checkbox"
                checked={m.enabled}
                onChange={(e) =>
                  onChange(
                    value.map((x, j) =>
                      j === i ? { ...x, enabled: e.target.checked } : x,
                    ),
                  )
                }
              />
              <span className="check-mark">
                {m.enabled && <Check size={13} />}
              </span>
              {m.display_name}
            </label>
            {m.is_custom && (
              <button
                type="button"
                className="icon-button"
                aria-label={`移除${m.display_name}`}
                onClick={() => onChange(value.filter((_, j) => j !== i))}
              >
                <X size={13} />
              </button>
            )}
          </div>
        ))}
        <button
          type="button"
          className="add-module"
          onClick={() => setAdd(true)}
        >
          <Plus size={15} />
          添加模块
        </button>
      </div>
      {value.some((m) => m.enabled && m.module_summary) && (
        <div className="module-summaries">
          {value
            .filter((m) => m.enabled && m.module_summary)
            .map((m) => (
              <p key={m.display_name}>
                <b>{m.display_name}</b>：{m.module_summary}
              </p>
            ))}
        </div>
      )}
      {add && (
        <Modal title="添加教学模块" onClose={() => setAdd(false)}>
          <div className="modal-body form-stack">
            <p className="muted">
              补充你希望体现的教学内容，也可以选择常用预设。
            </p>
            <div className="preset-pills">
              {PRESET_MODULES.map(([, label]) => (
                <button
                  className="chip"
                  type="button"
                  key={label}
                  onClick={() => setName(label)}
                >
                  {label}
                </button>
              ))}
            </div>
            <label>
              模块名称
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={20}
                placeholder="例如：跨学科拓展"
              />
            </label>
            <label>
              生成重点
              <textarea
                value={summary}
                onChange={(e) => setSummary(e.target.value)}
                placeholder="希望这个模块包含什么？"
                rows={3}
                maxLength={2000}
              />
            </label>
            {error && <ErrorBox message={error} />}
            <button className="button primary" type="button" onClick={save}>
              添加模块
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}
export function NewLesson({ user }: { user: User }) {
  const navigate = useNavigate();
  const notify = useNotify();
  const [mode, setMode] = useState(
    new URLSearchParams(location.search).get("mode") === "interactive"
      ? "interactive"
      : "one-click",
  );
  const [modules, setModules] = useState(DEFAULT_MODULES);
  const [dictionary, setDictionary] = useState<Dictionary>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [configLoading, setConfigLoading] = useState(true);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [knowledgeBaseIds, setKnowledgeBaseIds] = useState<string[] | null>(
    null,
  );
  const [knowledgeLoading, setKnowledgeLoading] = useState(true);
  const [knowledgeError, setKnowledgeError] = useState("");
  const [knowledgeRevision, setKnowledgeRevision] = useState(0);
  const [form, setForm] = useState({
    topic: "",
    school_stage: user.profile?.school_stage || user.school_stage || "primary",
    grade: user.profile?.grade || user.grade || "grade_4",
    subject: user.profile?.subject || user.subject || "chinese",
    lesson_type: "new_lesson",
    total_duration_min: 40,
    requirements: "",
  });
  useEffect(() => {
    Promise.all([
      api<Dictionary>("/lesson-dictionaries"),
      api<ModuleConfig[] | { modules: ModuleConfig[] }>(
        "/me/lesson-module-config",
      ),
    ])
      .then(([dict, pref]) => {
        setDictionary(dict);
        const list = Array.isArray(pref) ? pref : pref.modules;
        if (list?.length) setModules(list);
      })
      .catch((e) =>
        notify(`配置加载失败：${errorMessage(e)}，请稍后重试。`, true),
      )
      .finally(() => setConfigLoading(false));
  }, [notify]);
  useEffect(() => {
    const controller = new AbortController();
    setKnowledgeLoading(true);
    setKnowledgeError("");
    api<KnowledgeBase[]>("/knowledge-bases", { signal: controller.signal })
      .then((items) => {
        if (!controller.signal.aborted) setKnowledgeBases(items);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setKnowledgeError(errorMessage(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setKnowledgeLoading(false);
      });
    return () => controller.abort();
  }, [knowledgeRevision]);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!modules.some((m) => m.enabled)) {
      setError("请至少选择一个教学模块。");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const data = await post<{ draft_id: string; run_id: string }>(
        `/lesson-drafts/generations/${mode}`,
        {
          ...form,
          knowledge_base_ids: knowledgeBaseIds,
          request_id: crypto.randomUUID(),
          modules: modules.map((m, display_order) => ({ ...m, display_order })),
        },
      );
      navigate(`/lessons/${data.draft_id}?run=${data.run_id}`);
      track("generation_started", {
        draft_id: data.draft_id,
        run_id: data.run_id,
        mode,
      });
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="page narrow-page">
      <PageHeading
        eyebrow="A NEW LESSON BEGINS"
        title="准备一堂新课"
        description="告诉我们你的教学构想，一起把它变成清晰可行的课堂。"
      />
      <form onSubmit={submit}>
        <section className="form-card">
          <div className="form-section-title">
            <span>01</span>
            <div>
              <h2>选择备课方式</h2>
              <p>根据你的节奏，选择合适的创作方式。</p>
            </div>
          </div>
          <div className="mode-grid">
            <button
              type="button"
              className={`mode-card ${mode === "one-click" ? "selected" : ""}`}
              onClick={() => setMode("one-click")}
            >
              <Sparkles size={24} />
              <span>
                <b>一键生成</b>
                <small>一次生成完整教案，再按需调整</small>
              </span>
              <span className="radio-dot" />
            </button>
            <button
              type="button"
              className={`mode-card ${mode === "interactive" ? "selected" : ""}`}
              onClick={() => setMode("interactive")}
            >
              <Waypoints size={24} />
              <span>
                <b>结构化备课</b>
                <small>逐模块生成，确认后继续下一步</small>
              </span>
              <span className="radio-dot" />
            </button>
          </div>
        </section>
        <section className="form-card">
          <div className="form-section-title">
            <span>02</span>
            <div>
              <h2>这堂课的基本信息</h2>
              <p>清楚的起点，让教学设计更有方向。</p>
            </div>
          </div>
          <div className="form-stack">
            <label>
              课题名称 <span className="required">*</span>
              <input
                className="topic-input"
                value={form.topic}
                onChange={(e) => setForm({ ...form, topic: e.target.value })}
                placeholder="例如：观潮、分数的初步认识、光的直线传播"
                required
                maxLength={200}
              />
            </label>
            <div className="field-grid">
              <SelectField
                label="学段"
                value={form.school_stage}
                onChange={(school_stage) => setForm({ ...form, school_stage })}
                choices={options(dictionary.school_stages, [
                  "小学",
                  "初中",
                  "高中",
                ])}
              />
              <SelectField
                label="年级"
                value={form.grade}
                onChange={(grade) => setForm({ ...form, grade })}
                choices={options(dictionary.grades, [
                  "一年级",
                  "二年级",
                  "三年级",
                  "四年级",
                  "五年级",
                  "六年级",
                  "七年级",
                  "八年级",
                  "九年级",
                  "高一",
                  "高二",
                  "高三",
                ])}
              />
              <SelectField
                label="学科"
                value={form.subject}
                onChange={(subject) => setForm({ ...form, subject })}
                choices={options(dictionary.subjects, [
                  "语文",
                  "数学",
                  "英语",
                  "物理",
                  "化学",
                  "生物",
                  "道德与法治",
                  "历史",
                  "地理",
                  "科学",
                  "音乐",
                  "美术",
                  "体育",
                ])}
              />
              <SelectField
                label="课型"
                value={form.lesson_type}
                onChange={(lesson_type) => setForm({ ...form, lesson_type })}
                choices={
                  options(dictionary.lesson_types, []).length
                    ? options(dictionary.lesson_types, [])
                    : [
                        { value: "new_lesson", label: "新授课" },
                        { value: "review_lesson", label: "复习课" },
                        { value: "practice_lesson", label: "练习课" },
                      ]
                }
              />
              <label>
                课时长度（分钟）
                <input
                  type="number"
                  min={5}
                  max={240}
                  value={form.total_duration_min}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      total_duration_min: Number(e.target.value),
                    })
                  }
                  required
                />
              </label>
            </div>
            <label>
              补充要求 <span className="optional">选填</span>
              <textarea
                value={form.requirements}
                onChange={(e) =>
                  setForm({ ...form, requirements: e.target.value })
                }
                placeholder="例如：学生已有的知识基础、课堂资源、教学重点、想尝试的活动……"
                rows={4}
                maxLength={12000}
              />
              <small>若涉及具体教材页码或原文，请在此提供相关内容。</small>
            </label>
          </div>
        </section>
        <section className="form-card">
          <div className="form-section-title">
            <span>03</span>
            <div>
              <h2>安排教学模块</h2>
              <p>
                已选 {modules.filter((m) => m.enabled).length}{" "}
                个模块，可按本次课程需要增减。
              </p>
            </div>
          </div>
          {configLoading ? (
            <Spinner label="读取模块偏好" />
          ) : (
            <ModuleChooser value={modules} onChange={setModules} />
          )}
        </section>
        <section className="form-card knowledge-selection">
          <div className="form-section-title">
            <span>
              <FolderOpen size={18} />
            </span>
            <div>
              <h2>
                选用知识资料 <span className="optional">选填</span>
              </h2>
              <p>默认参考全部知识库，仅使用解析完成且与本次课题相关的片段。</p>
            </div>
          </div>
          <div className="knowledge-selection-toolbar">
            <span>
              {knowledgeBaseIds === null
                ? "参考全部知识库"
                : knowledgeBaseIds.length
                  ? `已选择 ${knowledgeBaseIds.length} 个知识库`
                  : "本次不使用知识资料"}
            </span>
            <div>
              <button
                type="button"
                className="text-button"
                onClick={() => setKnowledgeBaseIds(null)}
              >
                全部选用
              </button>
              <button
                type="button"
                className="text-button"
                onClick={() => setKnowledgeBaseIds([])}
              >
                不使用知识资料
              </button>
              <Link className="text-link" to="/knowledge">
                管理知识库 <ArrowUpRight size={13} />
              </Link>
            </div>
          </div>
          {knowledgeLoading ? (
            <Spinner label="正在读取可选知识库" />
          ) : knowledgeError ? (
            <div className="error-banner" role="alert">
              {knowledgeError}
              <button
                type="button"
                className="text-button"
                onClick={() => setKnowledgeRevision((value) => value + 1)}
              >
                重新读取
              </button>
            </div>
          ) : !knowledgeBases.length ? (
            <p className="knowledge-hint">
              还没有知识库。你可以直接开始备课，或先添加教材和课程参考资料。
            </p>
          ) : (
            <div className="knowledge-base-options">
              {knowledgeBases.map((base) => (
                <label key={base.id}>
                  <input
                    type="checkbox"
                    checked={
                      knowledgeBaseIds === null ||
                      knowledgeBaseIds.includes(base.id)
                    }
                    onChange={(event) =>
                      setKnowledgeBaseIds((current) => {
                        const selected =
                          current ?? knowledgeBases.map((item) => item.id);
                        return event.target.checked
                          ? [
                              ...selected.filter((id) => id !== base.id),
                              base.id,
                            ]
                          : selected.filter((id) => id !== base.id);
                      })
                    }
                  />
                  <span>
                    <b>{base.title}</b>
                    <small>{base.documents_count} 份资料</small>
                  </span>
                </label>
              ))}
            </div>
          )}
        </section>
        {error && <ErrorBox message={error} />}
        <div className="form-actions">
          <span>
            <BookOpen size={16} /> 根据你的选择，生成专属教学初稿
          </span>
          <button className="button primary" disabled={busy || configLoading}>
            <Sparkles size={17} />
            {busy
              ? "正在创建教案…"
              : mode === "interactive"
                ? "开始结构化备课"
                : "生成完整教案"}
            <ArrowRight size={17} />
          </button>
        </div>
      </form>
    </div>
  );
}

export function HistoryPage() {
  const { items, loading, error, reload } = useDrafts();
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [deleting, setDeleting] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const notify = useNotify();
  const filtered = items.filter(
    (d) =>
      (!query ||
        `${d.topic}${displayValue(d.grade)}${displayValue(d.subject)}`.includes(
          query,
        )) &&
      (!status ||
        d.status === status ||
        (status === "failed" && d.status === "failed_retryable")),
  );
  return (
    <div className="page">
      <PageHeading
        eyebrow="YOUR LESSON COLLECTION"
        title="生成历史"
        description="每一份备课，都是教学经验的积累。"
        action={<NewLessonLink />}
      />
      <div className="filter-bar">
        <label className="search-box">
          <Search size={18} />
          <input
            placeholder="搜索课题、学科或年级"
            aria-label="搜索教案"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        <label className="filter-select">
          <SlidersHorizontal size={16} />
          <select
            aria-label="筛选状态"
            value={status}
            onChange={(e) => setStatus(e.target.value)}
          >
            <option value="">全部状态</option>
            <option value="ready">已完成</option>
            <option value="generating">生成中</option>
            <option value="ready_partial">部分完成</option>
            <option value="failed">失败</option>
            <option value="cancelled">已取消</option>
          </select>
        </label>
        <span className="muted">共 {filtered.length} 份教案</span>
      </div>
      {loading ? (
        <Spinner />
      ) : error ? (
        <ErrorBox message={error} retry={reload} />
      ) : filtered.length ? (
        <div className="lesson-grid">
          {filtered.map((d) => (
            <LessonCard key={d.id} draft={d} onDelete={() => setDeleting(d)} />
          ))}
        </div>
      ) : (
        <Empty
          title={query || status ? "没有找到匹配的教案" : "还没有生成过教案"}
          action={<NewLessonLink />}
        >
          换个关键词，或从一份新教案开始。
        </Empty>
      )}
      {deleting && (
        <Modal
          title="删除这份教案？"
          onClose={() => !busy && setDeleting(null)}
        >
          <div className="modal-body">
            <p>
              “{deleting.topic}”将从生成历史中移除。请确认已保存需要的内容。
            </p>
            <div className="modal-actions">
              <button
                className="button secondary"
                onClick={() => setDeleting(null)}
                disabled={busy}
              >
                保留教案
              </button>
              <button
                className="button danger"
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  try {
                    await api(`/lesson-drafts/${deleting.id}`, {
                      method: "DELETE",
                    });
                    setDeleting(null);
                    await reload();
                    notify("教案已删除");
                  } catch (e) {
                    notify(errorMessage(e), true);
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                {busy ? "删除中…" : "确认删除"}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}

type PersonalLesson = {
  id: string;
  filename?: string;
  file_name?: string;
  title?: string;
  status: string;
  parse_status?: string;
  subject?: string;
  grade?: string;
  created_at?: string;
  error?: string;
  error_message?: string;
  style_summary?: string | Record<string, unknown>;
  thumbnail_kind?: string;
  thumbnail_url?: string;
};
function LessonThumbnail({ item }: { item: PersonalLesson }) {
  const [url, setUrl] = useState("");
  useEffect(() => {
    if (!item.thumbnail_url) return;
    const controller = new AbortController();
    let objectUrl = "";
    privateBlob(item.thumbnail_url, controller.signal)
      .then((blob) => {
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => {});
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [item.thumbnail_url]);
  const label =
    item.thumbnail_kind === "parsed_preview" ? "解析内容预览" : "PDF 原始首页";
  return url ? (
    <figure className="lesson-thumbnail">
      <img src={url} alt={label} />
      <figcaption>{label}</figcaption>
    </figure>
  ) : (
    <span className="document-icon">
      <FileText size={25} />
    </span>
  );
}
export function LibraryPage() {
  const [items, setItems] = useState<PersonalLesson[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [uploading, setUploading] = useState(false);
  const [deleting, setDeleting] = useState<PersonalLesson | null>(null);
  const notify = useNotify();
  const reload = useCallback(async () => {
    try {
      setItems(await api<PersonalLesson[]>("/personal-lessons"));
      setError("");
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void reload();
  }, [reload]);
  useEffect(() => {
    if (
      items.some((i) =>
        ["queued", "parsing", "pending", "uploaded", "processing"].includes(
          i.parse_status || i.status,
        ),
      )
    ) {
      const timer = setInterval(() => void reload(), 4000);
      return () => clearInterval(timer);
    }
  }, [items, reload]);
  const upload = async (file?: File) => {
    if (!file) return;
    if (!/\.(docx|pdf)$/i.test(file.name)) {
      notify("请选择 DOCX 或 PDF 教案文件。", true);
      return;
    }
    if (file.size > 20 * 1024 * 1024) {
      notify("文件不能超过 20 MB。", true);
      return;
    }
    setUploading(true);
    const data = new FormData();
    data.append("file", file);
    try {
      await api("/personal-lessons", { method: "POST", body: data });
      notify("上传成功，正在解析教案");
      await reload();
    } catch (e) {
      notify(errorMessage(e), true);
    } finally {
      setUploading(false);
    }
  };
  return (
    <div className="page">
      <PageHeading
        eyebrow="YOUR TEACHING SIGNATURE"
        title="个人教案库"
        description="让过去的用心，成为下一次创作的灵感。"
      />
      <div
        className={`upload-zone ${uploading ? "uploading" : ""}`}
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => {
          e.preventDefault();
          if (!uploading) void upload(e.dataTransfer.files[0]);
        }}
      >
        <span className="upload-icon">
          <Upload size={26} />
        </span>
        <div>
          <h2>{uploading ? "正在上传教案…" : "把你的教学积累带进来"}</h2>
          <p>上传以往教案，生成时将参考适合本次课程的个人教学风格。</p>
          <small>支持 DOCX、PDF · 单个文件不超过 20 MB</small>
        </div>
        <label className={`button primary ${uploading ? "disabled" : ""}`}>
          <Plus size={17} />
          选择教案文件
          <input
            type="file"
            accept=".docx,.pdf"
            disabled={uploading}
            className="visually-hidden"
            onChange={(e) => {
              void upload(e.target.files?.[0]);
              e.target.value = "";
            }}
          />
        </label>
      </div>
      <div className="section-heading">
        <h2>
          我的教学收藏 <span>{items.length} 份</span>
        </h2>
        <span className="muted">识别结果由系统自动提取</span>
      </div>
      {loading ? (
        <Spinner />
      ) : error ? (
        <ErrorBox message={error} retry={reload} />
      ) : !items.length ? (
        <Empty title="收藏你自己的教学风格">
          上传第一份教案，在这里积累专属于你的备课参考。
        </Empty>
      ) : (
        <div className="library-list">
          {items.map((item) => (
            <article className="library-item" key={item.id}>
              <LessonThumbnail item={item} />
              <div className="library-item-info">
                <h3>
                  {item.filename || item.file_name || item.title || "教案文件"}
                </h3>
                <p>
                  {displayValue(item.grade) || "年级未识别"} ·{" "}
                  {displayValue(item.subject) || "学科未识别"} ·{" "}
                  {formatDate(item.created_at)}
                </p>
                {typeof item.style_summary === "string" &&
                  item.style_summary && <small>{item.style_summary}</small>}
                {(item.error || item.error_message) && (
                  <p className="danger-text">
                    {item.error || item.error_message}
                  </p>
                )}
              </div>
              <Status value={item.parse_status || item.status} />
              <button
                className="icon-button"
                title="下载原文件"
                aria-label="下载原文件"
                onClick={() =>
                  download(
                    `/personal-lessons/${item.id}/download`,
                    item.filename || item.file_name || "教案.docx",
                  ).catch((e) => notify(errorMessage(e), true))
                }
              >
                <Download size={17} />
              </button>
              <button
                className="icon-button danger-text"
                title="删除文件"
                aria-label="删除文件"
                onClick={() => setDeleting(item)}
              >
                <Trash2 size={17} />
              </button>
            </article>
          ))}
        </div>
      )}
      {deleting && (
        <Modal title="从教案库移除文件？" onClose={() => setDeleting(null)}>
          <div className="modal-body">
            <p>
              移除后，新的备课将不再使用此文件作为参考。已有教案的风格依据会保留。
            </p>
            <div className="modal-actions">
              <button
                className="button secondary"
                onClick={() => setDeleting(null)}
              >
                保留文件
              </button>
              <button
                className="button danger"
                onClick={async () => {
                  try {
                    await api(`/personal-lessons/${deleting.id}`, {
                      method: "DELETE",
                    });
                    setDeleting(null);
                    await reload();
                    notify("文件已移除");
                  } catch (e) {
                    notify(errorMessage(e), true);
                  }
                }}
              >
                移除文件
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}

export function SettingsPage({
  user,
  onUser,
}: {
  user: User;
  onUser: (user: User) => void;
}) {
  const [form, setForm] = useState({
    name: user.name,
    school_stage: user.profile?.school_stage || user.school_stage || "",
    grade: user.profile?.grade || user.grade || "",
    subject: user.profile?.subject || user.subject || "",
  });
  const [modules, setModules] = useState(DEFAULT_MODULES);
  const [profileDictionary, setProfileDictionary] = useState<Dictionary>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const notify = useNotify();
  useEffect(() => {
    api<Dictionary>("/profile-options")
      .then(setProfileDictionary)
      .catch((e) => setError(errorMessage(e)));
    api<ModuleConfig[] | { modules: ModuleConfig[] }>(
      "/me/lesson-module-config",
    )
      .then((data) => {
        const list = Array.isArray(data) ? data : data.modules;
        if (list?.length) setModules(list);
      })
      .catch((e) => setError(errorMessage(e)))
      .finally(() => setLoading(false));
  }, []);
  return (
    <div className="page narrow-page">
      <PageHeading
        eyebrow="MAKE YOURSELF AT HOME"
        title="个人设置"
        description="让备课空间，更贴合你的教学习惯。"
      />
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          try {
            onUser(
              await patch<User>("/me", {
                name: form.name,
                profile: {
                  school_stage: form.school_stage,
                  grade: form.grade,
                  subject: form.subject,
                },
              }),
            );
            notify("个人资料已保存");
          } catch (e) {
            notify(errorMessage(e), true);
          } finally {
            setBusy(false);
          }
        }}
      >
        <section className="form-card">
          <div className="form-section-title">
            <span>
              <BookOpen size={20} />
            </span>
            <div>
              <h2>教师资料</h2>
              <p>{user.email}</p>
            </div>
          </div>
          <div className="field-grid">
            <label>
              姓名
              <input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                required
                maxLength={80}
              />
            </label>
            <SelectField
              label="任教学段"
              value={form.school_stage}
              onChange={(school_stage) => setForm({ ...form, school_stage })}
              required={false}
              choices={[
                { value: "", label: "暂不设置" },
                ...options(profileDictionary.school_stages, []),
              ]}
            />
            <SelectField
              label="常用年级"
              value={form.grade}
              onChange={(grade) => setForm({ ...form, grade })}
              required={false}
              choices={[
                { value: "", label: "暂不设置" },
                ...options(profileDictionary.grades, []),
              ]}
            />
            <SelectField
              label="任教学科"
              value={form.subject}
              onChange={(subject) => setForm({ ...form, subject })}
              required={false}
              choices={[
                { value: "", label: "暂不设置" },
                ...options(profileDictionary.subjects, []),
              ]}
            />
          </div>
          <div className="card-actions">
            <button className="button primary" disabled={busy}>
              保存资料
            </button>
          </div>
        </section>
      </form>
      <section className="form-card">
        <div className="form-section-title">
          <span>
            <Layers3 size={20} />
          </span>
          <div>
            <h2>默认教学模块</h2>
            <p>用于下一次新建教案，已有教案保持原有配置。</p>
          </div>
        </div>
        {loading ? (
          <Spinner />
        ) : error ? (
          <ErrorBox message={error} />
        ) : (
          <>
            <ModuleChooser value={modules} onChange={setModules} />
            <div className="card-actions">
              <button
                className="button secondary"
                onClick={() => setModules(DEFAULT_MODULES)}
              >
                恢复默认
              </button>
              <button
                className="button primary"
                disabled={busy}
                onClick={async () => {
                  if (!modules.some((m) => m.enabled)) {
                    notify("请至少启用一个模块", true);
                    return;
                  }
                  setBusy(true);
                  try {
                    await api("/me/lesson-module-config", {
                      method: "PUT",
                      body: JSON.stringify({ modules }),
                    });
                    notify("模块偏好已保存");
                  } catch (e) {
                    notify(errorMessage(e), true);
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                保存模块偏好
              </button>
            </div>
          </>
        )}
      </section>
    </div>
  );
}

export function AdminPage() {
  const [tab, setTab] = useState("overview");
  const [data, setData] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [refresh, setRefresh] = useState(0);
  const notify = useNotify();
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    api(`/admin/${tab}`, { signal: controller.signal })
      .then((value) => {
        if (!controller.signal.aborted) setData(value);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(errorMessage(e));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [tab, refresh]);
  const tabs = [
    ["overview", "运行概要"],
    ["users", "用户管理"],
    ["runs", "生成任务"],
    ["issues", "运行问题"],
    ["audit", "审计记录"],
  ];
  const rows = Array.isArray(data) ? (data as Record<string, unknown>[]) : [];
  const filtered = rows.filter((row) =>
    JSON.stringify(row).toLowerCase().includes(search.toLowerCase()),
  );
  const show = (v: unknown): ReactNode =>
    v === null || v === undefined ? (
      "—"
    ) : typeof v === "object" ? (
      <code className="json-cell">{JSON.stringify(v)}</code>
    ) : (
      String(v)
    );
  const columns = rows.length
    ? Object.keys(rows[0]).filter(
        (k) =>
          ![
            "password_hash",
            "password",
            "token",
            "teacher_view_content",
            "content",
            "input",
            "result",
          ].includes(k),
      )
    : [];
  return (
    <div className="page">
      <PageHeading
        eyebrow="SYSTEM OBSERVATORY"
        title="管理控制台"
        description="查看真实运行事实，追踪系统问题与管理操作。"
        action={
          <div className="recovery-actions">
            <Link className="button primary" to="/admin/recovery">
              任务恢复与告警
            </Link>
            <button
              className="button secondary"
              onClick={() => setRefresh((v) => v + 1)}
            >
              刷新数据
            </button>
          </div>
        }
      />
      <div className="tab-bar">
        {tabs.map(([id, label]) => (
          <button
            key={id}
            className={tab === id ? "active" : ""}
            onClick={() => setTab(id)}
          >
            {label}
          </button>
        ))}
        <Link className="admin-execution-log-tab" to="/admin/execution-logs">
          执行日志 <ArrowUpRight size={14} />
        </Link>
      </div>
      {loading ? (
        <Spinner />
      ) : error ? (
        <ErrorBox message={error} retry={() => setRefresh((v) => v + 1)} />
      ) : tab === "overview" ? (
        <div className="overview-grid">
          {data !== null &&
            typeof data === "object" &&
            Object.entries(data).map(([key, value]) => (
              <article className="overview-card" key={key}>
                <span>
                  {(
                    {
                      users: "教师账号",
                      drafts: "教案总数",
                      runs: "生成任务",
                      issues: "运行问题",
                      active_runs: "进行中任务",
                      failed_runs: "失败任务",
                      exports: "导出记录",
                      total_users: "教师账号",
                      total_drafts: "教案总数",
                      total_runs: "生成任务",
                      total_issues: "运行问题",
                    } as Record<string, string>
                  )[key] || key}
                </span>
                <b>{show(value)}</b>
              </article>
            ))}
        </div>
      ) : (
        <>
          <label className="search-box admin-search">
            <Search size={18} />
            <input
              placeholder="在当前数据中搜索"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              aria-label="搜索管理数据"
            />
          </label>
          {!rows.length ? (
            <Empty title="暂无记录">新的系统事实会出现在这里。</Empty>
          ) : (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    {columns.map((c) => (
                      <th key={c}>
                        {(
                          {
                            id: "ID",
                            email: "邮箱",
                            name: "姓名",
                            role: "角色",
                            is_active: "账号可用",
                            status: "状态",
                            created_at: "创建时间",
                            workflow_type: "工作流",
                            code: "问题代码",
                            message: "说明",
                            action: "操作",
                            actor_id: "操作人",
                          } as Record<string, string>
                        )[c] || c}
                      </th>
                    ))}
                    {tab === "users" && <th>操作</th>}
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((row, i) => (
                    <tr key={String(row.id || i)}>
                      {columns.map((c) => (
                        <td key={c}>
                          {c === "status" ? (
                            <Status value={String(row[c])} />
                          ) : (
                            show(row[c])
                          )}
                        </td>
                      ))}
                      {tab === "users" && (
                        <td>
                          <button
                            className="text-button"
                            onClick={async () => {
                              try {
                                await patch(`/admin/users/${row.id}`, {
                                  is_active: row.is_active === false,
                                });
                                setRefresh((v) => v + 1);
                                notify("账号状态已更新");
                              } catch (e) {
                                notify(errorMessage(e), true);
                              }
                            }}
                          >
                            {row.is_active === false ? "启用账号" : "停用账号"}
                          </button>
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}
