import type { ChatResponse, LeaveRequest, Profile } from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

type ApiError = { error?: { message?: string }; detail?: string };

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
    throw new Error(payload.error?.message || payload.detail || `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export async function login(username: string, password: string) {
  return request<{ access_token: string; role: string }>("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

export const getProfile = (token: string) => request<Profile>("/api/v1/auth/me", {}, token);

export const sendChat = (token: string, message: string, sessionId?: string) =>
  request<ChatResponse>(
    "/api/v1/chat",
    { method: "POST", body: JSON.stringify({ message, session_id: sessionId || null }) },
    token,
  );

export const getLeaveRequests = (token: string, role: Profile["role"]) =>
  request<LeaveRequest[]>(
    role === "EMPLOYEE" ? "/api/v1/leave/requests" : "/api/v1/manager/leave-requests",
    {},
    token,
  );
