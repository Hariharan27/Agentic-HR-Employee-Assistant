import type { Vehicle, ChatResponse, LiveStep, LeaveRequest, OnboardingFormPayload, OnboardingStatus, ParkingReservation, Profile, ReportingManager } from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

type ApiError = { error?: { message?: string; code?: string }; detail?: string };

async function request<T>(path: string, options: RequestInit = {}, token?: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options.headers,
    },
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as ApiError;
    throw new HttpError(payload.error?.message || payload.detail || `Request failed (${response.status})`, response.status, payload.error?.code);
  }
  return response.json() as Promise<T>;
}

/** An API error that keeps the HTTP status and error code (e.g. 403 forbidden). */
export class HttpError extends Error {
  constructor(message: string, public status: number, public code?: string) {
    super(message);
  }
}

export const getMyVehicles = (token: string) => request<Vehicle[]>("/api/v1/parking/me/vehicles", {}, token);

export async function login(username: string, password: string) {
  return request<{ access_token: string; role: string; must_change_password?: boolean }>("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

export const getProfile = (token: string) => request<Profile>("/api/v1/auth/me", {}, token);

export const changePassword = (token: string, currentPassword: string, newPassword: string) =>
  request<Profile>(
    "/api/v1/auth/change-password",
    { method: "POST", body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }) },
    token,
  );

export const sendChat = (token: string, message: string, sessionId?: string, onboardingForm?: OnboardingFormPayload) =>
  request<ChatResponse>(
    "/api/v1/chat",
    {
      method: "POST",
      body: JSON.stringify({ message, session_id: sessionId || null, onboarding_form: onboardingForm || null }),
    },
    token,
  );

class StreamUnavailable extends Error {}

/**
 * Chat over Server-Sent Events: calls onStep for every live agent step and resolves with the same
 * ChatResponse as POST /chat. Falls back to POST /chat when streaming is not available.
 */
export async function sendChatStream(
  token: string,
  message: string,
  onStep: (step: LiveStep) => void,
  sessionId?: string,
  onboardingForm?: OnboardingFormPayload,
): Promise<ChatResponse> {
  try {
    return await readChatStream(token, message, onStep, sessionId, onboardingForm);
  } catch (error) {
    if (error instanceof StreamUnavailable) return sendChat(token, message, sessionId, onboardingForm);
    throw error;
  }
}

async function readChatStream(
  token: string,
  message: string,
  onStep: (step: LiveStep) => void,
  sessionId?: string,
  onboardingForm?: OnboardingFormPayload,
): Promise<ChatResponse> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/v1/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ message, session_id: sessionId || null, onboarding_form: onboardingForm || null }),
    });
  } catch {
    throw new StreamUnavailable("Streaming request failed");
  }
  if (response.status === 404 || response.status === 405) throw new StreamUnavailable("No streaming endpoint");
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as ApiError;
    throw new HttpError(payload.error?.message || payload.detail || `Request failed (${response.status})`, response.status, payload.error?.code);
  }
  if (!response.body) throw new StreamUnavailable("Streaming not supported");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
      let event = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (!data) continue;
      const payload = JSON.parse(data);
      if (event === "step") onStep(payload as LiveStep);
      else if (event === "final") return payload as ChatResponse;
      else if (event === "error") throw new HttpError(payload.message || "The assistant could not respond", payload.status || 500);
    }
    if (done) break;
  }
  throw new Error("The assistant stopped before replying. Please try again.");
}

export const getLeaveRequests = (token: string, role: Profile["role"]) =>
  request<LeaveRequest[]>(
    role === "EMPLOYEE" ? "/api/v1/leave/requests" : "/api/v1/manager/leave-requests",
    {},
    token,
  );

export const getParkingAdminReservations = (token: string, reservationDate: string) =>
  request<ParkingReservation[]>(
    `/api/v1/parking-admin/reservations?reservation_date=${encodeURIComponent(reservationDate)}`,
    {},
    token,
  );

export const getOnboardingStatus = (token: string, query: string) => {
  const normalized = query.trim();
  const path = /^\d+$/.test(normalized)
    ? `/api/v1/manager/onboarding/${normalized}`
    : `/api/v1/manager/onboarding/status/by-employee?employee=${encodeURIComponent(normalized)}`;
  return request<OnboardingStatus>(path, {}, token);
};

export const getReportingManagers = (token: string) =>
  request<ReportingManager[]>("/api/v1/manager/onboarding/reporting-managers", {}, token);

export const getPendingOnboardingApprovals = (token: string) =>
  request<OnboardingStatus[]>("/api/v1/hr-admin/onboarding/pending", {}, token);
