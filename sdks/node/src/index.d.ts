export type LensMode = "client" | "developer" | "user";

export interface LensClientOptions {
  baseUrl?: string;
  apiKey?: string;
  timeoutMs?: number;
  fetch?: typeof fetch;
}

export interface LensAnswer {
  output: Record<string, unknown>;
  citations: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

export interface LensSqlResult {
  sql: string;
  dialect: string;
  execution: "not_performed";
  validated_read_only: true;
}

export declare class LensClientError extends Error {
  status?: number;
  body?: unknown;
}

export declare class LensClient {
  constructor(options?: LensClientOptions);
  health(): Promise<{ status: string; project_id: string }>;
  ask(question: string, options?: { mode?: LensMode }): Promise<LensAnswer>;
  sql(question: string, options?: { variable?: string; configFile?: string; envFile?: string; userId?: number; recordScope?: "user_records_only" | "all_records" }): Promise<LensSqlResult>;
  index(options?: { refresh?: boolean; force?: boolean }): Promise<Record<string, unknown>>;
}
