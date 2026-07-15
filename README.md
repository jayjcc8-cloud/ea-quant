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
