# EA Quant Web

This is the UI-001 Dark Professional frontend foundation. It is a deterministic, TypeScript-only
mock workspace: it has no API, backend, network adapter, database, WebSocket, trading control, or
recovery command.

Run the supported frontend checks from this directory:

```bash
npm ci
npm run lint
npm run typecheck
npm test
npm run build
```

Use `npm run dev` for local visual review. The Overview mock-state fixtures are available at
`/?state=loading`, `/?state=empty`, and `/?state=error`; normal `/` is the ready state.
