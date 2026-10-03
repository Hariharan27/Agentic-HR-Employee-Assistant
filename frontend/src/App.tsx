import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { getLeaveRequests, getProfile, login, sendChat } from "./api";
import type { ChatMessage, LeaveRequest, Profile, Source } from "./types";

const demoAccounts = {
  EMPLOYEE: { username: "employee", password: "employee123" },
  MANAGER: { username: "manager", password: "manager123" },
  HR: { username: "hr", password: "hr12345" },
} as const;

const quickPrompts: Record<Profile["role"], string[]> = {
  EMPLOYEE: [
    "What is my leave balance?",
    "What is the casual leave policy?",
    "Show my recent leave requests",
  ],
  MANAGER: [
    "Show my pending approvals",
    "What is the casual leave policy?",
    "Show my leave balance",
  ],
  HR: [
    "Show my approval queue",
    "What is the notice period leave policy?",
    "Show my leave balance",
  ],
};

function groupedSourceLabels(sources: Source[]) {
  const documents = new Map<string, Set<number>>();
  for (const source of sources) {
    const document = source.document || "Company policy";
    const pages = documents.get(document) || new Set<number>();
    if (source.page) pages.add(source.page);
    documents.set(document, pages);
  }
  return [...documents.entries()].map(([document, pages]) => {
    const sortedPages = [...pages].sort((a, b) => a - b);
    const pageLabel = sortedPages.length > 0 ? ` · ${sortedPages.length === 1 ? "p." : "pp."} ${sortedPages.join(", ")}` : "";
    return `${document}${pageLabel}`;
  });
}

function App() {
  const [token, setToken] = useState(() => sessionStorage.getItem("hr-token") || "");
  const [profile, setProfile] = useState<Profile | null>(null);
  const [sessionId, setSessionId] = useState<string>();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [requests, setRequests] = useState<LeaveRequest[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [loginForm, setLoginForm] = useState({ username: "employee", password: "employee123" });
  const messageEnd = useRef<HTMLDivElement>(null);

  const suggestions = useMemo(() => (profile ? quickPrompts[profile.role] : []), [profile]);

  async function refreshRequests(currentToken = token, currentProfile = profile) {
    if (!currentToken || !currentProfile) return;
    try {
      setRequests(await getLeaveRequests(currentToken, currentProfile.role));
    } catch {
      setRequests([]);
    }
  }

  useEffect(() => {
    if (!token) return;
    getProfile(token)
      .then((nextProfile) => {
        setProfile(nextProfile);
        void refreshRequests(token, nextProfile);
      })
      .catch(() => logout());
  }, [token]);

  useEffect(() => {
    const target = messageEnd.current;
    if (target && typeof target.scrollIntoView === "function") {
      target.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, loading]);

  async function handleLogin(event: FormEvent) {
    event.preventDefault();
    setLoading(true);
    setError("");
    try {
      const result = await login(loginForm.username, loginForm.password);
      sessionStorage.setItem("hr-token", result.access_token);
      setToken(result.access_token);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "Login failed");
    } finally {
      setLoading(false);
    }
  }

  function logout() {
    sessionStorage.removeItem("hr-token");
    setToken("");
    setProfile(null);
    setSessionId(undefined);
    setMessages([]);
    setPendingAction(null);
    setRequests([]);
  }

  async function submitMessage(text = input) {
    const normalized = text.trim();
    if (!normalized || !token || loading) return;
    setInput("");
    setError("");
    setMessages((current) => [
      ...current,
      { id: crypto.randomUUID(), role: "user", text: normalized },
    ]);
    setLoading(true);
    try {
      const response = await sendChat(token, normalized, sessionId);
      setSessionId(response.session_id);
      setPendingAction(response.pending_action || null);
      setMessages((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: "assistant",
          text: response.message,
          sources: response.sources,
          intent: response.intent,
        },
      ]);
      await refreshRequests();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "The assistant could not respond");
    } finally {
      setLoading(false);
    }
  }

  if (!profile) {
    return (
      <main className="login-page">
        <section className="login-story">
          <div className="brand-mark">I</div>
          <p className="eyebrow">IDEATOR PEOPLEDESK</p>
          <h1>HR help for Ideators, without the waiting.</h1>
          <p className="lead">Your secure ideas2it HR help desk, grounded in company policy and connected to real employee workflows.</p>
          <div className="trust-row"><span>Policy grounded</span><span>Authenticated</span><span>Auditable</span></div>
        </section>
        <section className="login-card">
          <p className="eyebrow dark">WELCOME BACK</p>
          <h2>Sign in to your workspace</h2>
          <p className="muted">Use a demo identity to explore role-aware HR workflows.</p>
          <div className="role-switcher">
            {Object.entries(demoAccounts).map(([role, account]) => (
              <button key={role} type="button" className={loginForm.username === account.username ? "active" : ""}
                onClick={() => setLoginForm(account)}>{role[0] + role.slice(1).toLowerCase()}</button>
            ))}
          </div>
          <form onSubmit={handleLogin}>
            <label>Username<input value={loginForm.username} onChange={(event) => setLoginForm({ ...loginForm, username: event.target.value })} /></label>
            <label>Password<input type="password" value={loginForm.password} onChange={(event) => setLoginForm({ ...loginForm, password: event.target.value })} /></label>
            {error && <p className="error">{error}</p>}
            <button className="primary" disabled={loading}>{loading ? "Signing in…" : "Sign in"}</button>
          </form>
          <p className="security-note">Demo credentials only · JWT authenticated session</p>
        </section>
      </main>
    );
  }

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand"><div className="brand-mark small">I</div><div><strong>Ideator PeopleDesk</strong><span>by ideas2it</span></div></div>
        <div className="profile-card"><div className="avatar">{profile.name.split(" ").map((part) => part[0]).slice(0, 2).join("")}</div><div><strong>{profile.name}</strong><span>{profile.employee_code} · {profile.role}</span></div></div>
        <nav><button className="nav-active"><span>✦</span> Assistant</button><button onClick={() => void refreshRequests()}><span>◷</span> Requests</button></nav>
        <div className="side-note"><span className="live-dot" />Connected to HR services</div>
        <button className="logout" onClick={logout}>Sign out</button>
      </aside>

      <section className="workspace">
        <header><div><p className="eyebrow dark">IDEATOR HR HELP DESK</p><h2>How can PeopleDesk help today?</h2></div><div className="role-pill">{profile.role === "EMPLOYEE" ? "Employee self-service" : "Approval workspace"}</div></header>
        <div className="content-grid">
          <section className="chat-panel">
            <div className="messages">
              {messages.length === 0 && <div className="welcome"><div className="spark">✦</div><h3>Hello, {profile.name.split(" ")[0]}</h3><p>Ask PeopleDesk about HR policies, balances, eligibility, or leave requests. I’ll show sources and confirm before changing anything.</p><div className="suggestions">{suggestions.map((prompt) => <button key={prompt} onClick={() => void submitMessage(prompt)}>{prompt}<span>→</span></button>)}</div></div>}
              {messages.map((message) => <article key={message.id} className={`message ${message.role}`}><div className="message-label">{message.role === "assistant" ? "Ideator PeopleDesk" : "You"}{message.intent && <span>{message.intent.replaceAll("_", " ")}</span>}</div><p>{message.text}</p>{message.sources && message.sources.length > 0 && <div className="sources"><strong>Based on</strong>{groupedSourceLabels(message.sources).map((label) => <span key={label}>{label}</span>)}</div>}</article>)}
              {loading && <article className="message assistant typing"><span /><span /><span /></article>}
              <div ref={messageEnd} />
            </div>
            {pendingAction && <div className="pending-banner"><div><strong>Confirmation required</strong><span>{pendingAction}</span></div><div><button onClick={() => void submitMessage("cancel")}>Cancel</button><button className="confirm" onClick={() => void submitMessage("yes")}>Confirm</button></div></div>}
            {error && <p className="error chat-error">{error}</p>}
            <form className="composer" onSubmit={(event) => { event.preventDefault(); void submitMessage(); }}><textarea rows={1} value={input} onChange={(event) => setInput(event.target.value)} placeholder="Ask Ideator PeopleDesk…" onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void submitMessage(); } }} /><button disabled={!input.trim() || loading} aria-label="Send">↑</button></form>
            <p className="disclaimer">Responses are grounded in company policy. Confirm important decisions with HR.</p>
          </section>

          <aside className="request-panel"><div className="panel-heading"><div><p className="eyebrow dark">LIVE DATA</p><h3>{profile.role === "EMPLOYEE" ? "My requests" : "Approval queue"}</h3></div><button onClick={() => void refreshRequests()} aria-label="Refresh">↻</button></div>{requests.length === 0 ? <div className="empty-state"><span>✓</span><p>{profile.role === "EMPLOYEE" ? "No leave requests yet" : "No pending approvals"}</p></div> : <div className="request-list">{requests.slice(0, 8).map((request) => <article key={request.id}><div><strong>#{request.id} · {request.leave_type.toLowerCase()}</strong><span className={`status ${request.status.toLowerCase()}`}>{request.status}</span></div>{request.employee_name && profile.role !== "EMPLOYEE" && <p>{request.employee_name} · {request.employee_code}</p>}<p>{request.start_date} → {request.end_date}</p><small>{request.working_days} working day(s)</small></article>)}</div>}</aside>
        </div>
      </section>
    </main>
  );
}

export default App;
