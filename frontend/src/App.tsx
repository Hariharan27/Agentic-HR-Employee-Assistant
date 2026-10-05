import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { HttpError, changePassword, getMyVehicles, getOnboardingStatus, getParkingAdminReservations, getPendingOnboardingApprovals, getProfile, getReportingManagers, login, sendChatStream } from "./api";
import { RichText } from "./RichText";
import type { Vehicle, ChatMessage, LiveStep, OnboardingFormPayload, OnboardingStatus, ParkingReservation, Profile, ReportingManager, Source } from "./types";

const quickPrompts: Record<Profile["role"], string[]> = {
  EMPLOYEE: [
    "How many leaves do I have left?",
    "Can I take a casual leave next Tuesday and Sunday?",
    "If I don't use my casual leave, does it carry over to next year?",
    "Which parking slots are free today?",
  ],
  MANAGER: [
    "Show my pending approvals",
    "I need to onboard a new employee",
    "How many leaves do I have left?",
  ],
  HR: [
    "I need to onboard a new employee",
    "Show my approval queue",
    "Can I work from home? What does the hybrid policy say?",
  ],
  HR_ADMIN: [
    "Which onboarding requests are waiting for my approval?",
    "What does the code of conduct say about conflicts of interest?",
    "How many leaves do I have left?",
  ],
  PARKING_ADMIN: [
    "Show today's parking queue",
    "Show parking admin queue for tomorrow",
    "How many leaves do I have left?",
  ],
};

const emptyOnboardingForm = {
  name: "",
  email: "",
  accountRole: "EMPLOYEE",
  designation: "",
  department: "",
  reportingManager: "",
  joiningDate: "",
  location: "Chennai",
  employmentType: "Permanent",
};

const emptyVehicleForm = {
  registrationNumber: "",
  vehicleType: "CAR",
  makeModel: "",
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

const CHAT_KEY = "peopledesk-chat";

type SavedChat = { token: string; sessionId?: string; messages: ChatMessage[]; pendingAction: string | null };

function loadChat(token: string): SavedChat | null {
  try {
    const saved = JSON.parse(sessionStorage.getItem(CHAT_KEY) || "null") as SavedChat | null;
    return saved && token && saved.token === token ? saved : null;
  } catch {
    return null;
  }
}

function saveChat(token: string, sessionId: string | undefined, messages: ChatMessage[], pendingAction: string | null) {
  try {
    sessionStorage.setItem(CHAT_KEY, JSON.stringify({ token, sessionId, messages: messages.slice(-60), pendingAction }));
  } catch {
    // Storage can be unavailable (private mode); the chat still works without it.
  }
}

function App() {
  const [token, setToken] = useState(() => sessionStorage.getItem("hr-token") || "");
  const [profile, setProfile] = useState<Profile | null>(null);
  const restored = useMemo(() => loadChat(sessionStorage.getItem("hr-token") || ""), []);
  const [sessionId, setSessionId] = useState<string | undefined>(restored?.sessionId);
  const [messages, setMessages] = useState<ChatMessage[]>(restored?.messages || []);
  const [pendingAction, setPendingAction] = useState<string | null>(restored?.pendingAction || null);
  const [sidePanel, setSidePanel] = useState<"onboarding" | "parking">("onboarding");
  const [onboardingQuery, setOnboardingQuery] = useState("");
  const [onboardingStatus, setOnboardingStatus] = useState<OnboardingStatus | null>(null);
  const [onboardingLoading, setOnboardingLoading] = useState(false);
  const [onboardingError, setOnboardingError] = useState("");
  const [onboardingApprovals, setOnboardingApprovals] = useState<OnboardingStatus[]>([]);
  const [reportingManagers, setReportingManagers] = useState<ReportingManager[]>([]);
  const [onboardingForm, setOnboardingForm] = useState(emptyOnboardingForm);
  const [onboardingFormError, setOnboardingFormError] = useState("");
  const [vehicleForm, setVehicleForm] = useState(emptyVehicleForm);
  const [vehicleFormError, setVehicleFormError] = useState("");
  const [parkingDate, setParkingDate] = useState(localDateInputValue);
  const [parkingReservations, setParkingReservations] = useState<ParkingReservation[]>([]);
  const [parkingLoading, setParkingLoading] = useState(false);
  const [parkingError, setParkingError] = useState("");
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [liveSteps, setLiveSteps] = useState<LiveStep[]>([]);
  const [myVehicles, setMyVehicles] = useState<Vehicle[]>([]);
  const [passwordForm, setPasswordForm] = useState({ current: "", next: "", confirm: "" });
  const [passwordError, setPasswordError] = useState("");
  const [error, setError] = useState("");
  const [loginForm, setLoginForm] = useState({ username: "", password: "" });
  const messageEnd = useRef<HTMLDivElement>(null);

  const suggestions = useMemo(() => (profile ? quickPrompts[profile.role] : []), [profile]);
  const canCreateOnboarding = profile?.role === "MANAGER" || profile?.role === "HR";
  const canManageOnboarding = canCreateOnboarding || profile?.role === "HR_ADMIN";
  const canManageParking = profile?.role === "PARKING_ADMIN";
  const showSidePanel = (sidePanel === "parking" && canManageParking) || (sidePanel === "onboarding" && canManageOnboarding);

  async function refreshOnboardingApprovals(currentToken = token, currentProfile = profile) {
    if (!currentToken || currentProfile?.role !== "HR_ADMIN") return;
    try {
      setOnboardingApprovals(await getPendingOnboardingApprovals(currentToken));
    } catch {
      setOnboardingApprovals([]);
    }
  }

  async function refreshReportingManagers(currentToken = token, currentProfile = profile) {
    if (!currentToken || !currentProfile || !["MANAGER", "HR", "HR_ADMIN"].includes(currentProfile.role)) return;
    try {
      const managers = await getReportingManagers(currentToken);
      setReportingManagers(managers);
      setOnboardingForm((current) => ({
        ...current,
        reportingManager: current.reportingManager || managers[0]?.name || "",
      }));
    } catch {
      setReportingManagers([]);
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
        void refreshReportingManagers(token, nextProfile);
        void refreshOnboardingApprovals(token, nextProfile);
        if (nextProfile.role === "PARKING_ADMIN") {
          void getParkingAdminReservations(token, parkingDate).then(setParkingReservations).catch(() => setParkingReservations([]));
        }
      })
      .catch(() => logout());
  }, [token]);

  // Keep the conversation for this browser tab, so a refresh mid-demo keeps the session and its
  // pending confirmation. Cleared on sign-out and "New chat".
  useEffect(() => {
    if (token) saveChat(token, sessionId, messages, pendingAction);
  }, [token, sessionId, messages, pendingAction]);

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
      // A temporary password must be replaced first; keep it to pre-fill "current password".
      setPasswordForm({ current: result.must_change_password ? loginForm.password : "", next: "", confirm: "" });
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
    sessionStorage.removeItem(CHAT_KEY);
    setLoginForm({ username: "", password: "" });
    setToken("");
    setProfile(null);
    setSessionId(undefined);
    setMessages([]);
    setPendingAction(null);
    setSidePanel("onboarding");
    setOnboardingQuery("");
    setOnboardingStatus(null);
    setOnboardingApprovals([]);
    setOnboardingError("");
    setReportingManagers([]);
    setOnboardingForm(emptyOnboardingForm);
    setOnboardingFormError("");
    setVehicleForm(emptyVehicleForm);
    setVehicleFormError("");
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
    setOnboardingFormError("");
    void submitMessage("Start onboarding a new employee");
  }

  function updateOnboardingForm(field: keyof typeof emptyOnboardingForm, value: string) {
    setOnboardingForm((current) => ({ ...current, [field]: value }));
  }

  async function submitOnboardingForm(event: FormEvent) {
    event.preventDefault();
    if (!canCreateOnboarding) return;
    const values = Object.fromEntries(
      Object.entries(onboardingForm).map(([key, value]) => [key, value.trim()])
    ) as typeof onboardingForm;
    const missing = [
      ["name", "name"],
      ["email", "email"],
      ["accountRole", "account role"],
      ["designation", "designation"],
      ["department", "department"],
      ["reportingManager", "reporting manager"],
      ["joiningDate", "joining date"],
      ["location", "location"],
      ["employmentType", "employment type"],
    ].filter(([key]) => !values[key as keyof typeof onboardingForm]).map(([, label]) => label);
    if (missing.length > 0) {
      setOnboardingFormError(`Please provide ${missing.join(", ")}.`);
      return;
    }
    setOnboardingFormError("");
    await submitMessage(`Submitted the onboarding form for ${values.name}`, {
      name: values.name,
      email: values.email,
      designation: values.designation,
      department: values.department,
      reporting_manager: values.reportingManager,
      joining_date: values.joiningDate,
      location: values.location,
      employment_type: values.employmentType,
    });
  }

  function renderOnboardingForm() {
    return (
      <form className="onboarding-form chat-onboarding-form" onSubmit={(event) => void submitOnboardingForm(event)}>
        <label>Name<input value={onboardingForm.name} onChange={(event) => updateOnboardingForm("name", event.target.value)} placeholder="New employee name" /></label>
        <label>Email<input type="email" value={onboardingForm.email} onChange={(event) => updateOnboardingForm("email", event.target.value)} placeholder="name@ideas2it.com" /></label>
        <label>Account role<select value={onboardingForm.accountRole} onChange={(event) => updateOnboardingForm("accountRole", event.target.value)}><option value="EMPLOYEE">Employee</option></select></label>
        <label>Designation<input value={onboardingForm.designation} onChange={(event) => updateOnboardingForm("designation", event.target.value)} placeholder="Software Engineer" /></label>
        <label>Department<input value={onboardingForm.department} onChange={(event) => updateOnboardingForm("department", event.target.value)} placeholder="Engineering" /></label>
        <label>Reporting manager<select value={onboardingForm.reportingManager} onChange={(event) => updateOnboardingForm("reportingManager", event.target.value)}>{reportingManagers.length === 0 ? <option value="">No managers available</option> : reportingManagers.map((manager) => <option key={manager.id} value={manager.name}>{manager.name}{manager.employee_code ? ` · ${manager.employee_code}` : ""}</option>)}</select></label>
        <div className="onboarding-form-grid"><label>Joining date<input type="date" min={localDateInputValue()} value={onboardingForm.joiningDate} onChange={(event) => updateOnboardingForm("joiningDate", event.target.value)} /></label><label>Employment type<select value={onboardingForm.employmentType} onChange={(event) => updateOnboardingForm("employmentType", event.target.value)}><option>Permanent</option><option>Contract</option><option>Intern</option></select></label></div>
        <label>Location<select value={onboardingForm.location} onChange={(event) => updateOnboardingForm("location", event.target.value)}><option>Chennai</option><option>Bengaluru</option></select></label>
        {onboardingFormError && <p className="panel-error">{onboardingFormError}</p>}
        <button className="submit-onboarding" disabled={loading || reportingManagers.length === 0}>Create request for confirmation</button>
      </form>
    );
  }

  function shouldRenderOnboardingForm(message: ChatMessage) {
    return (
      message.role === "assistant" &&
      (message.showOnboardingForm ||
        message.intent === "start_onboarding" ||
        message.text.toLowerCase().includes("onboarding form below"))
    );
  }

  function updateVehicleForm(field: keyof typeof emptyVehicleForm, value: string) {
    setVehicleForm((current) => ({ ...current, [field]: value }));
  }

  async function submitVehicleForm(event: FormEvent) {
    event.preventDefault();
    const registrationNumber = vehicleForm.registrationNumber.trim();
    const makeModel = vehicleForm.makeModel.trim();
    if (!registrationNumber) {
      setVehicleFormError("Please provide the vehicle registration number.");
      return;
    }
    setVehicleFormError("");
    await submitMessage(
      [
        "Register vehicle with these details:",
        `registration number: ${registrationNumber}`,
        `vehicle type: ${vehicleForm.vehicleType}`,
        makeModel ? `make and model: ${makeModel}` : "",
      ].filter(Boolean).join("; ")
    );
  }

  function renderVehicleForm() {
    return (
      <form className="onboarding-form chat-onboarding-form vehicle-form" onSubmit={(event) => void submitVehicleForm(event)}>
        {myVehicles.length > 0 && <div className="vehicle-picks"><span>Your vehicles ({myVehicles.length} of 2). Pick one to update, or enter a new registration to add:</span>{myVehicles.map((vehicle) => <button type="button" key={vehicle.registration_number} className={vehicleForm.registrationNumber === vehicle.registration_number ? "active" : ""} onClick={() => setVehicleForm({ registrationNumber: vehicle.registration_number, vehicleType: vehicle.vehicle_type, makeModel: vehicle.make_model || "" })}>{vehicle.registration_number}{vehicle.make_model ? ` · ${vehicle.make_model}` : ""}</button>)}</div>}
        <label>Registration number<input value={vehicleForm.registrationNumber} onChange={(event) => updateVehicleForm("registrationNumber", event.target.value)} placeholder="TN01AB1234" autoCapitalize="characters" /></label>
        <label>Vehicle type<select value={vehicleForm.vehicleType} onChange={(event) => updateVehicleForm("vehicleType", event.target.value)}><option value="CAR">Car</option><option value="MOTORCYCLE">Motorcycle</option></select></label>
        <label>Make and model (optional)<input value={vehicleForm.makeModel} onChange={(event) => updateVehicleForm("makeModel", event.target.value)} placeholder="Hyundai i20" /></label>
        {vehicleFormError && <p className="panel-error">{vehicleFormError}</p>}
        <button className="submit-onboarding" disabled={loading}>Continue to confirmation</button>
      </form>
    );
  }

  function shouldRenderVehicleForm(message: ChatMessage) {
    return (
      message.role === "assistant" &&
      (message.showVehicleForm ||
        message.intent === "register_vehicle" ||
        message.text.toLowerCase().includes("vehicle registration form below"))
    );
  }

  async function submitMessage(text = input, onboardingPayload?: OnboardingFormPayload) {
    const normalized = text.trim();
    if (!normalized || !token || loading) return;
    setInput("");
    setError("");
    setMessages((current) => [
      ...current,
      { id: crypto.randomUUID(), role: "user", text: normalized },
    ]);
    setLoading(true);
    setLiveSteps([{ id: "route", label: "Understanding your request", status: "running" }]);
    try {
      // Live steps replace each other by id (running → success/error) as the agent works.
      const onStep = (step: LiveStep) =>
        setLiveSteps((current) => {
          const index = current.findIndex((item) => item.id === step.id);
          if (index < 0) return [...current, step];
          const next = [...current];
          next[index] = step;
          return next;
        });
      const response = await sendChatStream(token, normalized, onStep, sessionId, onboardingPayload);
      setSessionId(response.session_id);
      const draft = response.onboarding_draft;
      if (draft) {
        // Details given in chat pre-fill the form; anything the user already typed in the form wins.
        setOnboardingForm((current) => ({
          ...current,
          name: current.name || draft.name || "",
          email: current.email || draft.email || "",
          designation: current.designation || draft.designation || "",
          department: current.department || draft.department || "",
          reportingManager: draft.reporting_manager || current.reportingManager,
          joiningDate: current.joiningDate || draft.joining_date || "",
          location: draft.location || current.location,
          employmentType: draft.employment_type || current.employmentType,
        }));
      }
      setPendingAction(response.pending_action || null);
      const showOnboardingForm = response.domain === "onboarding" && response.intent === "start_onboarding" && !response.pending_action;
      const showVehicleForm = response.domain === "parking" && response.intent === "register_vehicle" && !response.pending_action;
      setMessages((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: "assistant",
          text: response.message,
          sources: response.sources,
          intent: response.intent,
          agentActivity: response.agent_activity,
          showOnboardingForm,
          showVehicleForm,
        },
      ]);
      if (showOnboardingForm) {
        setOnboardingFormError("");
        void refreshReportingManagers();
      }
      if (showVehicleForm) {
        setVehicleFormError("");
        // Pre-fill: list the employee's vehicles; an update request names (or has only) one of them.
        void getMyVehicles(token).then((vehicles) => {
          setMyVehicles(vehicles);
          const compact = normalized.replace(/[\s-]/g, "").toUpperCase();
          const named = vehicles.find((vehicle) => compact.includes(vehicle.registration_number));
          const wantsUpdate = /\b(update|edit|change)\b/i.test(normalized);
          const target = named || (wantsUpdate && vehicles.length === 1 ? vehicles[0] : undefined);
          setVehicleForm(target
            ? { registrationNumber: target.registration_number, vehicleType: target.vehicle_type, makeModel: target.make_model || "" }
            : emptyVehicleForm);
        }).catch(() => setMyVehicles([]));
      }
      if (response.domain === "onboarding") {
        setSidePanel("onboarding");
        const requestId = response.message.match(/Onboarding request #(\d+)/i)?.[1];
        if (requestId) void lookupOnboarding(requestId);
      }
      if (response.domain === "parking" && canManageParking) {
        setSidePanel("parking");
        void refreshParkingReservations();
      }
      await refreshOnboardingApprovals();
    } catch (nextError) {
      if (nextError instanceof HttpError && nextError.status === 403 && nextError.code !== "password_change_required") {
        setMessages((current) => [...current, { id: crypto.randomUUID(), role: "assistant", text: nextError.message, kind: "denied" }]);
      } else {
        setError(nextError instanceof Error ? nextError.message : "The assistant could not respond");
      }
    } finally {
      setLoading(false);
      setLiveSteps([]);
    }
  }

  function startNewChat() {
    setSessionId(undefined);
    setMessages([]);
    setPendingAction(null);
    setError("");
    setInput("");
    saveChat(token, undefined, [], null);
  }

  if (!profile) {
    return (
      <main className="login-page">
        <section className="login-story">
          <img className="brand-logo on-dark" src="/ideas2it-logo.svg" alt="ideas2it" />
          <p className="eyebrow">PEOPLEDESK BY IDEAS2IT</p>
          <h1>HR help for Ideators, without the waiting.</h1>
          <p className="lead">Your secure Ideas2IT HR help desk, grounded in company policy and connected to real employee workflows.</p>
          <div className="trust-row"><span>Policy grounded</span><span>Authenticated</span><span>Auditable</span></div>
        </section>
        <section className="login-card">
          <p className="eyebrow dark">WELCOME BACK</p>
          <h2>Sign in to your workspace</h2>
          <p className="muted">Sign in with your PeopleDesk username and password.</p>
          <form onSubmit={handleLogin}>
            <label>Username<input name="username" autoComplete="username" autoFocus value={loginForm.username} onChange={(event) => setLoginForm({ ...loginForm, username: event.target.value })} /></label>
            <label>Password<input type="password" name="password" autoComplete="current-password" value={loginForm.password} onChange={(event) => setLoginForm({ ...loginForm, password: event.target.value })} /></label>
            {error && <p className="error">{error}</p>}
            <button className="primary" disabled={loading || !loginForm.username.trim() || !loginForm.password}>{loading ? "Signing in…" : "Sign in"}</button>
          </form>
          <p className="security-note">Secured sign-in · role-based access</p>
        </section>
      </main>
    );
  }

  async function submitPasswordChange(event: FormEvent) {
    event.preventDefault();
    if (!token) return;
    setPasswordError("");
    if (passwordForm.next !== passwordForm.confirm) {
      setPasswordError("The new passwords do not match.");
      return;
    }
    setLoading(true);
    try {
      const updated = await changePassword(token, passwordForm.current, passwordForm.next);
      setPasswordForm({ current: "", next: "", confirm: "" });
      setProfile(updated);
    } catch (nextError) {
      setPasswordError(nextError instanceof Error ? nextError.message : "The password could not be changed");
    } finally {
      setLoading(false);
    }
  }

  if (profile.must_change_password) {
    return (
      <main className="login-page">
        <section className="login-story">
          <img className="brand-logo on-dark" src="/ideas2it-logo.svg" alt="ideas2it" />
          <p className="eyebrow">PEOPLEDESK BY IDEAS2IT</p>
          <h1>Welcome, {profile.name.split(" ")[0]}.</h1>
          <p className="lead">Your account was just activated with a one-time password. Choose your own password to continue.</p>
        </section>
        <section className="login-card">
          <p className="eyebrow dark">FIRST SIGN-IN</p>
          <h2>Change your password</h2>
          <p className="muted">At least 10 characters, with upper- and lower-case letters and a number.</p>
          <form onSubmit={submitPasswordChange}>
            <input type="text" name="username" autoComplete="username" value={profile.employee_code} readOnly hidden />
            <label>Temporary password<input type="password" autoComplete="current-password" value={passwordForm.current} onChange={(event) => setPasswordForm({ ...passwordForm, current: event.target.value })} /></label>
            <label>New password<input type="password" autoComplete="new-password" value={passwordForm.next} onChange={(event) => setPasswordForm({ ...passwordForm, next: event.target.value })} /></label>
            <label>Confirm new password<input type="password" autoComplete="new-password" value={passwordForm.confirm} onChange={(event) => setPasswordForm({ ...passwordForm, confirm: event.target.value })} /></label>
            {passwordError && <p className="error">{passwordError}</p>}
            <button className="primary" disabled={loading || !passwordForm.current || !passwordForm.next}>{loading ? "Saving…" : "Save and continue"}</button>
          </form>
          <button type="button" className="link-button" onClick={logout}>Sign out</button>
        </section>
      </main>
    );
  }

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand"><img className="brand-logo small on-dark" src="/ideas2it-logo.svg" alt="ideas2it" /><div><strong>PeopleDesk</strong><span>by Ideas2IT</span></div></div>
        <div className="profile-card"><div className="avatar">{profile.name.split(" ").map((part) => part[0]).slice(0, 2).join("")}</div><div><strong>{profile.name}</strong><span>{profile.employee_code} · {profile.role.replaceAll("_", " ")}</span></div></div>
        <nav><button className="new-chat" onClick={startNewChat} disabled={loading}><span>＋</span> New chat</button><button className="nav-active"><span>✦</span> Assistant</button>{canManageOnboarding && <button onClick={() => setSidePanel("onboarding")}><span>◇</span> Onboarding</button>}{canManageParking && <button onClick={() => { setSidePanel("parking"); void refreshParkingReservations(); }}><span>▦</span> Parking</button>}</nav>
        <div className="side-note"><span className="live-dot" />Connected to HR services</div>
        <button className="logout" onClick={logout}>Sign out</button>
      </aside>

      <section className="workspace">
        <header><div><p className="eyebrow dark">PEOPLEDESK · IDEAS2IT HR</p><h2>How can PeopleDesk help today?</h2></div><div className="role-pill">{profile.role === "EMPLOYEE" ? "Employee self-service" : profile.role === "HR_ADMIN" ? "HR administration" : profile.role === "PARKING_ADMIN" ? "Parking administration" : "Manager & HR workspace"}</div></header>
        <div className={`content-grid${showSidePanel ? "" : " chat-only"}`}>
          <section className="chat-panel">
            <div className="messages">
              {messages.length === 0 && <div className="welcome"><img className="brand-logo welcome-logo" src="/ideas2it-logo.svg" alt="" /><h3>Hello, {profile.name.split(" ")[0]}</h3><p>Ask PeopleDesk about HR policies, balances, leave requests, or workplace parking.{profile.role === "HR_ADMIN" ? " You can also review onboarding requests and activate employee accounts." : canManageParking ? " You can monitor parking arrivals and confirm admin actions before anything changes." : canCreateOnboarding ? " You can also onboard and track new Ideators." : ""} I’ll show sources and confirm before changing anything.</p><div className="suggestions">{suggestions.map((prompt) => <button key={prompt} onClick={() => void submitMessage(prompt)}>{prompt}<span>→</span></button>)}</div></div>}
              {messages.map((message) => {
                const lastAssistantId = [...messages].reverse().find((item) => item.role === "assistant")?.id;
                const renderInlineOnboardingForm = canCreateOnboarding && shouldRenderOnboardingForm(message);
                const renderInlineVehicleForm = profile.role === "EMPLOYEE" && shouldRenderVehicleForm(message);
                return <article key={message.id} className={`message ${message.role}${message.intent?.includes("onboarding") ? " onboarding-message" : ""}${renderInlineOnboardingForm || renderInlineVehicleForm ? " with-form" : ""}`}><div className="message-label">{message.role === "assistant" ? "PeopleDesk" : "You"}{message.intent && <span>{message.intent.replaceAll("_", " ")}</span>}</div>{message.kind === "denied" ? <div className="denied-card" role="alert"><strong>Not allowed for your role</strong><span>{message.text}</span><small>Checked from your sign-in before any AI step ran; nothing was changed.</small></div> : message.role === "assistant" ? <RichText text={message.text} /> : <p>{message.text}</p>}{pendingAction && message.role === "assistant" && message.id === lastAssistantId && <div className="inline-confirm"><button onClick={() => void submitMessage("cancel")} disabled={loading}>Cancel</button><button className="confirm" onClick={() => void submitMessage("yes")} disabled={loading}>Confirm</button></div>}{message.agentActivity && message.agentActivity.length > 0 && <details className="agent-activity" open><summary>Agent activity <span>{message.agentActivity.length}</span></summary><ul>{message.agentActivity.map((activity, index) => <li key={`${activity.tool}-${index}`} className={activity.status}><span aria-hidden="true">{activity.status === "success" ? "✓" : "!"}</span>{activity.label}</li>)}</ul></details>}{renderInlineOnboardingForm && renderOnboardingForm()}{renderInlineVehicleForm && renderVehicleForm()}{message.sources && message.sources.length > 0 && <div className="sources"><strong>Based on</strong>{groupedSourceLabels(message.sources).map((label) => <span key={label}>{label}</span>)}</div>}</article>;
              })}
              {loading && (liveSteps.length > 0
                ? <article className="message assistant live-activity" aria-live="polite"><div className="message-label">PeopleDesk<span>working</span></div><ul>{liveSteps.map((step) => <li key={step.id} className={step.status}><span aria-hidden="true">{step.status === "running" ? "" : step.status === "success" ? "✓" : "!"}</span>{step.label}{step.status === "running" ? "…" : ""}</li>)}</ul></article>
                : <article className="message assistant typing"><span /><span /><span /></article>)}
              <div ref={messageEnd} />
            </div>
            {pendingAction && <div className="pending-banner"><div><strong>Confirmation required</strong><span>{pendingAction}</span></div><div><button onClick={() => void submitMessage("cancel")}>Cancel</button><button className="confirm" onClick={() => void submitMessage("yes")}>Confirm</button></div></div>}
            {error && <p className="error chat-error">{error}</p>}
            <form className="composer" onSubmit={(event) => { event.preventDefault(); void submitMessage(); }}><textarea rows={1} value={input} onChange={(event) => setInput(event.target.value)} placeholder="Ask PeopleDesk…" onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void submitMessage(); } }} /><button disabled={!input.trim() || loading} aria-label="Send">↑</button></form>
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
                {canCreateOnboarding && <section className="onboarding-start"><div className="onboarding-icon">+</div><div><strong>Onboard an Ideator</strong><p>Start the guided onboarding conversation. PeopleDesk will collect details in chat, show the form there, and ask for confirmation before creating the request.</p></div><button onClick={startOnboarding} disabled={loading}>Start onboarding in chat</button></section>}
                {profile.role === "HR_ADMIN" && <section className="approval-list">{onboardingApprovals.length === 0 ? <div className="onboarding-empty"><span>✓</span><p>No onboarding requests are waiting for approval.</p></div> : onboardingApprovals.map((item) => <article key={item.id}><div><strong>#{item.id} · {item.candidate.name}</strong><span>{item.candidate.designation}</span><small>Joins {item.candidate.joining_date}</small></div><div><button className="reject" onClick={() => setInput(`Reject onboarding request #${item.id} because `)}>Reject</button><button className="approve" onClick={() => void submitMessage(`Approve onboarding request #${item.id}`)}>Approve</button></div></article>)}</section>}
                <form className="onboarding-search" onSubmit={(event) => { event.preventDefault(); void lookupOnboarding(); }}><label htmlFor="onboarding-query">Track onboarding</label><div><input id="onboarding-query" value={onboardingQuery} onChange={(event) => setOnboardingQuery(event.target.value)} placeholder="Name, email, or request ID" /><button disabled={!onboardingQuery.trim() || onboardingLoading}>{onboardingLoading ? "…" : "→"}</button></div></form>
                {onboardingError && <p className="panel-error">{onboardingError}</p>}
                {!onboardingStatus && !onboardingError && <div className="onboarding-empty"><span>◇</span><p>Search for an Ideator to view onboarding progress and provisioning tasks.</p></div>}
                {onboardingStatus && <section className="onboarding-status-card"><div className="onboarding-person"><div className="avatar">{onboardingStatus.candidate.name.split(" ").map((part) => part[0]).slice(0, 2).join("")}</div><div><strong>{onboardingStatus.candidate.name}</strong><span>{onboardingStatus.candidate.designation}</span></div><span className={`status ${onboardingStatus.status.toLowerCase()}`}>{onboardingStatus.status.replaceAll("_", " ")}</span></div><div className="onboarding-meta"><span>Joining<strong>{onboardingStatus.candidate.joining_date}</strong></span><span>Location<strong>{onboardingStatus.candidate.location}</strong></span><span>Manager<strong>{onboardingStatus.candidate.reporting_manager}</strong></span></div><div className="progress-heading"><span>Provisioning progress</span><strong>{onboardingStatus.completed_tasks}/{onboardingStatus.total_tasks}</strong></div><div className="progress-track"><span style={{ width: `${onboardingStatus.total_tasks ? (onboardingStatus.completed_tasks / onboardingStatus.total_tasks) * 100 : 0}%` }} /></div><div className="onboarding-tasks">{onboardingStatus.tasks.map((task) => <article key={task.id}><span className={`task-check ${task.status.toLowerCase()}`}>{task.status === "COMPLETED" ? "✓" : "·"}</span><div><strong>{task.title}</strong><small>{task.status.replaceAll("_", " ")}</small></div></article>)}</div></section>}
              </div>
            </aside>
          ) : null}
        </div>
      </section>
    </main>
  );
}

export default App;
