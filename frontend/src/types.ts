export type Profile = {
  user_id: number;
  employee_id: number;
  employee_code: string;
  name: string;
  email: string;
  role: "EMPLOYEE" | "MANAGER" | "HR";
};

export type Source = {
  document?: string;
  page?: number;
  section?: string;
};

export type ChatResponse = {
  session_id: string;
  message: string;
  domain: string;
  intent?: string | null;
  sources: Source[];
  pending_action?: string | null;
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  text: string;
  sources?: Source[];
  intent?: string | null;
};

export type LeaveRequest = {
  id: number;
  employee_id: number;
  employee_code?: string | null;
  employee_name?: string | null;
  leave_type: string;
  start_date: string;
  end_date: string;
  working_days: string;
  reason?: string | null;
  status: string;
};
