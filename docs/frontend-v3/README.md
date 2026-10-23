# QUINE 前端 v3

这是一套依据39幅最终效果图独立实现的前端，入口为 `apps/web/studio/`。它使用可复位的离线场景数据，无真实后端、模型、交易所或账户调用。已有应用与服务保持原入口。

从仓库根目录启动：

```sh
npm --prefix apps/web ci
npm --prefix apps/web run dev:studio
```

已有依赖时只需第二条。打开 http://127.0.0.1:4176/ 。独立端口被占用时会明确退出，不替换其他服务。

```sh
npm --prefix apps/web run lint:studio
npm --prefix apps/web run typecheck:studio
npm --prefix apps/web run test:studio
npm --prefix apps/web run build:studio
```

构建输出 `.local/frontend-v3/dist/`；缓存和运行证据在同一忽略区。无需启动 Python、数据库或桌面实例。`vite.studio.config.ts` 无后端代理配置，源码不引用旧 `apps/web/src`。

## 使用与恢复

- 正常入口：研究、回测、模拟、实盘四主区；侧栏资源、候选、需要处理、设置。
- 复位入口：`http://127.0.0.1:4176/#/dev`。选择场景，确认复位。它只替换本浏览器的 v3 演示状态，取消旧场景尚未提交的动作，并清除 v3 编辑缓冲。
- 场景：完整基线、首次空态、读取失败一次、提案冲突、实验未知、观测陈旧、停止未知、模型测试失败一次。
- 保存对象在 `localStorage[quine.frontend-v3.demo.v1]`；编辑缓冲在同前缀的 sessionStorage。刷新保留两者；关闭标签后的编辑缓冲遵循浏览器 session 生命周期。清除浏览器站点数据后回到内置基线。
- 同一浏览器标签是当前演示数据的写入边界。它不是跨窗口协同或真实工作区存储。
- 读取失败点击就地重试；未知请求查询原 ID。配置保存、候选接受、提案应用都不会自动启动运行。
- `⌘K` / `Ctrl+K` 搜索，方向键选择，Enter导航，Esc关闭。窄窗 Agent 以抽屉打开，编辑缓冲保留。

[实施方案](plan.md) · [39图覆盖与验收](coverage.md) · [未来后端契约](contract.md)

## 验收边界

已验证新前端的浏览器交互与离线数据一致性；未替换原生桌面入口、未构建签名包、未部署、未进行真实研究或交易。中文组合输入通过真实 Chromium 输入协议验证 Enter 不误提交；不把它表述为 macOS 系统拼音候选窗或原生 WebView 已验收。
