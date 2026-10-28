// Node 25 exposes a native localStorage even in jsdom tests. Use the browser
// environment's real Storage implementation, with its origin and isolation.
Object.defineProperty(globalThis, "localStorage", {
  configurable: true,
  value: (globalThis as unknown as { jsdom: { window: Window } }).jsdom.window
    .localStorage,
});
