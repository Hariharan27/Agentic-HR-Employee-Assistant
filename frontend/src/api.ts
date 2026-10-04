import type { ChatResponse, LeaveRequest, OnboardingStatus, ParkingReservation, Profile, ReportingManager } from "./types";

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
