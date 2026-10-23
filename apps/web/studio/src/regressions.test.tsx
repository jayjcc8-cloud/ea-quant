import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import App from "./App";
import { adapter } from "./scenario";
import { decodeBuffer, encodeBuffer, money } from "./context";

beforeEach(() => {
  sessionStorage.clear();
  adapter.reset("baseline");
  vi.stubGlobal("matchMedia", () => ({
    matches: false,
    addEventListener() {},
    removeEventListener() {},
  }));
  HTMLElement.prototype.scrollTo = () => {};
  HTMLElement.prototype.scrollIntoView = () => {};
  HTMLDialogElement.prototype.showModal = function () {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function () {
    this.open = false;
  };
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("审查发现的输入恢复回归", () => {
  it("空数字持久化和重载后仍是可修复输入", async () => {
    expect(Number.isNaN(decodeBuffer(encodeBuffer({ cash: NaN })).cash)).toBe(
      true,
    );
    expect(money(null as unknown as number)).toBe("—");
    location.hash = "/simulation/new?candidate=C-006";
    const view = render(<App />);
    const cash = await screen.findByLabelText("初始模拟资金（USD）");
    fireEvent.change(cash, { target: { value: "" } });
    view.unmount();
    render(<App />);
    const restored = (await screen.findByLabelText(
      "初始模拟资金（USD）",
    )) as HTMLInputElement;
    expect(restored.value).toBe("");
    fireEvent.change(restored, { target: { value: "120000" } });
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
    await waitFor(() =>
      expect(adapter.getSnapshot().simulations.at(-1)?.config.cash).toBe(
        120000,
      ),
    );
  });
  it("同一上下文重新绑定保留未保存参数", async () => {
    location.hash = "/research/R-001/draft";
    render(<App />);
    fireEvent.change(await screen.findByLabelText("快均线（周期）"), {
      target: { value: "25" },
    });
    fireEvent.click(screen.getByRole("button", { name: "修改绑定上下文" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "绑定并保存草稿" }),
    );
    await waitFor(() =>
      expect(adapter.getSnapshot().researches[0].draft).toMatchObject({
        revision: 5,
        fast: 25,
      }),
    );
    expect(adapter.getSnapshot().reports[0].input.fast).toBe(20);
  });
  it("测试完成不覆盖测试期间的新模型输入", async () => {
    location.hash = "/resources/models";
    render(<App />);
    const model = (await screen.findByLabelText(
      "默认模型",
    )) as HTMLInputElement;
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    fireEvent.change(model, { target: { value: "newer-model" } });
    await screen.findByText(
      "测试期间配置已更改；新输入已保留，请重新测试当前配置。",
    );
    expect(model.value).toBe("newer-model");
    expect(adapter.getSnapshot().connections[0].model).toBe("research-model");
  });
});
