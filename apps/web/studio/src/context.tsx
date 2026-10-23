import { createContext, useContext, useEffect, useRef, useState } from "react";
import { adapter } from "./scenario";
import type { ActionResult, Command, DemoState } from "./model";

export interface LeaveGuard {
  save: () => Promise<boolean>;
  discard: () => void;
}
export const StudioContext = createContext<{
  state: DemoState;
  route: string;
  navigate: (path: string, afterSave?: boolean) => void;
  setGuard: (guard: LeaveGuard | null) => void;
  openAgent: () => void;
  toast: (message: string) => void;
} | null>(null);
export function useStudio() {
  const value = useContext(StudioContext);
  if (!value) throw new Error("Missing StudioContext");
  return value;
}
export function useAction() {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    lock = useRef(false);
  const execute = async (
    command: Command,
    done?: (result: ActionResult) => void,
  ) => {
    if (lock.current) return null;
    lock.current = true;
    setBusy(true);
    setError("");
    try {
      const result = await adapter.execute(command);
      done?.(result);
      return result;
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败，请重试。");
      return null;
    } finally {
      lock.current = false;
      setBusy(false);
    }
  };
  return { busy, error, execute, clearError: () => setError(""), setError };
}
export function useUnsaved(
  dirty: boolean,
  save: () => Promise<boolean>,
  discard: () => void,
) {
  const { setGuard } = useStudio();
  useEffect(() => {
    setGuard(dirty ? { save, discard } : null);
    return () => setGuard(null);
  }, [dirty, save, discard, setGuard]);
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (dirty) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);
}
export const encodeBuffer = (value: unknown) =>
  JSON.stringify(value, (_key, v) =>
    typeof v === "number" && !Number.isFinite(v)
      ? { __quineNumber: "invalid" }
      : v,
  );
export const decodeBuffer = (text: string) =>
  JSON.parse(text, (_key, v) => (v && v.__quineNumber === "invalid" ? NaN : v));
export function useBuffer<T>(key: string, initial: T) {
  const [value, setValue] = useState<T>(() => {
    try {
      const stored = sessionStorage.getItem(`quine.frontend-v3.buffer.${key}`);
      return stored ? decodeBuffer(stored) : initial;
    } catch {
      return initial;
    }
  });
  const [bufferError, setBufferError] = useState(""),
    current = useRef(value);
  function update(next: T) {
    current.current = next;
    setValue(next);
    try {
      sessionStorage.setItem(
        `quine.frontend-v3.buffer.${key}`,
        encodeBuffer(next),
      );
      setBufferError("");
    } catch {
      setBufferError("编辑缓冲未能保存；关闭或刷新前请保存当前修改。");
    }
  }
  function clear() {
    try {
      sessionStorage.removeItem(`quine.frontend-v3.buffer.${key}`);
    } catch {
      /* The value remains in this open view. */
    }
  }
  return {
    value,
    update,
    clear,
    bufferError,
    getCurrent: () => current.current,
  };
}
export const requestKey = () => crypto.randomUUID();
export const percent = (v: number) => `${v.toFixed(1)}%`;
export const money = (v: number) =>
  typeof v !== "number" || !Number.isFinite(v)
    ? "—"
    : v.toLocaleString("en-US", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      });
export function download(name: string, content: unknown, csv = false) {
  const url = URL.createObjectURL(
    new Blob([csv ? String(content) : JSON.stringify(content, null, 2)], {
      type: csv ? "text/csv;charset=utf-8" : "application/json",
    }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
