# QUINE 前端 v3

v3 目前有两个用途不同的入口，均处于开发预览阶段：

| 入口 | 数据与能力 | 启动方式 |
| --- | --- | --- |
| 原生真实研究：`apps/web/src/studio-real.tsx`，桌面 `/studio` | 现有研究、CSV、草稿、实验预览/显式确认、冻结报告和工作区选择；使用 Python 服务与 Rust 实验 owner | [从源码构建 Mac 开发候选](../desktop-macos.md#source-build-prerequisites) |
| 离线设计预览：`apps/web/studio/` | 依据39幅效果图实现的可复位合成场景；不调用真实后端、模型、交易所或账户 | 下方 `dev:studio` 命令 |

原生入口的模型连接页复用桌面设置，可创建、编辑、取消、保存及设定工作区默认；助手可选择本研究连接或跟随默认，保存配置不调用模型。模拟、实盘、批量、留出和候选晋级尚未接入。源码整合不等于用户亲自验收或安装包发行。
真实研究使用普通 `npm --prefix apps/web run build` 构建；`build:studio` 只构建设计预览。

## 离线设计预览

使用与 CI 一致的 Node.js 24/npm，从当前 `main` 或待测试 PR 的仓库根目录启动：

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

设计预览构建输出 `.local/frontend-v3/dist/`；缓存和运行证据在同一忽略区。无需启动 Python、数据库或桌面实例。`vite.studio.config.ts` 无后端代理配置，预览源码不引用旧 `apps/web/src`；原生真实研究复用了部分设计组件。

## 使用与恢复

- 正常入口：研究、回测、模拟、实盘四主区；侧栏资源、候选、需要处理、设置。
- 复位入口：`http://127.0.0.1:4176/#/dev`。选择场景，确认复位。它只替换本浏览器的 v3 演示状态，取消旧场景尚未提交的动作，并清除 v3 编辑缓冲。
- 场景：完整基线、首次空态、读取失败一次、提案冲突、实验未知、观测陈旧、停止未知、模型测试失败一次。
- 保存对象在 `localStorage[quine.frontend-v3.demo.v1]`；编辑缓冲在同前缀的 sessionStorage。刷新保留两者；关闭标签后的编辑缓冲遵循浏览器 session 生命周期。清除浏览器站点数据后回到内置基线。
- 同一浏览器标签是当前演示数据的写入边界。它不是跨窗口协同或真实工作区存储。
- 读取失败点击就地重试；未知请求查询原 ID。配置保存、候选接受、提案应用都不会自动启动运行。
- `⌘K` / `Ctrl+K` 搜索，方向键选择，Enter导航，Esc关闭。窄窗 Agent 以抽屉打开，编辑缓冲保留。

[离线预览实施方案](plan.md) · [39图历史覆盖与验收](coverage.md) · [设计期后端契约](contract.md)

## 验收边界

上述39图证据验证的是浏览器交互与合成数据一致性，不能用来证明原生真实研究已验收。
设计预览的中文组合输入通过 Chromium 输入协议验证 Enter 不误提交；这不等于 macOS 系统拼音候选窗或原生 WebView 验收。
原生开发候选已经接入本地研究引擎；用户已授权源码合并，但未据此记录用户亲自验收 PASS，真实 provider 验收亦未完成。
签名、公证、干净 Mac 安装和 Paper 长时间运行分别约束安装包或运行声明，不是源码贡献的前置。
当前提交与 CI 范围见 [STATUS](../STATUS.md)，提交改进见 [CONTRIBUTING](../../CONTRIBUTING.md)。
