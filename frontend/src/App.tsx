import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import {
  BrowserRouter,
  Link,
  NavLink,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import {
  BookOpen,
  LayoutDashboard,
  Plus,
  History,
  Library,
  FileSearch,
  Settings,
  LogOut,
  ArrowUpRight,
  PanelLeftClose,
  PanelLeftOpen,
  ShieldCheck,
  X,
  CheckCircle2,
  AlertCircle,
  Leaf,
  Activity,
} from "lucide-react";
import { api, getToken, post, setToken } from "./api";
import { displayValue } from "./catalog";
import type { User } from "./types";
import { NotifyContext, Spinner } from "./ui";
import {
  Dashboard,
  HistoryPage,
  LibraryPage,
  SettingsPage,
  AdminPage,
  NewLesson,
} from "./Pages";
const LessonPage = lazy(() =>
  import("./Lesson").then((m) => ({ default: m.LessonPage })),
);
const KnowledgePage = lazy(() =>
  import("./Knowledge").then((module) => ({ default: module.KnowledgePage })),
);
const RecoveryPage = lazy(() =>
  import("./Recovery").then((module) => ({ default: module.RecoveryPage })),
);
const ExecutionLogsPage = lazy(() =>
  import("./ExecutionLogs").then((module) => ({ default: module.ExecutionLogsPage })),
);
export function App() {
  return (
    <BrowserRouter>
      <Application />
    </BrowserRouter>
  );
}
function Application() {
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(!!getToken());
  const [collapsed, setCollapsed] = useState(false);
  const [toasts, setToasts] = useState<
    { id: number; message: string; error?: boolean }[]
  >([]);
  const navigate = useNavigate();
  const location = useLocation();
  const notify = useCallback((message: string, error = false) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, message, error }]);
    setTimeout(() => setToasts((t) => t.filter((v) => v.id !== id)), 6500);
  }, []);
  useEffect(() => {
    if (getToken())
      api<User>("/me")
        .then(setUser)
        .catch(() => setToken(""))
        .finally(() => setChecking(false));
    const expired = () => {
      setToken("");
      setUser(null);
      notify("登录已过期，请重新登录。", true);
    };
    window.addEventListener("lesson:unauthorized", expired);
    return () => window.removeEventListener("lesson:unauthorized", expired);
  }, [notify]);
  const logout = async () => {
    try {
      await post("/auth/logout");
      setToken("");
      setUser(null);
      navigate("/");
    } catch (e) {
      notify(e instanceof Error ? e.message : "退出失败", true);
    }
  };
  const nav = [
    { to: "/", icon: LayoutDashboard, label: "备课工作台", end: true },
    { to: "/new", icon: Plus, label: "新建教案" },
    { to: "/history", icon: History, label: "生成历史" },
    { to: "/library", icon: Library, label: "个人教案库" },
    { to: "/knowledge", icon: FileSearch, label: "知识库" },
    { to: "/settings", icon: Settings, label: "个人设置" },
  ];
  return (
    <NotifyContext.Provider value={notify}>
      {checking ? (
        <div className="full-loading">
          <Spinner label="正在打开备课工作台" />
        </div>
      ) : !user ? (
        <Auth onLogin={setUser} />
      ) : (
        <div className={`app-shell ${collapsed ? "is-collapsed" : ""}`}>
          <aside className="sidebar">
            <Link to="/" className="brand">
              <span className="brand-mark">
                <BookOpen size={23} />
              </span>
              <span>
                教案助手<small>LESSON ATELIER</small>
              </span>
            </Link>
            <div className="sidebar-label">我的教学空间</div>
            <nav aria-label="主导航">
              {nav.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  title={item.label}
                >
                  <item.icon size={19} />
                  <span>{item.label}</span>
                  {item.to === "/new" && <kbd>＋</kbd>}
                </NavLink>
              ))}
              {user.role === "admin" && (
                <>
                  <NavLink to="/admin" end>
                    <ShieldCheck size={19} />
                    <span>管理控制台</span>
                  </NavLink>
                  <NavLink to="/admin/recovery">
                    <Activity size={19} />
                    <span>任务恢复与告警</span>
                  </NavLink>
                  <NavLink to="/admin/execution-logs">
                    <Activity size={19} />
                    <span>执行日志</span>
                  </NavLink>
                </>
              )}
            </nav>
            <div className="sidebar-note">
              <Leaf size={24} />
              <p>
                把时间留给思考，
                <br />
                把灵感带进课堂。
              </p>
              <small>每一堂课，都值得认真准备。</small>
            </div>
            <div className="sidebar-user">
              <span className="avatar">{user.name?.slice(0, 1) || "师"}</span>
              <span>
                <b>{user.name || "老师"}</b>
                <small>
                  {displayValue(user.profile?.subject || user.subject) ||
                    "我的教师账号"}
                </small>
              </span>
              <button
                className="icon-button"
                aria-label="退出登录"
                title="退出登录"
                onClick={logout}
              >
                <LogOut size={17} />
              </button>
            </div>
          </aside>
          <div className="app-body">
            <div className="topbar">
              <button
                className="icon-button"
                onClick={() => setCollapsed(!collapsed)}
                aria-label={collapsed ? "展开导航" : "收起导航"}
              >
                {collapsed ? (
                  <PanelLeftOpen size={19} />
                ) : (
                  <PanelLeftClose size={19} />
                )}
              </button>
              <span>
                我的教学空间 <span className="slash">/</span>{" "}
                {location.pathname === "/admin/execution-logs"
                  ? "执行日志"
                  : location.pathname === "/admin/recovery"
                  ? "任务恢复与告警"
                  : location.pathname.startsWith("/lessons/")
                    ? "教案编辑"
                    : nav.find((n) => n.to === location.pathname)?.label ||
                      "管理控制台"}
              </span>
              <span className="topbar-end">
                <i /> 让备课更从容
              </span>
            </div>
            <main id="main">
              <Suspense fallback={<Spinner label="正在打开教学空间" />}>
                <Routes>
                  <Route path="/" element={<Dashboard user={user} />} />
                  <Route path="/new" element={<NewLesson user={user} />} />
                  <Route path="/history" element={<HistoryPage />} />
                  <Route path="/library" element={<LibraryPage />} />
                  <Route path="/knowledge" element={<KnowledgePage />} />
                  <Route
                    path="/settings"
                    element={<SettingsPage user={user} onUser={setUser} />}
                  />
                  <Route path="/lessons/:id" element={<LessonPage />} />
                  <Route path="/admin/execution-logs" element={<ExecutionLogsPage user={user} />} />
                  <Route
                    path="/admin/recovery"
                    element={<RecoveryPage user={user} />}
                  />
                  <Route
                    path="/admin/*"
                    element={
                      user.role === "admin" ? (
                        <AdminPage />
                      ) : (
                        <div className="error-banner">无管理端访问权限</div>
                      )
                    }
                  />
                  <Route
                    path="*"
                    element={
                      <div className="empty">
                        <h1>未找到此页面</h1>
                        <Link to="/">返回工作台</Link>
                      </div>
                    }
                  />
                </Routes>
              </Suspense>
            </main>
            <footer className="app-footer">
              <span>
                教案助手 <span>·</span> 让教学灵感从容发生
              </span>
              <span>AI 辅助备课，教学内容请结合实际审核</span>
            </footer>
          </div>
        </div>
      )}
      <div className="toasts" aria-live="polite">
        {toasts.map((t) => (
          <div
            key={t.id}
            role={t.error ? "alert" : "status"}
            className={`toast ${t.error ? "toast-error" : ""}`}
          >
            {t.error ? <AlertCircle size={19} /> : <CheckCircle2 size={19} />}
            <span>{t.message}</span>
            <button
              className="icon-button"
              aria-label="关闭提示"
              onClick={() => setToasts((v) => v.filter((x) => x.id !== t.id))}
            >
              <X size={16} />
            </button>
          </div>
        ))}
      </div>
    </NotifyContext.Provider>
  );
}
function Auth({ onLogin }: { onLogin: (u: User) => void }) {
  const [register, setRegister] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [form, setForm] = useState({ name: "", email: "", password: "" });
  return (
    <div className="auth-page">
      <section className="auth-story">
        <Link to="/" className="brand">
          <span className="brand-mark">
            <BookOpen size={25} />
          </span>
          <span>
            教案助手<small>LESSON ATELIER</small>
          </span>
        </Link>
        <span className="eyebrow">为教学，留一份从容</span>
        <h1>
          好课堂，
          <br />
          从一个好想法开始。
        </h1>
        <p>
          从第一束灵感到完整教学设计，
          <br />
          陪你认真打磨每一堂课。
        </p>
        <div className="auth-illustration" aria-hidden="true">
          <div className="illustration-leaf">✳</div>
          <div className="illustration-paper">
            <span>LESSON NOTES</span>
            <b>
              让思考
              <br />
              在课堂发生。
            </b>
            <i />
            <i />
            <i />
          </div>
          <div className="illustration-stamp">
            备课中
            <br />
            TEACH WITH CARE
          </div>
        </div>
        <small>专属于教师的备课空间</small>
      </section>
      <section className="auth-form">
        <div className="auth-form-inner">
          <span className="eyebrow">欢迎来到你的教学空间</span>
          <h2>{register ? "开启备课新篇章" : "很高兴，再次见面"}</h2>
          <p>
            {register
              ? "创建账号，开始准备你的下一堂好课。"
              : "登录后，继续打磨你的教学灵感。"}
          </p>
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              setBusy(true);
              setError("");
              try {
                const result = await post<{ access_token: string; user: User }>(
                  `/auth/${register ? "register" : "login"}`,
                  register
                    ? form
                    : { email: form.email, password: form.password },
                );
                setToken(result.access_token);
                onLogin(result.user);
              } catch (e) {
                setError(e instanceof Error ? e.message : "登录失败");
              } finally {
                setBusy(false);
              }
            }}
          >
            {register && (
              <label>
                姓名
                <input
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  placeholder="老师，怎么称呼你？"
                  required
                  maxLength={80}
                  autoComplete="name"
                />
              </label>
            )}
            <label>
              邮箱
              <input
                type="email"
                value={form.email}
                onChange={(e) => setForm({ ...form, email: e.target.value })}
                placeholder="teacher@example.com"
                required
                autoComplete="email"
              />
            </label>
            <label>
              密码
              <input
                type="password"
                value={form.password}
                onChange={(e) => setForm({ ...form, password: e.target.value })}
                placeholder="请输入密码"
                minLength={8}
                maxLength={128}
                required
                autoComplete={register ? "new-password" : "current-password"}
              />
            </label>
            {error && (
              <div className="error-banner" role="alert">
                {error}
              </div>
            )}
            <button className="button primary auth-submit" disabled={busy}>
              {busy ? "正在进入…" : register ? "创建账号" : "登录工作台"}
              <ArrowUpRight size={19} />
            </button>
          </form>
          <p className="auth-switch">
            {register ? "已有账号？" : "第一次来到这里？"}
            <button
              className="text-button"
              onClick={() => {
                setRegister(!register);
                setError("");
              }}
            >
              {register ? "前往登录" : "创建账号"}
            </button>
          </p>
          <div className="auth-footnote">
            <ShieldCheck size={15} /> 你的教案和创作，只属于你。
          </div>
        </div>
      </section>
    </div>
  );
}
