import { useEffect, useRef, useState } from "react";
import {
  BookOpen,
  ChevronLeft,
  ChevronRight,
  FileSearch,
  FileText,
  FolderOpen,
  Layers3,
  Pencil,
  Plus,
  RotateCcw,
  Search,
  Trash2,
  Upload,
} from "lucide-react";
import { api, patch, post } from "./api";
import type { KnowledgeBase, KnowledgeChunk, KnowledgeDocument } from "./types";
import { Empty, Modal, PageHeading, Spinner, useNotify } from "./ui";

const errorText = (error: unknown) =>
  error instanceof Error ? error.message : "请求未完成，请稍后重试";
const pending = (item: KnowledgeDocument) =>
  item.status === "uploaded" || item.status === "processing";
const statuses: Record<KnowledgeDocument["status"], string> = {
  uploaded: "等待解析",
  processing: "正在解析",
  ready: "可检索",
  failed: "解析失败",
};
const CHUNK_PAGE_SIZE = 10;
export function KnowledgePage() {
  const notify = useNotify();
  const [bases, setBases] = useState<KnowledgeBase[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [basesLoading, setBasesLoading] = useState(true);
  const [basesError, setBasesError] = useState("");
  const [basesRevision, setBasesRevision] = useState(0);
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [docsLoading, setDocsLoading] = useState(false);
  const [docsError, setDocsError] = useState("");
  const [docsRevision, setDocsRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [edit, setEdit] = useState<{
    id?: string;
    title: string;
    description: string;
  } | null>(null);
  const [editError, setEditError] = useState("");
  const [deleteBase, setDeleteBase] = useState<KnowledgeBase | null>(null);
  const [deleteDocument, setDeleteDocument] =
    useState<KnowledgeDocument | null>(null);
  const [inspected, setInspected] = useState<KnowledgeDocument | null>(null);
  const [offset, setOffset] = useState(0);
  const [chunks, setChunks] = useState<KnowledgeChunk[]>([]);
  const [chunksTotal, setChunksTotal] = useState(0);
  const [chunksLoading, setChunksLoading] = useState(false);
  const [chunksError, setChunksError] = useState("");
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(5);
  const [results, setResults] = useState<KnowledgeChunk[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState("");
  const searchController = useRef<AbortController | null>(null);
  const selected = bases.find((base) => base.id === selectedId);
  useEffect(() => {
    const controller = new AbortController();
    setBasesLoading(true);
    setBasesError("");
    api<KnowledgeBase[]>("/knowledge-bases", { signal: controller.signal })
      .then((items) => {
        if (controller.signal.aborted) return;
        setBases(items);
        setSelectedId((current) =>
          items.some((base) => base.id === current)
            ? current
            : items[0]?.id || "",
        );
      })
      .catch((error) => {
        if (!controller.signal.aborted) setBasesError(errorText(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setBasesLoading(false);
      });
    return () => controller.abort();
  }, [basesRevision]);
  useEffect(() => {
    searchController.current?.abort();
    setResults(null);
    setSearchError("");
    setSearching(false);
    setInspected(null);
    setDocuments([]);
  }, [selectedId]);
  useEffect(() => {
    if (!selectedId) return;
    const controller = new AbortController();
    setDocsLoading(true);
    setDocsError("");
    api<KnowledgeDocument[]>(`/knowledge-bases/${selectedId}/documents`, {
      signal: controller.signal,
    })
      .then((items) => {
        if (!controller.signal.aborted) setDocuments(items);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setDocsError(errorText(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setDocsLoading(false);
      });
    return () => controller.abort();
  }, [selectedId, docsRevision]);
  useEffect(() => {
    if (docsLoading || !documents.some(pending)) return;
    const timer = setTimeout(() => setDocsRevision((value) => value + 1), 2500);
    return () => clearTimeout(timer);
  }, [documents, docsLoading]);
  useEffect(() => {
    if (!inspected) return;
    const controller = new AbortController();
    setChunksLoading(true);
    setChunksError("");
    setChunks([]);
    Promise.all([
      api<KnowledgeDocument>(`/knowledge-documents/${inspected.id}`, {
        signal: controller.signal,
      }),
      api<{ items: KnowledgeChunk[]; total: number }>(
        `/knowledge-documents/${inspected.id}/chunks?offset=${offset}&limit=${CHUNK_PAGE_SIZE}`,
        { signal: controller.signal },
      ),
    ])
      .then(([document, page]) => {
        if (controller.signal.aborted) return;
        setInspected(document);
        setChunks(page.items);
        setChunksTotal(page.total);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setChunksError(errorText(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setChunksLoading(false);
      });
    return () => controller.abort();
  }, [inspected?.id, offset]);
  useEffect(() => () => searchController.current?.abort(), []);
  const reloadDocuments = () => {
    setDocsRevision((value) => value + 1);
    setBasesRevision((value) => value + 1);
  };
  const upload = async (file?: File) => {
    if (!file || !selectedId) return;
    if (!/\.(docx|pdf|txt|md)$/i.test(file.name)) {
      notify("请选择 DOCX、PDF、TXT 或 Markdown 文件。", true);
      return;
    }
    if (file.size > 20 * 1024 * 1024) {
      notify("文件不能超过 20 MB。", true);
      return;
    }
    setBusy(true);
    const form = new FormData();
    form.append("file", file);
    try {
      await api<KnowledgeDocument>(`/knowledge-bases/${selectedId}/documents`, {
        method: "POST",
        body: form,
      });
      notify("资料已上传，正在准备可检索片段。");
      reloadDocuments();
    } catch (error) {
      notify(errorText(error), true);
    } finally {
      setBusy(false);
    }
  };
  const search = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!selectedId || !query.trim()) return;
    searchController.current?.abort();
    const controller = new AbortController();
    searchController.current = controller;
    setSearching(true);
    setSearchError("");
    try {
      const result = await api<{
        items: KnowledgeChunk[];
        method: "lexical_bm25";
      }>(`/knowledge-bases/${selectedId}/search`, {
        method: "POST",
        body: JSON.stringify({ query: query.trim(), top_k: topK }),
        signal: controller.signal,
      });
      if (!controller.signal.aborted) setResults(result.items);
    } catch (error) {
      if (!controller.signal.aborted) setSearchError(errorText(error));
    } finally {
      if (!controller.signal.aborted) setSearching(false);
    }
  };
  return (
    <div className="page knowledge-page">
      <PageHeading
        eyebrow="YOUR TEACHING REFERENCES"
        title="知识库"
        description="整理教材、课程标准与教学资料，让备课参考有据可循。"
        action={
          <button
            className="button primary"
            disabled={busy}
            onClick={() => {
              setEdit({ title: "", description: "" });
              setEditError("");
            }}
          >
            <Plus size={16} />
            新建知识库
          </button>
        }
      />
      {basesError && (
        <div className="error-banner" role="alert">
          {basesError}
          <button
            className="text-button"
            onClick={() => setBasesRevision((value) => value + 1)}
          >
            重新加载
          </button>
        </div>
      )}
      {basesLoading && !bases.length ? (
        <Spinner label="正在读取知识库" />
      ) : !bases.length && !basesError ? (
        <Empty
          title="为教学参考留一个位置"
          action={
            <button
              className="button primary"
              onClick={() => setEdit({ title: "", description: "" })}
            >
              <Plus size={16} />
              创建第一个知识库
            </button>
          }
        >
          把教材与课程材料整理在一起，生成教案时可选择需要的资料。
        </Empty>
      ) : (
        <div className="knowledge-layout">
          <aside className="knowledge-bases">
            <div className="knowledge-list-heading">
              <FolderOpen size={16} />
              我的知识库<span>{bases.length}</span>
            </div>
            <nav aria-label="知识库列表">
              {bases.map((base) => (
                <button
                  key={base.id}
                  aria-label={`选择知识库 ${base.title}`}
                  aria-current={selectedId === base.id ? "true" : undefined}
                  className={selectedId === base.id ? "selected" : ""}
                  disabled={busy}
                  onClick={() => setSelectedId(base.id)}
                >
                  <BookOpen size={18} />
                  <span>
                    <b>{base.title}</b>
                    <small>{base.documents_count} 份资料</small>
                  </span>
                  <ChevronRight size={14} />
                </button>
              ))}
            </nav>
            <p>
              仅你本人可访问这些资料。
              <br />
              个人教案库仍用于教学风格参考。
            </p>
          </aside>
          <section className="knowledge-content">
            {selected && (
              <>
                <div className="knowledge-base-heading">
                  <div>
                    <span className="eyebrow">KNOWLEDGE COLLECTION</span>
                    <h2>{selected.title}</h2>
                    <p>
                      {selected.description ||
                        "添加教学资料，为这一主题积累可查阅的参考。"}
                    </p>
                  </div>
                  <div>
                    <button
                      className="icon-button"
                      title="编辑知识库"
                      aria-label="编辑知识库"
                      disabled={busy}
                      onClick={() => {
                        setEdit({
                          id: selected.id,
                          title: selected.title,
                          description: selected.description,
                        });
                        setEditError("");
                      }}
                    >
                      <Pencil size={17} />
                    </button>
                    <button
                      className="icon-button danger-text"
                      title="删除知识库"
                      aria-label="删除知识库"
                      disabled={busy}
                      onClick={() => setDeleteBase(selected)}
                    >
                      <Trash2 size={17} />
                    </button>
                  </div>
                </div>
                <section className="knowledge-documents">
                  <div className="section-heading">
                    <h2>
                      参考资料 <span>{documents.length} 份</span>
                    </h2>
                    <label
                      className={`button secondary small ${busy ? "disabled" : ""}`}
                    >
                      <Upload size={15} />
                      上传资料
                      <input
                        className="visually-hidden"
                        type="file"
                        accept=".docx,.pdf,.txt,.md"
                        disabled={busy}
                        aria-label="上传知识资料"
                        onChange={(event) => {
                          void upload(event.target.files?.[0]);
                          event.target.value = "";
                        }}
                      />
                    </label>
                  </div>
                  <p className="knowledge-hint">
                    支持 DOCX、PDF、TXT、Markdown，单个文件不超过 20
                    MB。解析完成后才能检索。
                  </p>
                  {docsError ? (
                    <div className="error-banner" role="alert">
                      {docsError}
                      <button
                        className="text-button"
                        onClick={() => setDocsRevision((value) => value + 1)}
                      >
                        重试
                      </button>
                    </div>
                  ) : docsLoading && !documents.length ? (
                    <Spinner label="正在读取参考资料" />
                  ) : !documents.length ? (
                    <Empty title="这里还没有参考资料">
                      上传一份教材、课程标准或讲义，开始积累教学依据。
                    </Empty>
                  ) : (
                    <div className="knowledge-document-list">
                      {documents.map((document) => (
                        <article
                          className="knowledge-document"
                          key={document.id}
                        >
                          <span className="document-icon">
                            <FileText size={22} />
                          </span>
                          <div className="knowledge-document-info">
                            <h3>{document.filename}</h3>
                            <p>
                              {document.status === "ready"
                                ? `${document.chunk_count} 个可检索片段`
                                : statuses[document.status]}
                            </p>
                            {document.error_message && (
                              <p className="danger-text">
                                {document.error_message}
                              </p>
                            )}
                          </div>
                          <span className={`status status-${document.status}`}>
                            {statuses[document.status]}
                          </span>
                          <div className="knowledge-document-actions">
                            {document.status === "failed" && (
                              <button
                                className="button secondary small"
                                disabled={busy}
                                onClick={async () => {
                                  setBusy(true);
                                  try {
                                    await post(
                                      `/knowledge-documents/${document.id}/reindex`,
                                    );
                                    reloadDocuments();
                                    notify("已重新安排资料解析。");
                                  } catch (error) {
                                    notify(errorText(error), true);
                                  } finally {
                                    setBusy(false);
                                  }
                                }}
                              >
                                <RotateCcw size={14} />
                                重新解析
                              </button>
                            )}
                            <button
                              className="button secondary small"
                              disabled={busy || document.status !== "ready"}
                              onClick={() => {
                                setOffset(0);
                                setChunksTotal(0);
                                setInspected(document);
                              }}
                            >
                              <Layers3 size={14} />
                              查看片段
                            </button>
                            <button
                              className="icon-button danger-text"
                              aria-label={`删除资料 ${document.filename}`}
                              disabled={busy}
                              onClick={() => setDeleteDocument(document)}
                            >
                              <Trash2 size={16} />
                            </button>
                          </div>
                        </article>
                      ))}
                    </div>
                  )}
                </section>
                <section className="knowledge-search">
                  <div className="section-heading">
                    <div>
                      <h2>
                        <FileSearch size={20} />
                        试着找一段教学依据
                      </h2>
                      <p>按关键词相关性检索，结果保留原文和来源。</p>
                    </div>
                  </div>
                  <form
                    aria-label="知识检索测试"
                    className="knowledge-search-form"
                    onSubmit={search}
                  >
                    <label className="search-box">
                      <Search size={17} />
                      <input
                        aria-label="知识检索问题"
                        placeholder="例如：如何判断学生理解了平均分？"
                        value={query}
                        onChange={(event) => setQuery(event.target.value)}
                        maxLength={1000}
                        required
                      />
                    </label>
                    <label className="knowledge-topk">
                      <span>最多</span>
                      <select
                        aria-label="检索结果数量"
                        value={topK}
                        onChange={(event) =>
                          setTopK(Number(event.target.value))
                        }
                      >
                        {[3, 5, 10, 20].map((value) => (
                          <option key={value} value={value}>
                            {value} 条
                          </option>
                        ))}
                      </select>
                    </label>
                    <button
                      className="button primary"
                      disabled={searching || !query.trim()}
                    >
                      <Search size={16} />
                      {searching ? "查找中…" : "查找片段"}
                    </button>
                  </form>
                  {searchError && (
                    <div className="error-banner" role="alert">
                      {searchError}
                    </div>
                  )}
                  {searching ? (
                    <Spinner label="正在查找相关片段" />
                  ) : results !== null && !results.length ? (
                    <Empty title="没有找到相关片段">
                      尝试使用教材中的关键词，或检查资料是否已解析完成。
                    </Empty>
                  ) : (
                    results && (
                      <div className="knowledge-results" aria-live="polite">
                        <p className="knowledge-hint">
                          找到 {results.length} 条参考 · 关键词相关性检索
                        </p>
                        {results.map((item, index) => (
                          <article className="knowledge-result" key={item.id}>
                            <header>
                              <span>{String(index + 1).padStart(2, "0")}</span>
                              <div>
                                <b>{item.filename}</b>
                                <small>
                                  {item.base_name} · 片段 {item.ordinal + 1}
                                </small>
                              </div>
                              <button
                                className="text-button"
                                disabled={busy}
                                onClick={() => {
                                  const doc = documents.find(
                                    (document) =>
                                      document.id === item.document_id,
                                  );
                                  setOffset(
                                    Math.floor(
                                      Math.max(item.ordinal, 0) /
                                        CHUNK_PAGE_SIZE,
                                    ) * CHUNK_PAGE_SIZE,
                                  );
                                  setInspected(
                                    doc || {
                                      id: item.document_id,
                                      base_id: item.base_id,
                                      filename: item.filename,
                                      status: "ready",
                                      chunk_count: 0,
                                    },
                                  );
                                }}
                              >
                                查看来源 <ChevronRight size={13} />
                              </button>
                            </header>
                            <p>{item.text}</p>
                          </article>
                        ))}
                      </div>
                    )
                  )}
                </section>
              </>
            )}
          </section>
        </div>
      )}
      {edit && (
        <Modal
          title={edit.id ? "编辑知识库" : "新建知识库"}
          onClose={() => !busy && setEdit(null)}
        >
          <form
            className="modal-body form-stack"
            aria-label="知识库资料"
            onSubmit={async (event) => {
              event.preventDefault();
              if (!edit.title.trim()) return;
              setBusy(true);
              setEditError("");
              try {
                const body = {
                  title: edit.title.trim(),
                  description: edit.description.trim(),
                };
                const base = edit.id
                  ? await patch<KnowledgeBase>(
                      `/knowledge-bases/${edit.id}`,
                      body,
                    )
                  : await post<KnowledgeBase>("/knowledge-bases", body);
                setEdit(null);
                setSelectedId(base.id);
                setBasesRevision((value) => value + 1);
                notify(
                  edit.id
                    ? "知识库资料已更新。"
                    : "知识库已创建，可以添加参考资料。",
                );
              } catch (error) {
                setEditError(errorText(error));
              } finally {
                setBusy(false);
              }
            }}
          >
            <label>
              知识库名称
              <input
                aria-label="知识库名称"
                value={edit.title}
                onChange={(event) =>
                  setEdit({ ...edit, title: event.target.value })
                }
                required
                maxLength={120}
                placeholder="例如：小学数学课程资料"
                disabled={busy}
              />
            </label>
            <label>
              说明
              <textarea
                aria-label="知识库说明"
                value={edit.description}
                onChange={(event) =>
                  setEdit({ ...edit, description: event.target.value })
                }
                maxLength={2000}
                rows={3}
                placeholder="这些资料适合哪些教学主题？"
                disabled={busy}
              />
            </label>
            {editError && (
              <div className="error-banner" role="alert">
                {editError}
              </div>
            )}
            <div className="modal-actions">
              <button
                type="button"
                className="button secondary"
                disabled={busy}
                onClick={() => setEdit(null)}
              >
                取消
              </button>
              <button
                className="button primary"
                disabled={busy || !edit.title.trim()}
              >
                {busy ? "保存中…" : "保存知识库"}
              </button>
            </div>
          </form>
        </Modal>
      )}
      {deleteBase && (
        <Modal
          title="删除这个知识库？"
          onClose={() => !busy && setDeleteBase(null)}
        >
          <div className="modal-body">
            <p>
              将删除“{deleteBase.title}
              ”及其中的参考资料。已有教案保留创建时的引用记录。
            </p>
            <div className="modal-actions">
              <button
                className="button secondary"
                disabled={busy}
                onClick={() => setDeleteBase(null)}
              >
                保留知识库
              </button>
              <button
                className="button danger"
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  try {
                    await api(`/knowledge-bases/${deleteBase.id}`, {
                      method: "DELETE",
                    });
                    setDeleteBase(null);
                    setResults(null);
                    setDocuments([]);
                    setSelectedId("");
                    setBasesRevision((value) => value + 1);
                    notify("知识库已删除。");
                  } catch (error) {
                    notify(errorText(error), true);
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                确认删除知识库
              </button>
            </div>
          </div>
        </Modal>
      )}
      {deleteDocument && (
        <Modal
          title="删除这份参考资料？"
          onClose={() => !busy && setDeleteDocument(null)}
        >
          <div className="modal-body">
            <p>
              “{deleteDocument.filename}
              ”将不再参与新的知识检索。已有教案中的引用不受影响。
            </p>
            <div className="modal-actions">
              <button
                className="button secondary"
                disabled={busy}
                onClick={() => setDeleteDocument(null)}
              >
                保留资料
              </button>
              <button
                className="button danger"
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  try {
                    await api(`/knowledge-documents/${deleteDocument.id}`, {
                      method: "DELETE",
                    });
                    setDeleteDocument(null);
                    setDocuments((items) =>
                      items.filter((item) => item.id !== deleteDocument.id),
                    );
                    setResults(null);
                    reloadDocuments();
                    notify("参考资料已删除。");
                  } catch (error) {
                    notify(errorText(error), true);
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                确认删除资料
              </button>
            </div>
          </div>
        </Modal>
      )}
      {inspected && (
        <Modal
          title={`${inspected.filename} · 可检索片段`}
          onClose={() => setInspected(null)}
          wide
        >
          <div className="modal-body knowledge-chunks">
            <p className="knowledge-hint">
              {inspected.chunk_count || chunksTotal} 个片段 ·
              以下显示解析后的资料原文。
            </p>
            {chunksError ? (
              <div className="error-banner" role="alert">
                {chunksError}
              </div>
            ) : chunksLoading ? (
              <Spinner label="正在读取原文片段" />
            ) : chunks.length ? (
              chunks.map((item) => (
                <article className="knowledge-chunk" key={item.id}>
                  <header>
                    <b>片段 {item.ordinal + 1}</b>
                    <span>
                      {item.base_name} · {item.filename}
                    </span>
                  </header>
                  <p>{item.text}</p>
                </article>
              ))
            ) : (
              <Empty title="暂时没有可查看的片段">
                请检查文档解析状态，必要时重新解析。
              </Empty>
            )}
            <div className="knowledge-pagination">
              <span>
                第 {Math.floor(offset / CHUNK_PAGE_SIZE) + 1} /{" "}
                {Math.max(1, Math.ceil(chunksTotal / CHUNK_PAGE_SIZE))} 页 · 共{" "}
                {chunksTotal} 个片段
              </span>
              <div>
                <button
                  className="button secondary small"
                  disabled={chunksLoading || offset === 0}
                  onClick={() =>
                    setOffset((value) => Math.max(0, value - CHUNK_PAGE_SIZE))
                  }
                >
                  <ChevronLeft size={14} />
                  上一页
                </button>
                <button
                  className="button secondary small"
                  disabled={
                    chunksLoading || offset + CHUNK_PAGE_SIZE >= chunksTotal
                  }
                  onClick={() => setOffset((value) => value + CHUNK_PAGE_SIZE)}
                >
                  下一页
                  <ChevronRight size={14} />
                </button>
              </div>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
