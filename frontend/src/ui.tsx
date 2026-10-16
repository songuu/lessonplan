import { createContext, useContext, useEffect, useRef } from "react";
import type { ReactNode } from "react";
import { LoaderCircle, Inbox, X, ArrowRight } from "lucide-react";
import { Link } from "react-router-dom";
export const NotifyContext = createContext<
  (message: string, error?: boolean) => void
>(() => {});
export const useNotify = () => useContext(NotifyContext);
export function Spinner({ label = "正在加载" }: { label?: string }) {
  return (
    <div className="loading" role="status">
      <LoaderCircle size={22} className="spin" />
      {label}
    </div>
  );
}
export function Empty({
  title,
  children,
  action,
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <span className="empty-icon">
        <Inbox size={26} />
      </span>
      <h3>{title}</h3>
      <p>{children}</p>
      {action}
    </div>
  );
}
export function PageHeading({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow: string;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <header className="page-heading">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {action}
    </header>
  );
}
export function Modal({
  title,
  children,
  onClose,
  wide = false,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
}) {
  const dialogRef = useRef<HTMLElement>(null);
  const previousFocus = useRef(document.activeElement as HTMLElement | null);
  useEffect(() => {
    return () => previousFocus.current?.focus();
  }, []);
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <section
        ref={dialogRef}
        className={`modal ${wide ? "modal-wide" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onKeyDown={(e) => {
          if (e.key === "Escape") onClose();
          if (e.key === "Tab") {
            const items = Array.from(
              dialogRef.current?.querySelectorAll<HTMLElement>(
                'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [contenteditable="true"]',
              ) || [],
            ).filter((item) => item.offsetParent !== null);
            const first = items[0],
              last = items[items.length - 1];
            if (e.shiftKey && document.activeElement === first) {
              e.preventDefault();
              last?.focus();
            } else if (!e.shiftKey && document.activeElement === last) {
              e.preventDefault();
              first?.focus();
            }
          }
        }}
      >
        <header>
          <h2>{title}</h2>
          <button
            className="icon-button"
            aria-label="关闭"
            onClick={onClose}
            autoFocus
          >
            <X size={20} />
          </button>
        </header>
        {children}
      </section>
    </div>
  );
}
export function Status({ value }: { value?: string }) {
  const labels: Record<string, string> = {
    ready: "已完成",
    completed: "已完成",
    confirmed: "已确认",
    pending: "待生成",
    queued: "排队中",
    running: "生成中",
    generating: "生成中",
    failed: "失败",
    failed_retryable: "可重试",
    creating: "备课中",
    processing: "处理中",
    success: "已完成",
    cancelled: "已取消",
    ready_partial: "部分完成",
    draft: "草稿",
    awaiting_confirmation: "待确认",
    stale: "需复核",
    unknown: "需复核",
    parsing: "解析中",
    parsed: "已解析",
    done: "已完成",
    active: "正常",
    disabled: "已停用",
  };
  return (
    <span className={`status status-${value || "draft"}`}>
      {labels[value || ""] || value || "草稿"}
    </span>
  );
}
export function NewLessonLink() {
  return (
    <Link className="button primary" to="/new">
      新建教案 <ArrowRight size={16} />
    </Link>
  );
}
export function formatDate(value?: string) {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime())
    ? "—"
    : d.toLocaleDateString("zh-CN", { month: "long", day: "numeric" });
}
