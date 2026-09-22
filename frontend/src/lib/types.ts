export interface User {
  id: string;
  login: string;
  avatar_url: string;
}

export interface GitHubRepoSummary {
  github_id: number;
  owner: string;
  name: string;
  default_branch: string;
  is_private: boolean;
  description?: string | null;
  updated_at?: string | null;
  [key: string]: unknown;
}

export type RunStatus =
  | "queued"
  | "cloning"
  | "parsing"
  | "resolving"
  | "embedding"
  | "summarizing"
  | "ready"
  | "failed";

export const ACTIVE_RUN_STATUSES: RunStatus[] = [
  "queued",
  "cloning",
  "parsing",
  "resolving",
  "embedding",
  "summarizing",
];

export interface RunStats {
  files?: number;
  symbols?: number;
  edges?: number;
  chunks?: number;
  [key: string]: unknown;
}

export interface RepoOverview {
  summary?: string;
  [key: string]: unknown;
}

export interface LatestRun {
  id: string;
  commit_sha: string;
  stats: RunStats | null;
  overview: RepoOverview | null;
}

export interface Repository {
  id: string;
  slug: string;
  default_branch: string;
  is_private: boolean;
  latest_run: LatestRun | null;
}

export interface IndexRunListItem {
  id: string;
  commit_sha: string;
  status: RunStatus;
  phase_pct: number;
  stats: RunStats | null;
  error: string | null;
  created_at: string;
}

export interface IndexRunDetail {
  id: string;
  repository_id: string;
  commit_sha: string;
  status: RunStatus;
  phase_pct: number;
  stats: RunStats | null;
  overview: RepoOverview | null;
  error: string | null;
}

export interface ProgressFrame {
  run_id?: string;
  status?: RunStatus;
  phase_pct?: number;
  message?: string;
  stats?: RunStats | null;
  error?: string | null;
  type?: string;
}

export interface Citation {
  path: string;
  start: number;
  end: number;
}

export interface AgentStep {
  idx: number;
  tool: string;
  input?: Record<string, unknown>;
  summary?: string;
  ms?: number;
  error?: boolean;
}

export interface ConversationSummary {
  id: string;
  repository_id: string;
  title: string | null;
  created_at: string;
}

export interface MessageRecord {
  id: string;
  role: "user" | "assistant";
  content: string;
  confidence: "high" | "medium" | "low" | null;
  cost_usd: number | null;
  latency_ms: number | null;
  truncated: boolean;
  steps: AgentStep[];
  citations: Citation[];
}

export interface ConversationDetail {
  id: string;
  repository_id: string;
  title: string | null;
  messages: MessageRecord[];
}

export interface FileSlice {
  path: string;
  language: string | null;
  start: number;
  end: number;
  total_lines: number;
  content: string;
}
