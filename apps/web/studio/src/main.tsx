import { Component, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./studio.css";

class ErrorBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? (
      <main className="page">
        <h1>页面暂时无法显示</h1>
        <p>演示数据和编辑缓冲已保留。请重新载入，或返回验收工具恢复场景。</p>
        <button className="button primary" onClick={() => location.reload()}>
          重新载入
        </button>
        <button
          className="button"
          onClick={() => {
            location.hash = "/dev";
            location.reload();
          }}
        >
          打开恢复工具
        </button>
      </main>
    ) : (
      this.props.children
    );
  }
}
createRoot(document.getElementById("root")!).render(
  <ErrorBoundary>
    <App />
  </ErrorBoundary>,
);
