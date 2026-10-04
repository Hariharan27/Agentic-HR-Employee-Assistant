export type Profile = {
  user_id: number;
  employee_id: number;
  employee_code: string;
  name: string;
  email: string;
  role: "EMPLOYEE" | "MANAGER" | "HR" | "HR_ADMIN" | "PARKING_ADMIN";
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
  showOnboardingForm?: boolean;
  showVehicleForm?: boolean;
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

export type OnboardingTask = {
  id: number;
  task_type: string;
  title: string;
  status: string;
  created_at?: string | null;
  updated_at?: string | null;
  reviewed_by_user_id?: number | null;
  review_comment?: string | null;
  reviewed_at?: string | null;
  activated_employee_id?: number | null;
  activated_user_id?: number | null;
};

export type OnboardingStatus = {
  id: number;
  candidate: {
    name: string;
    email: string;
    designation: string;
    department: string;
    reporting_manager: string;
    joining_date: string;
    location: string;
    employment_type: string;
  };
  manager_employee_id: number;
  created_by_user_id: number;
  status: string;
  completed_tasks: number;
  total_tasks: number;
  tasks: OnboardingTask[];
  created_at?: string | null;
  updated_at?: string | null;
};

export type ReportingManager = {
  id: number;
  name: string;
  employee_code?: string | null;
  designation?: string | null;
  department?: string | null;
};

export type ParkingReservation = {
  id: number;
  employee_id: number;
  employee_code?: string | null;
  employee_name?: string | null;
  vehicle_registration?: string | null;
  slot_code: string;
  slot_location: string;
  reservation_date: string;
  status: string;
  cancelled_at?: string | null;
  checked_in_at?: string | null;
  completed_at?: string | null;
  no_show_at?: string | null;
};
