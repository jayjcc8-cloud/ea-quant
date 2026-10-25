import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { StudioContext } from "./context";
import { createFixture } from "./fixtures";
import { AgentPanel } from "./ui";
import { adapter } from "./scenario";

beforeEach(() => {
  sessionStorage.clear();
  HTMLElement.prototype.scrollIntoView = vi.fn();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function reportPanel() {
  const state = createFixture();
  // A later working draft must not change the report's frozen interpretation.
  state.researches[0].draft.revision = 5;
  state.researches[0].draft.fast = 30;
  const navigate = vi.fn();
  const element = (
    <StudioContext.Provider
      value={{
        state,
        route: "/report/REP-021/overview",
        navigate,
        setGuard: vi.fn(),
        openAgent: vi.fn(),
        toast: vi.fn(),
      }}
    >
      <AgentPanel objectId="REP-021" context="报告 REP-021 · r4 · 只读证据" />
    </StudioContext.Provider>
  );
  return { element, navigate };
}

it("报告解读使用冻结证据，引用和下一步只导航，不执行研究", () => {
  const execute = vi.spyOn(adapter, "execute");
  const { element, navigate } = reportPanel();
  render(element);

  expect(screen.getByText("7.9%")).toBeTruthy();
  expect(screen.getByText("10.6%")).toBeTruthy();
  expect(screen.getByText(/共 31 笔交易，费用影响 -0.19%/)).toBeTruthy();
  expect(screen.getByText(/离线演示.*未调用模型/)).toBeTruthy();
  expect(HTMLElement.prototype.scrollIntoView).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: "[2] 冻结输入 · r4" }));
  expect(navigate).toHaveBeenLastCalledWith("/report/REP-021/inputs");
  fireEvent.click(screen.getByRole("button", { name: "[3] 交易明细 · 31 笔" }));
  expect(navigate).toHaveBeenLastCalledWith("/report/REP-021/trades");
  fireEvent.click(screen.getByRole("button", { name: "配置留出验证" }));
  expect(navigate).toHaveBeenLastCalledWith("/holdout/new?report=REP-021");
  expect(execute).not.toHaveBeenCalled();
});

it("报告摘要不清空关闭或切页前的 Agent 编辑缓冲", () => {
  const execute = vi.spyOn(adapter, "execute");
  const { element } = reportPanel();
  const view = render(element);
  fireEvent.change(screen.getByLabelText("询问当前对象"), {
    target: { value: "请解释这份报告的成本与限制" },
  });
  view.unmount();
  render(element);
  expect((screen.getByLabelText("询问当前对象") as HTMLTextAreaElement).value).toBe(
    "请解释这份报告的成本与限制",
  );
  expect(execute).not.toHaveBeenCalled();
});
