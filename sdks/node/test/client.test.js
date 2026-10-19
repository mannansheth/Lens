import test from "node:test";
import assert from "node:assert/strict";
import { LensClient, LensClientError } from "../src/index.js";

test("sends ask with bearer key and client mode", async () => {
  let received;
  const client = new LensClient({ apiKey: "secret", fetch: async (url, init) => {
    received = { url, init };
    return Response.json({ output: { answer: "Go to settings" }, citations: [] });
  } });
  const result = await client.ask("How do I update my profile?");
  assert.equal(result.output.answer, "Go to settings");
  assert.equal(received.url, "http://127.0.0.1:8765/v1/ask");
  assert.equal(received.init.headers.Authorization, "Bearer secret");
  assert.deepEqual(JSON.parse(received.init.body), { question: "How do I update my profile?", mode: "client" });
});

test("SQL client returns the SQL response without executing it", async () => {
  let body;
  const client = new LensClient({ fetch: async (_url, init) => {
    body = JSON.parse(init.body);
    return Response.json({ sql: "SELECT id FROM users", dialect: "mysql", execution: "not_performed", validated_read_only: true });
  } });
  const result = await client.sql("List users", { configFile: "appsettings.json", userId: 17, recordScope: "user_records_only" });
  assert.deepEqual(body, { question: "List users", config_file: "appsettings.json", user_id: 17, record_scope: "user_records_only" });
  assert.equal(result.execution, "not_performed");
});

test("surfaces structured HTTP errors", async () => {
  const client = new LensClient({ fetch: async () => Response.json({ error: "unauthorized" }, { status: 401 }) });
  await assert.rejects(client.health(), error => error instanceof LensClientError && error.status === 401 && error.body.error === "unauthorized");
});
