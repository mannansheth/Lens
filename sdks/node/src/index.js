/** Dependency-free client for the Lens local HTTP API. */

export class LensClientError extends Error {
  constructor(message, { status, body, cause } = {}) {
    super(message, { cause });
    this.name = "LensClientError";
    this.status = status;
    this.body = body;
  }
}

export class LensClient {
  /**
   * @param {{baseUrl?: string, apiKey?: string, timeoutMs?: number, fetch?: typeof fetch}} [options]
   */
  constructor({ baseUrl = "http://127.0.0.1:8765", apiKey, timeoutMs = 660_000, fetch: fetchImpl = globalThis.fetch } = {}) {
    if (!fetchImpl) throw new TypeError("LensClient requires a runtime with fetch support (Node.js 18+)");
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw new TypeError("timeoutMs must be a positive number");
    this.baseUrl = new URL(baseUrl).toString().replace(/\/$/, "");
    this.apiKey = apiKey;
    this.timeoutMs = timeoutMs;
    this.fetch = fetchImpl;
  }

  health() { return this.#request("GET", "/health"); }

  ask(question, { mode = "client" } = {}) {
    return this.#request("POST", "/v1/ask", { question, mode });
  }

  /** Generate validated read-only SQL. The API never executes it. */
  sql(question, { variable, configFile, envFile, userId, recordScope } = {}) {
    const body = { question };
    if (variable !== undefined) body.variable = variable;
    if (configFile !== undefined) body.config_file = configFile;
    if (envFile !== undefined) body.env_file = envFile;
    if (userId !== undefined) {
      if (!Number.isSafeInteger(userId) || userId <= 0) throw new TypeError("userId must be a positive integer");
      body.user_id = userId;
    }
    if (recordScope !== undefined) {
      if (!userId || !["user_records_only", "all_records"].includes(recordScope)) {
        throw new TypeError("recordScope must be user_records_only or all_records and requires userId");
      }
      body.record_scope = recordScope;
    }
    return this.#request("POST", "/v1/sql", body);
  }

  /** Reindex only the repository already configured on the Lens service. */
  index({ refresh = false, force = false } = {}) {
    return this.#request("POST", "/v1/index", { refresh, force });
  }

  async #request(method, path, payload) {
    const headers = { Accept: "application/json" };
    const init = { method, headers, signal: AbortSignal.timeout(this.timeoutMs) };
    if (payload !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(payload);
    }
    if (this.apiKey) headers.Authorization = `Bearer ${this.apiKey}`;

    let response;
    try {
      response = await this.fetch(`${this.baseUrl}${path}`, init);
    } catch (cause) {
      throw new LensClientError(`Could not complete Lens request: ${cause.message}`, { cause });
    }
    const text = await response.text();
    let body;
    try { body = text ? JSON.parse(text) : {}; }
    catch (cause) {
      throw new LensClientError("Lens returned invalid JSON", { status: response.status, cause });
    }
    if (!response.ok) {
      throw new LensClientError(`Lens returned HTTP ${response.status}: ${body.error ?? text.slice(0, 4096)}`, {
        status: response.status,
        body,
      });
    }
    return body;
  }
}
