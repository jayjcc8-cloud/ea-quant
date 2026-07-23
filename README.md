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
- Phase 0 已具备 Python 项目骨架、配置、领域模型、CLI、测试、VSCode 配置和 CI 质量门禁。
- 每次迭代使用专家只读审查、单写入者实现、独立验证和 Pull Request 交付。

协作规则见 [AGENTS.md](AGENTS.md)，Git 与发布流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 目标模块

```text
src/ea/
  core/          # 领域模型、事件、时间、资产标识、错误类型
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
与生命周期见 [架构文档](docs/architecture.md)。

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

[Proposed ADR 0006](docs/adr/0006-reproducible-run-manifest-and-audit-lineage.md) 正在冻结
Phase 1 的最小 run manifest：UUID4 `run_id` 标识一次执行尝试，deterministic
`lineage_sha256` 标识相同的 clean code、normalized config、point-in-time data、UTC replay
window、effective parameters、runtime/dependencies 与 seed。

Manifest 必须在 audit、feed 和 output 启动前持久写入 `results/<run_id>/manifest.json`，且不可改写；
相同 lineage 的重跑使用新 UUID 和新目录。路径、hostname、wall-clock、raw secret 与 UUID
本身不进入 lineage hash。数据指纹覆盖 ADR 0004 的 source、sequence、revision、
`available_at`、interval、identity、adjustment 和 float64 bits，而不是文件路径或 final bars。
Raw secrets 不得出现在 manifest 的任何字段中，因此也不可能进入其 hash。

常用质量门禁：

```bash
uv --version
uv lock --check
git diff --exit-code HEAD -- uv.lock
UV_PROJECT_ENVIRONMENT=venv uv sync --locked --extra dev
venv/bin/python -I -c "import importlib.metadata as m; import ea; assert m.version('ea-quant') == ea.__version__ == '0.1.1'"
venv/bin/ea doctor
UV_PROJECT_ENVIRONMENT=venv uv run --locked pytest -q
UV_PROJECT_ENVIRONMENT=venv uv run --locked ruff check .
UV_PROJECT_ENVIRONMENT=venv uv run --locked ruff format --check .
UV_PROJECT_ENVIRONMENT=venv uv run --locked mypy
UV_PROJECT_ENVIRONMENT=venv uv run --locked ea doctor
```

可复现 wheel 使用独立于 `uv.lock` 的哈希构建约束，并保持 PEP 517 构建隔离：

```bash
export SOURCE_DATE_EPOCH="$(git show -s --format=%ct HEAD)"
UV_PROJECT_ENVIRONMENT=venv uv build --wheel --clear \
  --build-constraints build-constraints.txt --require-hashes --out-dir build/wheel-a
UV_PROJECT_ENVIRONMENT=venv uv build --wheel --clear \
  --build-constraints build-constraints.txt --require-hashes --out-dir build/wheel-b
cmp build/wheel-a/*.whl build/wheel-b/*.whl
venv/bin/python -c "import hashlib, pathlib; p = next(pathlib.Path('build/wheel-a').glob('*.whl')); print(hashlib.sha256(p.read_bytes()).hexdigest(), p)"
```

不要使用 `--no-build-isolation`，也不要把 setuptools 或 wheel 加入应用/dev 依赖来替代
`build-constraints.txt`。

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
