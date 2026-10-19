# `@lens-rag/client`

Dependency-free Node.js client for a locally hosted Lens HTTP service. Requires Node.js 18+ (built-in `fetch`). The package wraps the language-neutral API; it does not contain the Lens engine, index, embeddings, or model.

## Install from a Lens checkout

```sh
npm install ./sdks/node
```

For an app in a separate directory, install from the local SDK directory path or create a tarball with `npm pack ./sdks/node` and install that tarball. The package is not published to npm yet.

## Use from a Node backend

```js
import { LensClient } from "@lens-rag/client";

const lens = new LensClient({
  baseUrl: process.env.LENS_BASE_URL ?? "http://127.0.0.1:8765",
  apiKey: process.env.LENS_API_KEY,
});

const answer = await lens.ask("How can I update my profile?", { mode: "client" });
// Return only the fields your authenticated application user is allowed to see.
res.json(answer);

const generated = await lens.sql("Count active accounts", {
  userId: authenticatedUser.id,
  recordScope: "user_records_only",
});
// generated.sql is returned only. Lens does not execute it; enforce app policy yourself.
```

Keep this client on the server. Never expose `LENS_API_KEY` in React/browser bundles. Authenticate the user and enforce authorization in the host application's backend before calling Lens. In client mode, treat abstention as a valid answer. SQL generation returns validated SQL text and dialect only; the SDK does not execute it.

## API

- `health()` — readiness status.
- `ask(question, { mode })` — structured answer, citations, and navigation evidence.
- `sql(question, { variable, configFile, envFile, userId, recordScope })` — validated SQL text; never executes. Identity and scope are optional generation context from your authenticated backend, not authorization.
- `index({ refresh, force })` — reindex the repository configured on Lens. Restrict this to trusted deployment code.

Requests default to a 660-second timeout to account for local model startup and generation. Pass `timeoutMs` to change it. Non-2xx responses and network/protocol errors throw `LensClientError`, which exposes `status` and parsed `body` for HTTP failures.

See the [deployment guide](../../docs/release/deployment.md) and [OpenAPI definition](../../docs/release/openapi.yaml) for engine setup, networking, API security, and response details.

## Development

```sh
npm test
npm run build
npm pack --dry-run
```
