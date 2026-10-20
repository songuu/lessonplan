export type User = {
  id: string;
  email: string;
  name: string;
  role: string;
  school_stage?: string;
  grade?: string;
  subject?: string;
  profile?: { school_stage?: string; grade?: string; subject?: string };
};
export type ModuleConfig = {
  module_id?: string;
  module_type: string;
  display_name: string;
  enabled: boolean;
  module_summary: string;
  is_custom: boolean;
  display_order?: number;
};
export type ModuleState = {
  generation_status?: string;
  confirmation_status?: string;
  freshness?: string;
  workflow_stage?: string;
  outline?: OutlineStage[];
  outline_confirmed?: boolean;
  last_error?: unknown;
};
export type LessonModule = ModuleConfig & {
  module_id: string;
  teacher_view_content: string;
  current_version_id: string | null;
  state: ModuleState;
};
export type Draft = {
  id: string;
  topic: string;
  school_stage: string;
  grade: string;
  subject: string;
  lesson_type: string;
  total_duration_min: number;
  requirements?: string;
  generation_mode?: string;
  status: string;
  completion_status?: string;
  content_version: number;
  module_config_version_id: string;
  modules: LessonModule[];
  created_at?: string;
  updated_at?: string;
  active_run_id?: string;
  latest_run_id?: string;
  knowledge_base_ids?: string[];
  knowledge_context_snapshot?: KnowledgeChunk[];
};
export type KnowledgeBase = {
  id: string;
  title: string;
  description: string;
  documents_count: number;
};
export type KnowledgeDocument = {
  id: string;
  base_id: string;
  filename: string;
  status: "uploaded" | "processing" | "ready" | "failed";
  chunk_count: number;
  error_message?: string | null;
  run_id?: string | null;
};
export type KnowledgeChunk = {
  id: string;
  document_id: string;
  base_id: string;
  filename: string;
  base_name: string;
  ordinal: number;
  text: string;
  content_hash: string;
  score?: number;
};
export type OutlineStage = {
  stage_id: string;
  title: string;
  duration_min: number;
  objective?: string;
};
export type Run = {
  id: string;
  status: string;
  workflow_type?: string;
  payload?: Record<string, unknown>;
  result?: Record<string, unknown>;
  error?: { message?: string } | string;
  error_message?: string;
  draft_id?: string;
  created_at?: string;
};
export type StreamEvent = {
  id: string;
  event: string;
  data: Record<string, unknown>;
};
export type Candidate = {
  content?: string;
  teacher_view_content?: string;
  replacement_text?: string;
  selection_start?: number;
  selection_end?: number;
  [key: string]: unknown;
};
export type RecoveryDispatch = {
  id: string;
  status: string;
  attempts: number;
  next_attempt_at: string | null;
  last_attempt_at: string | null;
  last_error_code: string | null;
  deadline_at: string | null;
};
export type RecoveryRun = {
  id: string;
  draft_id: string | null;
  topic: string | null;
  user_id: string;
  workflow_type: string;
  status: string;
  error_code: string | null;
  execution_epoch: number;
  recovery_count: number;
  heartbeat_at: string | null;
  lease_expires_at: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  checkpoint_count: number;
  dispatch: RecoveryDispatch | null;
  can_retry: boolean;
  can_cancel: boolean;
};
export type RecoveryAlert = {
  id: string;
  run_id: string | null;
  code: string;
  severity: string;
  message: string;
  status: "open" | "acknowledged" | "resolved";
  occurrence_count: number;
  first_seen_at: string;
  last_seen_at: string;
  acknowledged_at: string | null;
  acknowledged_by: string | null;
  resolved_at: string | null;
};
export type RecoveryEvent = {
  sequence: number;
  event_type: string;
  created_at: string;
  payload?: {
    phase?: string;
    module_id?: string;
    checkpoint_key?: string;
    attempt?: number;
    max_attempts?: number;
    execution_epoch?: number;
    code?: string;
  };
  phase?: string;
  module_id?: string;
  checkpoint_key?: string;
  attempt?: number;
  max_attempts?: number;
  execution_epoch?: number;
  code?: string;
};
export type RecoveryInvocation = {
  id: string;
  invocation_key: string;
  step_key: string;
  attempt: number;
  call_id: string;
  call_index: number;
  route: string;
  status:
    "in_flight" | "response_saved" | "applied" | "failed" | "outcome_unknown";
  request_hash: string;
  response_hash: string | null;
  capture_epoch: number;
  created_at: string;
  response_saved_at: string | null;
  applied_at: string | null;
  error_code: string | null;
  provider_request_id: string | null;
  model_name: string | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
};
export type RecoveryDetail = {
  run: RecoveryRun;
  invocations?: RecoveryInvocation[];
  checkpoints: {
    id: string;
    key: string;
    created_at: string;
    input_fingerprint: string;
    content_hash: string;
  }[];
  calls: {
    id: string;
    model_name?: string;
    provider?: string;
    status?: string;
    created_at?: string;
    call_id?: string;
    call_outcome?: string;
    validation_outcome?: string;
    validation_codes?: string[];
    execution_epoch?: number;
  }[];
  events: RecoveryEvent[];
  event_total: number;
  alerts: RecoveryAlert[];
  budget?: {
    calls_started: number;
    max_calls: number;
    quality_retry_limit: number;
    attempt_counts: Record<string, number>;
    elapsed_seconds: number;
    run_timeout_seconds: number;
  };
};
export type RecoveryPageResult<T> = {
  items: T[];
  total: number;
  offset: number;
  limit: number;
  server_time?: string;
};
export type RecoveryOverview = {
  queued: number;
  running: number;
  retry_pending: number;
  expired_leases: number;
  open_alerts: number;
  failed: number;
};
export const terminal = (status: string) =>
  ["completed", "failed", "cancelled"].includes(status);

export type ExecutionLog = {
  sequence: number;
  event_id: string;
  run_id: string;
  execution_epoch: number;
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  phase: "start" | "end" | "event";
  name: string;
  run_type: string;
  level: "info" | "warning" | "error";
  status:
    | "running"
    | "completed"
    | "failed"
    | "cancelled"
    | "lease_lost"
    | "interrupted";
  started_at: string;
  ended_at: string | null;
  elapsed_ms: number | null;
  metadata: Record<string, unknown>;
  created_at: string;
  workflow_type: string;
};
export type ObservabilityStatus = {
  provider?: string;
  enabled?: boolean;
  configured?: boolean;
  project?: string;
  local?: Record<string, unknown>;
  [key: string]: unknown;
};
export type ExecutionLogPage = {
  items: ExecutionLog[];
  next_after: number;
  has_more: boolean;
  observability?: ObservabilityStatus;
};
