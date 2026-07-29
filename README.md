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

- Git 与 GitHub SSH remote 已建立，主分支为 `main`。
- Phase 0 已发布 `v0.1.0`；项目版本的唯一事实源是
  [`pyproject.toml`](pyproject.toml)，本次状态审查时为 `0.1.1`。
- Phase 1 的入口门禁完成 5/5：Issue #11–#15 均已完成，Issue #15 产出的
  [Accepted ADR 0008](docs/adr/0008-deterministic-execution-and-reconciliation.md) 已冻结
  deterministic execution、matching、ledger authority 和 reconciliation 语义。
- Phase 1 入口门禁的历史状态审查基线为
  `main@f93fb89d8b3cab11f4a1dde8f5e95758aef1e914`；该提交已具备配置、market/time、
  run manifest、audit lineage、composition preparation、CLI、测试、VSCode 配置和 CI
  质量门禁。Issue #26 已建立 86% line / 71% branch coverage floor 并移除未使用的运行依赖；
  Issue #28 已将 manifest 的 model、wire、codec 和 evidence 职责分离，同时保持公开与 wire
  契约不变。当前开发已在该历史基线上继续推进；Accepted ADR 0014 的 trusted fact
  ingress/dispatch、canonical Fill allocation 与 observation-derived Order projection 已实现
  当前切片；Accepted ADR 0015 的严格本地 OHLCV 解码、稳定文件捕获、canonical selection /
  fingerprint 和 bounded no-look-ahead historical source 已实现当前切片。尚未实现 historical
  runtime 的完整 lifecycle/stage coordinator、venue submission、ledger/runtime integration、
  reconciliation correction、portfolio planning、strategy 或完整 backtest；Accepted ADR 0016
  的 virtual clock、incremental historical frontier、run-wide arbitration/dispatch sequence、
  exact acknowledgement/cursor commit 与 deterministic trace 已实现当前切片。
- Phase 1 实现从 dependency-neutral economic values 开始：`ea.core.economics` 提供严格
  `ea-decimal-v1`、exact grid 与唯一 settlement rounding boundary；`ea.core.execution`
  提供 versioned instrument specification set、canonical bytes/digest 和 identity-bound
  settlement。该切片不代表 OMS 或 matcher 已实现。
- `ea.core.outcomes` 提供 Accepted ADR 0008 的完整封闭 outcome registry；
  `ea.core.execution_identity` 提供 run-scoped owner ID、source-scoped fact dedup key、
  canonical bytes/digest 与纯 replay/conflict 分类。`ea.core.execution_messages` 在同一无依赖
  边界上提供 factory-only immutable `OrderIntent -> RiskDecision -> ExecutionApproval ->
  Order -> ExecutionFactIngress/ExecutionFact -> Fill` 消息、effective intent / execution
  request 投影、严格 canonical codec、因果链、spec/policy lineage 与黄金向量。
- `ea.core.portfolio` 与 `ea.portfolio.ledger` 已按
  [Accepted ADR 0010](docs/adr/0010-canonical-fill-ledger-and-portfolio-snapshots.md) 实现单一
  Fill ledger、原子应用/重放/冲突结果，以及供 risk 消费的 immutable canonical
  `PortfolioSnapshot`。
- `ea.core.risk` 与 `ea.risk.authority` 已按
  [Accepted ADR 0011](docs/adr/0011-deterministic-pre-trade-risk-authority.md) 和
  [Accepted ADR 0012](docs/adr/0012-risk-conflict-dispatch-sequence-compatibility.md) 实现
  deny-by-default Phase 1 policy、position/order quantity limits、replay-stable
  allow/resize/reject/evaluation-failed、owner ID、证据摘要和 monotone halt，并保留 v1
  `OrderIntent` 的 exact non-negative dispatch domain。Risk 边界本身不创建 Order、不维护
  shadow ledger，也不替代 execution、runtime gate 或 reconciliation。
- `ea.execution.authority` 已按
  [Accepted ADR 0013](docs/adr/0013-risk-result-issuance-provenance.md) 实现 shared OMS 的
  Order-creation authority：只接受完整且重新证明的 `OrderIntent + RiskEvaluationResult`，
  并要求 exact canonical tuple 已存在于绑定 Risk authority 的非淘汰签发注册表；静态
  order-limit 真值表只是纵深防御。approval/intent/decision 三组 identity 保证一次性消费、
  exact replay 与 conflict，`EXECUTION_ORDER` 确定性分配，并在单次 state publication 前
  预检 canonical Order、execution request、digest 与 client submission key，并提供
  authority-backed Order ID / client submission key resolution ports。该切片不提交 venue，
  也不替代紧邻 submission 的 audit/freshness/halt gate。
- `ea.core.runtime` 与 `ea.runtime` 已按
  [Accepted ADR 0009](docs/adr/0009-runtime-root-ordering-clarifications.md) 建立 safety、execution
  fact、market、timer、end-of-run 的全局 root key、完整有界计划验证和不可插入的单消费者顺序
  queue；并按 [Accepted ADR 0014](docs/adr/0014-trusted-execution-fact-dispatch-and-order-projection.md)
  增加 source issuance、单 active fact dispatch lease、non-evicting dispatch history 与 exact
  acknowledgement；并按
  [Accepted ADR 0016](docs/adr/0016-deterministic-historical-runtime-frontier.md) 增加只读 virtual
  clock、固定 producer set 的 run-wide time arbiter、continuous dispatch sequence、one-event
  historical frontier、ack 后 source cursor commit，以及 versioned canonical trace/digest。
  reconciliation rank 仅保留词汇，不存在 opaque placeholder；这些切片尚不是完整 lifecycle
  coordinator，也不包含 audit gate、stage callback、strategy/portfolio orchestration 或 matcher。
- `ea.core.execution_state` 与 `ea.execution.fact_authority` 已按 Accepted ADR 0014 实现 exact
  fact replay/conflict、authority-backed Order correlation、staged venue binding、deterministic
  Fill allocation、coherent observed quantity、bounded immutable Order projection、closed
  processing outcome 与 monotone halt。它尚未接入 ledger/runtime coordinator，也不实现 venue
  adapter、reconciliation correction 或 matcher。
- `ea.data.historical` 已按
  [Accepted ADR 0015](docs/adr/0015-strict-historical-ohlcv-source.md) 实现固定
  `ea-phase1-ohlcv-csv-v1`：严格 UTF-8/CSV/token/time/identity/revision 验证、稳定 regular-file
  capture、ADR 0004 canonical order、ADR 0006 semantic fingerprint，以及只公开
  `next_available_at` 和 clock-gated `admit` 的 immutable bounded source。它不提供未来 payload
  iterator，也不推进 clock；outer `ea.data` bridge 私有持有 concrete cursor，并按 ADR 0016
  只在 exact runtime acknowledgement 后提交。完整 coordinator、matcher 与结果报告仍由后续
  切片实现。
- 每次迭代使用专家只读审查、单写入者实现、独立验证和 Pull Request 交付。

协作规则见 [AGENTS.md](AGENTS.md)，Git 与发布流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 目标模块

```text
src/ea/
  core/          # 领域模型、事件、时间、资产标识、错误类型
  composition/   # 唯一 outer composition root；绑定配置、数据、provenance 与运行能力
  runtime/       # 统一 coordinator、事件顺序、生命周期和 composition boundary
  config/        # 配置加载、校验、环境变量、密钥引用
  cli/           # ea doctor/data/backtest/paper/live/report
  data/          # 数据接入、清洗、存储、质量检查
  features/      # 因子、指标、特征工程
  strategy/      # 策略接口、策略状态和 Signal
  portfolio/     # PortfolioTarget、OrderIntent、canonical ledger、绩效归因
  backtest/      # 历史 feed、虚拟时钟、模拟 venue 和结果 adapter profile
  paper/         # 实时/replay feed、模拟 venue adapter profile
  execution/     # 所有模式共享的 OMS、订单状态机、report normalize 和对账
  brokers/       # live broker/exchange SDK adapter（当前不可用）
  risk/          # risk decision、限仓、限损、熔断、kill switch
  monitoring/    # 日志、指标、告警、审计 trail
  experiments/   # 实验追踪、参数、数据版本、模型版本
```

所有产生订单或 P&L 的模式都必须复用
`strategy -> portfolio -> risk -> shared execution/OMS`，只替换 clock、feed、venue、audit、result
等 adapters。已接受的运行契约见
[ADR 0003](docs/adr/0003-shared-runtime-ports-and-adapters.md)，完整 ownership、mode matrix
与生命周期见 [架构文档](docs/architecture.md)；deterministic execution、matching、ledger
和 reconciliation 契约见
[Accepted ADR 0008](docs/adr/0008-deterministic-execution-and-reconciliation.md)；root safety
suffix、sequence authority、factory-only plan 与错误映射的实现澄清见
[Accepted ADR 0009](docs/adr/0009-runtime-root-ordering-clarifications.md)；Risk result 的
canonical issuance provenance 边界见
[Accepted ADR 0013](docs/adr/0013-risk-result-issuance-provenance.md)；trusted execution-fact
dispatch 与 Order projection authority 见
[Accepted ADR 0014](docs/adr/0014-trusted-execution-fact-dispatch-and-order-projection.md)。
严格 historical OHLCV source boundary 见
[Accepted ADR 0015](docs/adr/0015-strict-historical-ohlcv-source.md)；deterministic historical
frontier、virtual clock、run-wide arbitration 与 trace contract 见
[Accepted ADR 0016](docs/adr/0016-deterministic-historical-runtime-frontier.md)。

订单、数量、费用、现金、持仓、风险限制和账本金额不得使用 `float`。当前 economic value
边界只接受 canonical decimal text，使用无界整数 coefficient/scale 完成精确乘法，并仅在
currency quantum settlement 处执行一次 signed `ROUND_HALF_EVEN`。Instrument spec set
使用封闭、版本化 JSON 和 domain-separated SHA-256；调用方不能提供或覆盖 digest。

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

## 本机 VSCode 开发环境

项目固定使用 Python 3.12 和 uv 0.11.28，并以仓库内真实、非隐藏的 `venv/` 目录作为本机 VSCode 运行环境。
不要创建 `.venv -> venv` 符号链接；所有创建或使用项目环境的 uv 命令都显式指定
`UV_PROJECT_ENVIRONMENT=venv`。

macOS 首次设置使用 uv 官方的版本化安装器安装项目要求的全局版本；若 `uv --version`
已经报告 0.11.28，可跳过安装命令：

```bash
curl -LsSf https://astral.sh/uv/0.11.28/install.sh | sh
uv --version
python3 scripts/bootstrap_local.py
```

首次运行 standalone installer 后，若当前 shell 尚未找到 `uv`，请按安装器提示加载其环境文件
或重启终端，再执行 `uv --version`。

`pyproject.toml` 会拒绝不匹配的 uv 版本。其他安装方式见
[uv 官方 standalone installer 文档](https://docs.astral.sh/uv/getting-started/installation/#standalone-installer)。
bootstrap 会依据 `.python-version` 创建 `venv/`、执行 locked editable sync，并从仓库外验证
isolated import、真实 `ea` console entrypoint 与 `uv run`。若旧 `.venv` 仍存在，脚本只提示其已废弃，
不会修改或删除它。

VSCode 会通过 `.vscode/settings.json` 自动指向：

```text
${workspaceFolder}/venv/bin/python
```

若 VSCode 已为该工作区缓存过旧解释器，请执行 `Python: Select Interpreter`，选择
`venv/bin/python`，然后执行 `Developer: Reload Window`。

## 严格配置边界

配置只在 CLI / composition boundary 加载一次，并形成可传递不可变的 typed snapshot。来源优先级从低到高为：

```text
typed defaults < versioned YAML < process environment < CLI
```

支持的环境变量只有：

- `EA_CONFIG_PATH`：YAML 路径；相对路径从当前工作目录解析；
- `EA_ENVIRONMENT`：`development | staging | production`；
- `EA_RUN_MODE`：`backtest | paper | live`。

未知、旧版或大小写不规范的 `EA_*` 名称会失败，不会静默忽略。`live` 是保留 vocabulary，
但当前 live profile 不可构建，最终 snapshot 会 fail closed。YAML 必须声明
`schema_version: 1`，使用 `run.mode`，且重复/未知 key 会失败。项目不会自动读取 `.env`；
`.env.example` 只记录可显式导出的 process-environment 名称。CLI override 必须位于子命令之前：

```bash
export EA_CONFIG_PATH=configs/backtest/example.yaml
export EA_ENVIRONMENT=development
export EA_RUN_MODE=backtest
venv/bin/ea doctor
venv/bin/ea --config configs/backtest/example.yaml \
  --environment development --run-mode backtest doctor
```

YAML、CLI、normalized snapshot 和日志都不得包含 broker/exchange raw credential。当前 schema
没有 secret 字段；`SecretRef` 只定义 opaque identifier，未来 adapter 只能在 outer boundary
解析它，resolved payload 不得返回 snapshot、runtime message 或 audit。
完整决策见 [Accepted ADR 0005](docs/adr/0005-strict-typed-configuration.md)。

## 可复现 run lineage（Issue #14）

[Accepted ADR 0006](docs/adr/0006-reproducible-run-manifest-and-audit-lineage.md) 已冻结
Phase 1 的最小 run manifest：UUID4 `run_id` 标识一次执行尝试，deterministic
`lineage_sha256` 标识相同的 clean code、normalized config、point-in-time data、UTC replay
window、effective parameters、runtime/dependencies 与 seed。

V1 只接受有界 `backtest`。一个 tracked outer launcher 会在导入 `ea` 前，用
`python -I -B` 等价的 isolated Python 验证当前 clean Git checkout、locked editable
`venv`、实际源码、active-environment distribution inventory 与完整 `src/` import surface，
并在 preparation 期间禁写 bytecode。普通 source-backed cache 只有在 code object 与对应
tracked source 重新编译后完全一致时才允许；sourceless/tampered cache、隐藏 import artifact
与 symlink 全部失败，collector 不会静默清理。`paper`/`live` 需要后续 schema。经济因果链
使用固定顺序的 binary64 运算与独占、串行消费的 PCG64 raw-word stream，不把无法证明的
“整个进程只有一个线程”写成 lineage 承诺。

Manifest 必须在 audit、feed 和 output 启动前持久写入 `results/<run_id>/manifest.json`，且不可改写；
相同 lineage 的重跑使用新 UUID 和新目录。路径、hostname、wall-clock、raw secret 与 UUID
本身不进入 lineage hash。数据指纹覆盖 ADR 0004 的 source、sequence、revision、
`available_at`、interval、identity、adjustment 和 float64 bits，而不是文件路径或 final bars。
Raw secrets 不得出现在 manifest 的任何字段中，因此也不可能进入其 hash。

受支持的生产路径必须从 tracked `scripts/reproducible_run.py` 开始：launcher 在 pre-import
检查成功后的 bootstrap stack frame 内创建一次性 grant（模块不暴露可重放的 constructor、
issuer seal 或 publisher），登记 exact process-local object identity 后才动态导入
`ea.composition.run`。Composition 立即兑换并清除该 pending grant，签发一次性 preflight
session。Preparation 强制消费该 session，从中取得不可由 caller 改写的 repository/commit，
再重新采集
Git/runtime/lock evidence，把同一个 `MarketDataSelection` 的 window、events 与重算 fingerprint
绑定到 lineage 并持久化 manifest。首条 mandatory audit acknowledgement 返回后，它才把 exact
event tuple、`RunReference`、lineage-bound RNG 与 numeric capability 交给 feed/runtime。
直接导入 composition、调用底层 manifest builder 或 result store 都不能形成 reproducibility
claim。

干净分支上可单独验证 tracked、stdlib-only 的 pre-import launcher：

```bash
venv/bin/python -I -B scripts/reproducible_run.py
```

这个入口会在任何 `ea` 模块执行前检查 clean HEAD、完整 `src/` import surface、source-backed
cache、symlink/shadow、locked editable environment 与 import topology，然后通过 exact pending
grant 进入 composition 并建立一次性的 preparation gate。Issue #14 已完成，但其范围不包含
尚未实现的 Phase 1 historical runtime。

常用质量门禁：

```bash
uv run --no-project --python 3.12 python scripts/verify.py --profile quality
```

提交前完整验证会在上述质量门禁之后构建并比较两个隔离 wheel，再用独立环境验证成品：

```bash
uv run --no-project --python 3.12 python scripts/verify.py --profile full
```

质量 profile 包含上述 tracked、stdlib-only 复现准备门禁。脚本从 `pyproject.toml` 读取项目
和 uv 版本；CI 也调用同一入口。不要使用 `--no-build-isolation`，也不要把 setuptools 或
wheel 加入应用/dev 依赖来替代 `build-constraints.txt`。

VSCode 已提供：

- Python 解释器配置
- pytest 发现配置
- Ruff formatter/linter 配置
- `EA: test`、`EA: lint`、`EA: format check`、`EA: typecheck`、`EA: doctor` tasks
- `EA doctor` debug configuration

## 参考框架

- [QuantConnect LEAN](https://github.com/QuantConnect/Lean)：参考 Universe / Alpha / Portfolio / Risk / Execution 的模块边界。
- [NautilusTrader](https://github.com/nautechsystems/nautilus_trader)：参考研究、仿真、实盘同一事件语义。
- [vn.py](https://github.com/vnpy/vnpy)：参考 Gateway / App / Event Engine / Risk Manager 和国内市场接入。
- [Freqtrade](https://github.com/freqtrade/freqtrade)：参考 dry-run/live、CLI、配置、Docker、监控与加密货币 bot 运维。
- [vectorbt](https://github.com/polakowo/vectorbt)：用于研究阶段参数扫描，不作为实盘核心。
- [Backtrader](https://github.com/mementum/backtrader) 与 [Zipline Reloaded](https://github.com/stefan-jansen/zipline-reloaded)：参考回测 API 与研究工作流。

## GitHub 接入

当前 Git/GitHub 配置：

- GitHub 仓库名称：`ea-quant`
- 仓库归属：`jayjcc8-cloud`
- 可见性：`private`
- Git 身份：`jayjcc8-cloud <275956764+jayjcc8-cloud@users.noreply.github.com>`
- Remote 协议：SSH
- Remote URL：`git@github.com:jayjcc8-cloud/ea-quant.git`
