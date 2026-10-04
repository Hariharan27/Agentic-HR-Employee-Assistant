import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { getLeaveRequests, getOnboardingStatus, getParkingAdminReservations, getPendingOnboardingApprovals, getProfile, login, sendChat } from "./api";
import type { ChatMessage, LeaveRequest, OnboardingStatus, ParkingReservation, Profile, Source } from "./types";

const demoAccounts = {
  EMPLOYEE: { username: "employee", password: "employee123" },
  MANAGER: { username: "manager", password: "manager123" },
  HR: { username: "hr", password: "hr12345" },
  HR_ADMIN: { username: "hradmin", password: "hradmin123" },
  PARKING_ADMIN: { username: "parkingadmin", password: "parkingadmin123" },
} as const;

const quickPrompts: Record<Profile["role"], string[]> = {
  EMPLOYEE: [
    "What is my leave balance?",
    "What is the casual leave policy?",
    "Reserve parking tomorrow",
  ],
  MANAGER: [
    "Start onboarding a new employee",
    "What's Priya's onboarding status?",
    "Show my pending approvals",
  ],
  HR: [
    "Start onboarding a new employee",
    "What's Priya's onboarding status?",
    "Show my approval queue",
  ],
  HR_ADMIN: [
    "Show pending onboarding approvals",
    "What's Priya's onboarding status?",
    "What is the employee onboarding policy?",
  ],
  PARKING_ADMIN: [
    "Show parking admin queue for today",
    "Check in parking reservation #",
    "Mark parking reservation # as no-show",
  ],
};

function localDateInputValue(date = new Date()) {
  const copy = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return copy.toISOString().slice(0, 10);
}

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
  const [sidePanel, setSidePanel] = useState<"requests" | "onboarding" | "parking">("requests");
  const [onboardingQuery, setOnboardingQuery] = useState("");
  const [onboardingStatus, setOnboardingStatus] = useState<OnboardingStatus | null>(null);
  const [onboardingLoading, setOnboardingLoading] = useState(false);
  const [onboardingError, setOnboardingError] = useState("");
  const [onboardingApprovals, setOnboardingApprovals] = useState<OnboardingStatus[]>([]);
  const [parkingDate, setParkingDate] = useState(localDateInputValue);
  const [parkingReservations, setParkingReservations] = useState<ParkingReservation[]>([]);
  const [parkingLoading, setParkingLoading] = useState(false);
  const [parkingError, setParkingError] = useState("");
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [loginForm, setLoginForm] = useState({ username: "employee", password: "employee123" });
  const messageEnd = useRef<HTMLDivElement>(null);

  const suggestions = useMemo(() => (profile ? quickPrompts[profile.role] : []), [profile]);
  const canCreateOnboarding = profile?.role === "MANAGER" || profile?.role === "HR";
  const canManageOnboarding = canCreateOnboarding || profile?.role === "HR_ADMIN";
  const canManageParking = profile?.role === "PARKING_ADMIN";

  async function refreshOnboardingApprovals(currentToken = token, currentProfile = profile) {
    if (!currentToken || currentProfile?.role !== "HR_ADMIN") return;
    try {
      setOnboardingApprovals(await getPendingOnboardingApprovals(currentToken));
    } catch {
      setOnboardingApprovals([]);
    }
  }

  async function refreshRequests(currentToken = token, currentProfile = profile) {
    if (!currentToken || !currentProfile) return;
    if (currentProfile.role === "PARKING_ADMIN") {
      setRequests([]);
      return;
    }
    try {
      setRequests(await getLeaveRequests(currentToken, currentProfile.role));
    } catch {
      setRequests([]);
    }
  }

  async function refreshParkingReservations(currentToken = token, dateValue = parkingDate) {
    if (!currentToken || !canManageParking) return;
    setParkingLoading(true);
    setParkingError("");
    try {
      setParkingReservations(await getParkingAdminReservations(currentToken, dateValue));
    } catch (nextError) {
      setParkingReservations([]);
      setParkingError(nextError instanceof Error ? nextError.message : "Parking queue could not be loaded");
    } finally {
      setParkingLoading(false);
    }
  }

  useEffect(() => {
    if (!token) return;
    getProfile(token)
      .then((nextProfile) => {
        setProfile(nextProfile);
        if (nextProfile.role === "HR_ADMIN") setSidePanel("onboarding");
        if (nextProfile.role === "PARKING_ADMIN") setSidePanel("parking");
        void refreshRequests(token, nextProfile);
        void refreshOnboardingApprovals(token, nextProfile);
        if (nextProfile.role === "PARKING_ADMIN") {
          void getParkingAdminReservations(token, parkingDate).then(setParkingReservations).catch(() => setParkingReservations([]));
        }
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
    setSidePanel("requests");
    setOnboardingQuery("");
    setOnboardingStatus(null);
    setOnboardingApprovals([]);
    setOnboardingError("");
    setParkingDate(localDateInputValue());
    setParkingReservations([]);
    setParkingError("");
  }

  async function lookupOnboarding(query = onboardingQuery) {
    const normalized = query.trim();
    if (!normalized || !token || !canManageOnboarding) return;
    setOnboardingLoading(true);
    setOnboardingError("");
    try {
      const status = await getOnboardingStatus(token, normalized);
      setOnboardingStatus(status);
      setOnboardingQuery(normalized);
    } catch (nextError) {
      setOnboardingStatus(null);
      setOnboardingError(nextError instanceof Error ? nextError.message : "Onboarding status could not be loaded");
    } finally {
      setOnboardingLoading(false);
    }
  }

  function startOnboarding() {
    setSidePanel("onboarding");
    void submitMessage("Start onboarding a new employee");
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
      if (response.domain === "onboarding") {
        setSidePanel("onboarding");
        const requestId = response.message.match(/Onboarding request #(\d+)/i)?.[1];
        if (requestId) void lookupOnboarding(requestId);
      }
      if (response.domain === "parking" && canManageParking) {
        setSidePanel("parking");
        void refreshParkingReservations();
      }
      await refreshRequests();
      await refreshOnboardingApprovals();
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
                onClick={() => setLoginForm(account)}>{role === "HR_ADMIN" ? "HR Admin" : role === "PARKING_ADMIN" ? "Parking Admin" : role[0] + role.slice(1).toLowerCase()}</button>
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
        <div className="profile-card"><div className="avatar">{profile.name.split(" ").map((part) => part[0]).slice(0, 2).join("")}</div><div><strong>{profile.name}</strong><span>{profile.employee_code} · {profile.role.replaceAll("_", " ")}</span></div></div>
        <nav><button className="nav-active"><span>✦</span> Assistant</button>{!canManageParking && <button onClick={() => { setSidePanel("requests"); void refreshRequests(); }}><span>◷</span> Requests</button>}{canManageOnboarding && <button onClick={() => setSidePanel("onboarding")}><span>◇</span> Onboarding</button>}{canManageParking && <button onClick={() => { setSidePanel("parking"); void refreshParkingReservations(); }}><span>▦</span> Parking</button>}</nav>
        <div className="side-note"><span className="live-dot" />Connected to HR services</div>
        <button className="logout" onClick={logout}>Sign out</button>
      </aside>

      <section className="workspace">
        <header><div><p className="eyebrow dark">IDEATOR HR HELP DESK</p><h2>How can PeopleDesk help today?</h2></div><div className="role-pill">{profile.role === "EMPLOYEE" ? "Employee self-service" : profile.role === "HR_ADMIN" ? "HR administration" : profile.role === "PARKING_ADMIN" ? "Parking administration" : "Manager & HR workspace"}</div></header>
        <div className="content-grid">
          <section className="chat-panel">
            <div className="messages">
              {messages.length === 0 && <div className="welcome"><div className="spark">✦</div><h3>Hello, {profile.name.split(" ")[0]}</h3><p>Ask PeopleDesk about HR policies, balances, leave requests, or workplace parking.{profile.role === "HR_ADMIN" ? " You can also review onboarding requests and activate employee accounts." : canManageParking ? " You can monitor parking arrivals and confirm admin actions before anything changes." : canCreateOnboarding ? " You can also onboard and track new Ideators." : ""} I’ll show sources and confirm before changing anything.</p><div className="suggestions">{suggestions.map((prompt) => <button key={prompt} onClick={() => void submitMessage(prompt)}>{prompt}<span>→</span></button>)}</div></div>}
              {messages.map((message) => <article key={message.id} className={`message ${message.role}${message.intent?.includes("onboarding") ? " onboarding-message" : ""}`}><div className="message-label">{message.role === "assistant" ? "Ideator PeopleDesk" : "You"}{message.intent && <span>{message.intent.replaceAll("_", " ")}</span>}</div><p>{message.text}</p>{message.sources && message.sources.length > 0 && <div className="sources"><strong>Based on</strong>{groupedSourceLabels(message.sources).map((label) => <span key={label}>{label}</span>)}</div>}</article>)}
              {loading && <article className="message assistant typing"><span /><span /><span /></article>}
              <div ref={messageEnd} />
            </div>
            {pendingAction && <div className="pending-banner"><div><strong>Confirmation required</strong><span>{pendingAction}</span></div><div><button onClick={() => void submitMessage("cancel")}>Cancel</button><button className="confirm" onClick={() => void submitMessage("yes")}>Confirm</button></div></div>}
            {error && <p className="error chat-error">{error}</p>}
            <form className="composer" onSubmit={(event) => { event.preventDefault(); void submitMessage(); }}><textarea rows={1} value={input} onChange={(event) => setInput(event.target.value)} placeholder="Ask Ideator PeopleDesk…" onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void submitMessage(); } }} /><button disabled={!input.trim() || loading} aria-label="Send">↑</button></form>
            <p className="disclaimer">Responses are grounded in company policy. Confirm important decisions with HR.</p>
          </section>

          {sidePanel === "parking" && canManageParking ? (
            <aside className="request-panel parking-panel">
              <div className="panel-heading"><div><p className="eyebrow dark">WORKPLACE OPS</p><h3>Parking queue</h3></div><button onClick={() => void refreshParkingReservations()} aria-label="Refresh parking">↻</button></div>
              <div className="parking-content">
                <label className="parking-date">Reservation date<input type="date" value={parkingDate} onChange={(event) => { setParkingDate(event.target.value); void refreshParkingReservations(token, event.target.value); }} /></label>
                {parkingError && <p className="panel-error">{parkingError}</p>}
                {parkingLoading ? <div className="empty-state compact"><p>Loading parking reservations…</p></div> : parkingReservations.length === 0 ? <div className="empty-state compact"><span>✓</span><p>No parking reservations for this date.</p></div> : <div className="parking-list">{parkingReservations.map((reservation) => <article key={reservation.id}><div className="parking-card-head"><strong>#{reservation.id} · {reservation.slot_code}</strong><span className={`status ${reservation.status.toLowerCase()}`}>{reservation.status.replaceAll("_", " ")}</span></div><p>{reservation.employee_name || reservation.employee_code || "Employee"} · {reservation.vehicle_registration || "Vehicle unavailable"}</p><small>{reservation.slot_location}</small><div className="parking-actions">{reservation.status === "RESERVED" && <><button onClick={() => void submitMessage(`Check in parking reservation #${reservation.id}`)}>Check in</button><button onClick={() => void submitMessage(`Mark parking reservation #${reservation.id} as no-show`)}>No-show</button><button className="reject" onClick={() => setInput(`Admin late cancel parking reservation #${reservation.id} because `)}>Cancel</button></>}{reservation.status === "CHECKED_IN" && <button onClick={() => void submitMessage(`Complete parking reservation #${reservation.id}`)}>Complete</button>}{reservation.status === "NO_SHOW" && <button onClick={() => setInput(`Override no-show for parking reservation #${reservation.id} because `)}>Correct</button>}</div></article>)}</div>}
              </div>
            </aside>
          ) : sidePanel === "onboarding" && canManageOnboarding ? (
            <aside className="request-panel onboarding-panel">
              <div className="panel-heading"><div><p className="eyebrow dark">EMPLOYEE JOURNEY</p><h3>{profile.role === "HR_ADMIN" ? "Approval queue" : "Onboarding"}</h3></div><button onClick={() => profile.role === "HR_ADMIN" ? void refreshOnboardingApprovals() : onboardingQuery && void lookupOnboarding()} aria-label="Refresh onboarding">↻</button></div>
              <div className="onboarding-content">
                {canCreateOnboarding && <section className="onboarding-start"><div className="onboarding-icon">+</div><div><strong>Onboard an Ideator</strong><p>PeopleDesk collects the required details and asks for confirmation before creating requests.</p></div><button onClick={startOnboarding} disabled={loading}>Start guided onboarding</button></section>}
                {profile.role === "HR_ADMIN" && <section className="approval-list">{onboardingApprovals.length === 0 ? <div className="onboarding-empty"><span>✓</span><p>No onboarding requests are waiting for approval.</p></div> : onboardingApprovals.map((item) => <article key={item.id}><div><strong>#{item.id} · {item.candidate.name}</strong><span>{item.candidate.designation}</span><small>Joins {item.candidate.joining_date}</small></div><div><button className="reject" onClick={() => setInput(`Reject onboarding request #${item.id} because `)}>Reject</button><button className="approve" onClick={() => void submitMessage(`Approve onboarding request #${item.id}`)}>Approve</button></div></article>)}</section>}
                <form className="onboarding-search" onSubmit={(event) => { event.preventDefault(); void lookupOnboarding(); }}><label htmlFor="onboarding-query">Track onboarding</label><div><input id="onboarding-query" value={onboardingQuery} onChange={(event) => setOnboardingQuery(event.target.value)} placeholder="Name, email, or request ID" /><button disabled={!onboardingQuery.trim() || onboardingLoading}>{onboardingLoading ? "…" : "→"}</button></div></form>
                {onboardingError && <p className="panel-error">{onboardingError}</p>}
                {!onboardingStatus && !onboardingError && <div className="onboarding-empty"><span>◇</span><p>Search for an Ideator to view onboarding progress and provisioning tasks.</p></div>}
                {onboardingStatus && <section className="onboarding-status-card"><div className="onboarding-person"><div className="avatar">{onboardingStatus.candidate.name.split(" ").map((part) => part[0]).slice(0, 2).join("")}</div><div><strong>{onboardingStatus.candidate.name}</strong><span>{onboardingStatus.candidate.designation}</span></div><span className={`status ${onboardingStatus.status.toLowerCase()}`}>{onboardingStatus.status.replaceAll("_", " ")}</span></div><div className="onboarding-meta"><span>Joining<strong>{onboardingStatus.candidate.joining_date}</strong></span><span>Location<strong>{onboardingStatus.candidate.location}</strong></span><span>Manager<strong>{onboardingStatus.candidate.reporting_manager}</strong></span></div><div className="progress-heading"><span>Provisioning progress</span><strong>{onboardingStatus.completed_tasks}/{onboardingStatus.total_tasks}</strong></div><div className="progress-track"><span style={{ width: `${onboardingStatus.total_tasks ? (onboardingStatus.completed_tasks / onboardingStatus.total_tasks) * 100 : 0}%` }} /></div><div className="onboarding-tasks">{onboardingStatus.tasks.map((task) => <article key={task.id}><span className={`task-check ${task.status.toLowerCase()}`}>{task.status === "COMPLETED" ? "✓" : "·"}</span><div><strong>{task.title}</strong><small>{task.status.replaceAll("_", " ")}</small></div></article>)}</div></section>}
              </div>
            </aside>
          ) : (
            <aside className="request-panel"><div className="panel-heading"><div><p className="eyebrow dark">LIVE DATA</p><h3>{profile.role === "EMPLOYEE" ? "My requests" : "Approval queue"}</h3></div><button onClick={() => void refreshRequests()} aria-label="Refresh">↻</button></div>{requests.length === 0 ? <div className="empty-state"><span>✓</span><p>{profile.role === "EMPLOYEE" ? "No leave requests yet" : "No pending approvals"}</p></div> : <div className="request-list">{requests.slice(0, 8).map((request) => <article key={request.id}><div><strong>#{request.id} · {request.leave_type.toLowerCase()}</strong><span className={`status ${request.status.toLowerCase()}`}>{request.status}</span></div>{request.employee_name && profile.role !== "EMPLOYEE" && <p>{request.employee_name} · {request.employee_code}</p>}<p>{request.start_date} → {request.end_date}</p><small>{request.working_days} working day(s)</small></article>)}</div>}</aside>
          )}
        </div>
      </section>
    </main>
  );
}

export default App;
