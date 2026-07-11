# EA Quant Trading System

EA 是一个长期迭代的量化交易系统工程。第一阶段不追求“马上实盘赚钱”，而是先建立可复现、可审计、可扩展、可风控的最小内核。

## 项目原则

- 自上而下设计：先明确领域模型、模块边界、数据与交易语义，再实现策略。
- 借鉴成熟框架，不被框架锁死：参考 LEAN、NautilusTrader、vn.py、Freqtrade、vectorbt、Backtrader、Zipline Reloaded 的成熟模式，但以本项目实际验证结果为准。
- 研究、回测、纸交易、实盘尽量共享同一套领域对象和事件语义。
- 策略不直接调用交易所或券商 SDK；所有订单必须经过组合构建、风控、执行适配层。
- Git 与 GitHub 是硬要求；任何关键架构选择都通过 ADR 记录。
- 密钥、账号、行情源 token、broker 凭证永远不进 Git。

## 当前状态

- 本地 Git 仓库已初始化，当前分支为 `main`。
- GitHub CLI 已登录账号 `jayjcc8-cloud`。
- 本机 SSH 公钥已添加到 GitHub，SSH 认证已通过。
- 当前仓库尚未连接 GitHub remote，目标新仓库为 `jayjcc8-cloud/ea-quant`。
- 本仓库还未配置 `user.name` / `user.email`，首次 commit 前将设置 repo-local Git 身份。

## 顶层模块

```text
src/ea/
  core/          # 领域模型、事件、时间、资产标识、错误类型
  config/        # 配置加载、校验、环境变量、密钥引用
  cli/           # ea doctor/data/backtest/paper/live/report
  data/          # 数据接入、清洗、存储、质量检查
  features/      # 因子、指标、特征工程
  strategy/      # 策略接口、信号、调仓目标
  portfolio/     # 组合构建、资金分配、绩效归因
  backtest/      # 回测引擎、撮合、滑点、手续费、报告
  paper/         # 纸交易模式
  execution/     # OMS、订单路由、状态机、对账
  brokers/       # broker/exchange adapter
  risk/          # 风控规则、熔断、kill switch
  monitoring/    # 日志、指标、告警、审计 trail
  experiments/   # 实验追踪、参数、数据版本、模型版本
```

## 阶段路线

1. Phase 0：工程地基  
   建立 Git/GitHub、README、ADR、CI、Python 项目结构、配置与测试骨架。

2. Phase 1：回测 MVP  
   导入本地 OHLCV 数据，跑通策略接口、事件驱动回测、报告输出和固定样例测试。

3. Phase 2：严肃回测  
   加入手续费、滑点、成交延迟、样本外验证、walk-forward、实验追踪和数据版本。

4. Phase 3：纸交易  
   接入真实行情或模拟行情，复用 OMS、风控、监控，默认禁止绕过风控。

5. Phase 4：小额实盘  
   只接一个 broker/exchange，低频、小资金、强风控上线，必须有 kill switch 和对账。

6. Phase 5：小规模生产化  
   多策略、多环境、监控告警、日报周报、备份恢复、部署手册。

## 参考框架

- [QuantConnect LEAN](https://github.com/QuantConnect/Lean)：参考 Universe / Alpha / Portfolio / Risk / Execution 的模块边界。
- [NautilusTrader](https://github.com/nautechsystems/nautilus_trader)：参考研究、仿真、实盘同一事件语义。
- [vn.py](https://github.com/vnpy/vnpy)：参考 Gateway / App / Event Engine / Risk Manager 和国内市场接入。
- [Freqtrade](https://github.com/freqtrade/freqtrade)：参考 dry-run/live、CLI、配置、Docker、监控与加密货币 bot 运维。
- [vectorbt](https://github.com/polakowo/vectorbt)：用于研究阶段参数扫描，不作为实盘核心。
- [Backtrader](https://github.com/mementum/backtrader) 与 [Zipline Reloaded](https://github.com/stefan-jansen/zipline-reloaded)：参考回测 API 与研究工作流。

## GitHub 接入待确认

默认使用以下信息继续配置本地 Git 身份、创建首次 commit、连接 GitHub remote 并 push：

- GitHub 仓库名称：`ea-quant`
- 仓库归属：`jayjcc8-cloud`
- 可见性：`private`
- Git 身份：`jayjcc8-cloud <275956764+jayjcc8-cloud@users.noreply.github.com>`
- Remote 协议：SSH
