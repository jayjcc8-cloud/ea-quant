# 39图覆盖与前端验收

依据完整最终图册逐幅查看后实施；全部界面是 React 组件，不使用图册位图或图册浏览器。源码入口 `apps/web/studio/`，基础版本 `90e7170e64547db36579f2d7af5d5aad3cc8ce43`。本表是唯一逐图覆盖表；方案见 [plan.md](plan.md)，启动和复位见 [README.md](README.md)。

验收载体为隔离的真实 Chromium，开发服务 `127.0.0.1:4176`。主对照视口1586×992，另验证1000×800和390×844。下表“通过”指本轮离线前端交互与视觉验收，不代表研究结果、真实后端或原生桌面发布。截图保留于本机忽略区；源码、场景、复位和测试纳入 Git。实施位置均相对于 `apps/web/studio/src/`。

| 图号 | 页面、组件、状态与实现位置 | 可到达的操作路径及实际验收 | 结果与证据 |
| --- | --- | --- | --- |
| 01 | 研究首页；Research.tsx、App.tsx | 四主区→研究；目标输入、继续研究与筛选、最近活动入口；首页无空Agent栏 | 通过：[首页](../../.local/frontend-v3/evidence/final-01.png) |
| 02 | 首次空态与新建；Research.tsx、fixtures.ts | `/#/dev`载入首次空态→研究→新建；可先保存目标，再补绑定；保存不执行 | 通过：[空工作区](../../.local/frontend-v3/evidence/02-empty-workspace.png)、[新建](../../.local/frontend-v3/evidence/02-empty-create.png) |
| 03 | 上下文绑定；Research.tsx、ui.tsx | 新研究→补齐上下文→选择数据/模板→取消或绑定保存；同模板重新绑定保留未保存参数 | 通过：[详情](../../.local/frontend-v3/evidence/final-03.png)、[绑定层](../../.local/frontend-v3/evidence/03-binding.png)、[保留编辑](../../.local/frontend-v3/evidence/review-rebinding.png) |
| 04 | 草稿与修订；Research.tsx、context.tsx | 研究→策略草稿→参数/费用修改→保存新版本；历史、刷新、返回和未保存离开 | 通过：[草稿](../../.local/frontend-v3/evidence/final-04.png) |
| 05 | 修改提案；Research.tsx、scenario.ts | 需要处理→P-008→审阅差异→应用；r4变r5，快线20→30，旧报告/实验数不变；也可拒绝 | 通过：[审阅](../../.local/frontend-v3/evidence/final-05.png)、[应用后r5](../../.local/frontend-v3/evidence/05-applied-r5.png) |
| 06 | 冻结实验确认；Research.tsx | 草稿保存→检查并运行→审阅数据、参数、成本、版本→取消或确认；重复点击只创建一个实验 | 通过：[确认](../../.local/frontend-v3/evidence/final-06.png) |
| 07 | 实验进度/结果；Research.tsx、scenario.ts | 确认后→实验标签→展开阶段→完成后打开报告；进度明确为演示 | 通过：[执行状态](../../.local/frontend-v3/evidence/07-progress.png)、[新报告](../../.local/frontend-v3/evidence/10-new-report.png) |
| 08 | 两实验比较；Reports.tsx | 记录选择E-021/E-020→比较；冻结输入差异、曲线与指标同源 | 通过：[比较](../../.local/frontend-v3/evidence/final-08.png) |
| 09 | 全部记录；Reports.tsx | 四主区→回测；类型/状态/日期/排序/搜索、分页、选择与清空；搜索无结果可恢复 | 通过：[记录](../../.local/frontend-v3/evidence/final-09.png)、[空筛选](../../.local/frontend-v3/evidence/09-empty-filter.png) |
| 10 | 报告概览；Reports.tsx、ui.tsx | 记录→REP-021→曲线切换与展开数据表、继续验证、导出；r4冻结输入跨刷新不变 | 通过：[报告](../../.local/frontend-v3/evidence/final-10.png)、[曲线数据](../../.local/frontend-v3/evidence/10-chart-data.png) |
| 11 | 交易、冻结输入、文件；Reports.tsx | 报告→交易→方向筛选/下一页/选择行检查；冻结输入标签；文件标签与下载 | 通过：[交易检查器](../../.local/frontend-v3/evidence/11-trade-inspector.png)、[冻结输入](../../.local/frontend-v3/evidence/11-inputs.png)、[文件](../../.local/frontend-v3/evidence/11-report-files.png) |
| 12 | 批次配置；Validation.tsx | 研究→实验→新建批次→添加/删除/选择组合→保存；刷新恢复原批次ID，再独立确认 | 通过：[配置](../../.local/frontend-v3/evidence/final-12.png) |
| 13 | 批次结果与失败成员；Validation.tsx | 研究实验或记录→B-004；成功/失败筛选、排序、两项比较；查看失败原因；状态从成员汇总 | 通过：[结果](../../.local/frontend-v3/evidence/final-13.png)、[运行中](../../.local/frontend-v3/evidence/13-batch-running.png)、[失败原因](../../.local/frontend-v3/evidence/13-failure.png) |
| 14 | 留出配置；Validation.tsx | 报告→继续验证→留出→后续数据与区间→保存→确认；冻结来源参数，阻止重叠区间 | 通过：[配置](../../.local/frontend-v3/evidence/final-14.png) |
| 15 | 留出结果；Validation.tsx、Reports.tsx | 留出完成→H详情→查看来源与独立留出报告→创建候选；两份曲线各自同源 | 通过：[双报告](../../.local/frontend-v3/evidence/final-15.png) |
| 16 | 候选中心；Validation.tsx | 侧栏/记录→候选中心→待审/接受/拒绝、搜索、展开证据、决定记录 | 通过：[中心](../../.local/frontend-v3/evidence/final-16.png) |
| 17 | 候选人工决定；Validation.tsx | C-007→查看两份证据→填写理由→选择决定→确认或取消；接受后模拟数量不变 | 通过：[证据](../../.local/frontend-v3/evidence/final-17.png)、[决定确认](../../.local/frontend-v3/evidence/17-candidate-decision.png) |
| 18 | 模拟实例列表；Simulation.tsx | 四主区→模拟；搜索/状态标签/历史；配置实例或查看SIM-003 | 通过：[列表](../../.local/frontend-v3/evidence/final-18.png) |
| 19 | 模拟配置；Simulation.tsx | 已接受候选→配置资金、成本、限额→保存；刷新仍未启动；空数字刷新后可以修复 | 通过：[配置](../../.local/frontend-v3/evidence/final-19.png)、[空值恢复](../../.local/frontend-v3/evidence/review-numeric-recovery.png) |
| 20 | 模拟概览；Simulation.tsx | SIM-003→概览→刷新观测、查看配置；现金+市值=权益，观测与进程分离 | 通过：[概览](../../.local/frontend-v3/evidence/final-20.png) |
| 21 | 持仓/成交/事件检查器；Simulation.tsx | 实例→各标签→选行检查；事件搜索与类型过滤；资料来自同一快照 | 通过：[持仓](../../.local/frontend-v3/evidence/21-position-inspector.png)、[成交](../../.local/frontend-v3/evidence/21-trade-inspector.png)、[事件](../../.local/frontend-v3/evidence/21-event-inspector.png) |
| 22 | 实盘工作台预览；Live.tsx | 四主区→实盘→概览/委托/事件→配置/详情；常驻未接入标识 | 通过：[工作台](../../.local/frontend-v3/evidence/final-22.png) |
| 23 | 实盘配置预览；Live.tsx | 实盘→查看启动配置→修改预览额度→保存；工作台/详情额度同步；真实启动不可用 | 通过：[配置](../../.local/frontend-v3/evidence/final-23.png)、[编辑保护](../../.local/frontend-v3/evidence/37-live-unsaved.png) |
| 24 | 实盘详情/停止预览；Live.tsx | 工作台→详情→预览停止请求→取消/提交→查询；仅更新集中预览对象 | 通过：[停止预览确认](../../.local/frontend-v3/evidence/24-live-stop.png) |
| 25 | 数据目录与检查器；Resources.tsx | 数据与模型→数据→搜索/来源过滤→选择版本→样本/用于研究；精确绑定所选版本 | 通过：[目录](../../.local/frontend-v3/evidence/final-25.png)、[样本](../../.local/frontend-v3/evidence/25-sample.png) |
| 26 | CSV导入与错误恢复；Resources.tsx、scenario.ts | 导入→本地文件/示例→映射→问题行禁止发布→编辑修复→发布；以MSFT导入、创建研究并确认新报告交易标的 | 通过：[错误行](../../.local/frontend-v3/evidence/final-26.png)、[修复](../../.local/frontend-v3/evidence/26-csv-valid.png)、[发布](../../.local/frontend-v3/evidence/26-published.png)、[标的传播](../../.local/frontend-v3/evidence/26-msft-report.png) |
| 27 | 模板目录/参数/规则/来源；Resources.tsx | 数据与模型→策略与模板→选择模板→参数/规则/来源→查看源文件/创建研究；登记本地策略元数据 | 通过：[模板](../../.local/frontend-v3/evidence/final-27.png)、[规则](../../.local/frontend-v3/evidence/27-template-rules.png)、[来源](../../.local/frontend-v3/evidence/27-source.png) |
| 28 | 模型连接；Resources.tsx | 模型连接→编辑/新建→演示测试失败→重试→保存；测试期间更改输入不被旧结果覆盖；密钥字段不可编辑 | 通过：[连接](../../.local/frontend-v3/evidence/final-28.png)、[失败](../../.local/frontend-v3/evidence/28-model-failed.png)、[并发输入](../../.local/frontend-v3/evidence/review-model-race.png) |
| 29 | 交易所能力预览；Live.tsx | 侧栏→交易所配置→能力说明/接入规划/添加预览；账户、行情、执行分别标明未接入 | 通过：[能力](../../.local/frontend-v3/evidence/final-29.png) |
| 30 | 外观与深色；System.tsx、App.tsx、studio.css | 设置→外观→浅/深/系统、密度、字号、缩放、减少动效；系统主题随媒体条件变化，恢复默认 | 通过：[外观](../../.local/frontend-v3/evidence/30-dark-settings.png)、[深色草稿](../../.local/frontend-v3/evidence/30-dark-draft.png)、[125%](../../.local/frontend-v3/evidence/30-scaled.png) |
| 31 | 工作区、诊断；System.tsx | 设置→工作区→保存下次位置，当前位置不变；诊断检查/选择/预览/实际下载排除个人路径 | 通过：[工作区](../../.local/frontend-v3/evidence/final-31.png)、[诊断](../../.local/frontend-v3/evidence/31-diagnostics.png)、[下载文件](../../.local/frontend-v3/evidence/diagnostics.json) |
| 32 | 窄窗与Agent抽屉；App.tsx、ui.tsx、studio.css | 1000px研究详情→打开Agent→编辑→Esc→重开保留输入；390px四主区、导航菜单、报告与模拟 | 通过：[窄窗](../../.local/frontend-v3/evidence/32-narrow-draft.png)、[抽屉](../../.local/frontend-v3/evidence/32-agent-drawer.png)、[小窗报告](../../.local/frontend-v3/evidence/32-phone-report.png)、[小窗模拟](../../.local/frontend-v3/evidence/32-phone-simulation.png) |
| 33 | 需要处理；System.tsx、scenario.ts | 侧栏→待处理→审阅/稍后处理→稍后列表恢复→处理后列表；计数由对象状态派生 | 通过：[待办](../../.local/frontend-v3/evidence/final-33.png) |
| 34 | 实验结果未知恢复；Research.tsx、ui.tsx | dev载入实验未知→研究实验E-022→刷新→查询REQ-022；实验数量不增，原身份保留 | 通过：[未知](../../.local/frontend-v3/evidence/34-unknown.png) |
| 35 | 提案版本冲突；Research.tsx | dev载入冲突→P-008→基线20/建议30/当前25；应用禁用→显式重新生成，原案保留 | 通过：[冲突](../../.local/frontend-v3/evidence/35-conflict.png)、[重新生成](../../.local/frontend-v3/evidence/35-regenerated.png) |
| 36 | 全局搜索与命令；System.tsx、App.tsx | ⌘K/Ctrl+K→输入AAPL-daily-v4→方向键/Enter打开准确版本；空结果与Esc恢复焦点 | 通过：[搜索](../../.local/frontend-v3/evidence/36-search.png)、[空结果](../../.local/frontend-v3/evidence/36-search-empty.png) |
| 37 | 加载、失败、未保存、陈旧；App.tsx、context.tsx、Simulation.tsx | 读取失败→就地重试；草稿编辑→导航→继续/放弃/保存离开；观测陈旧→刷新，不改变进程事实 | 通过：[加载](../../.local/frontend-v3/evidence/37-loading.png)、[读取失败](../../.local/frontend-v3/evidence/37-read-error.png)、[离开](../../.local/frontend-v3/evidence/37-unsaved.png)、[陈旧](../../.local/frontend-v3/evidence/37-stale.png) |
| 38 | 模拟独立启动确认；Simulation.tsx | 保存实例→刷新→审阅并启动→核对保存版本→返回修改/确认；重复点击只启动原实例 | 通过：[启动确认](../../.local/frontend-v3/evidence/38-simulation-start.png) |
| 39 | 模拟停止/查询；Simulation.tsx、ui.tsx、scenario.ts | 运行实例→停止确认→提交仍显示运行中→刷新→查询原STOP请求→确认停止；未知查询不重发、不自动平仓 | 通过：[确认](../../.local/frontend-v3/evidence/39-stop-confirm.png)、[待确认](../../.local/frontend-v3/evidence/39-stop-pending.png)、[未知](../../.local/frontend-v3/evidence/39-stop-unknown.png)、[已停止](../../.local/frontend-v3/evidence/39-stopped.png) |

## 统一调整与理由

- 保留白/浅灰/石墨方向、224px侧栏、56px顶栏、正文15px与详情Agent。窄窗以内容可读性划分断点，不把位图尺寸写死。长表格局部横向滚动；小窗资金指标改为单列，避免数字被裁切。
- 同对象指标由一份曲线/交易/资金快照派生。REP-021为7.9%、最大回撤−10.6%、31笔；费用影响为实际场景交易费用计算的−0.19%，不照抄效果图中不闭合的−1.3%。SIM-003为现金97,243 + 20×199.85 = 权益101,240，浮动盈亏−3。
- P-008应用后显示r5；E-021/REP-021仍为r4。批次补齐独立的E-024/REP-024；留出报告独立为REP-025。既有模拟使用已接受C-006与其r3证据，待审C-007不会被暗中接受。
- 图表按组件实际宽度布局13px坐标文字，提供同源数据表；不依赖缩小SVG文字凑入双栏。确认层使用原生dialog并支持Esc与焦点返回。
- API Key仅保留不可编辑占位。模板登记只记录元数据。实盘/交易所能力保持明确预览边界，实际启动禁用。所有曲线、对话和进度标为演示。

## 检查结果与证据范围

本地 `lint:studio`、`typecheck:studio`、`test:studio`、`build:studio`通过。18项测试包括提案版本、冻结报告、去重、未知请求、停止状态、候选/配置不自动执行、批次恢复、留出区间、CSV质量、资金闭合、标的传播、复位取消、存储失败，以及下列3个真实组件回归。Node 25测试初始化明确使用jsdom自身Storage，避免误用Node原生localStorage；不替换产品存储逻辑。

一轮独立主审发现空数字刷新崩溃、重复绑定丢失未保存参数、旧模型测试结果覆盖新输入。一次集中修复后，主审限定复验和真实浏览器复现路径均通过；没有启动第二轮审查链。

真实浏览器完成新建研究→绑定→保存→确认→实验→报告，提案应用/冻结不变，比较/批次/留出/候选，模拟保存/取消启动/启动/停止查询，CSV修复发布，模型失败恢复，未保存三选项、刷新/返回、重复点击、深色与键盘操作。[操作记录](../../.local/frontend-v3/evidence/browser-checks.json)包含操作断言与截图名；[布局记录](../../.local/frontend-v3/evidence/viewport-audit.json)包含39个基础视图×3视口=117次检查，实际路由到达后检查，无页面崩溃或文档/主区域整体横向溢出。额外状态层证据见表中截图。

另以1000×800深色窄窗遍历全部39个基础视图，等待主题保存后逐页确认实际`data-theme=dark`，均无整体溢出或崩溃，见[深色记录](../../.local/frontend-v3/evidence/dark-audit.json)与[报告截图](../../.local/frontend-v3/evidence/dark-final-_report_REP-021_overview.png)。

[网络记录](../../.local/frontend-v3/evidence/network-audit.json)覆盖最终路由遍历与空态复位，外部请求为0；2个blob请求为本地文件下载。源码无fetch、XHR、WebSocket或旧业务模块导入。Vite开发资源请求仅访问独立本地端口。整个功能浏览器会话未捕获pageerror。

中文输入使用真实Chromium的`Input.imeSetComposition`进行组合文本输入，组合期间Enter没有发送；提交中文后Enter仅发送一次，见[输入截图](../../.local/frontend-v3/evidence/32-ime.png)。未执行macOS系统拼音候选窗或原生WebView验收，不扩大该结论。

当前边界：单浏览器标签的离线演示持久化，没有跨标签协作；没有真实引擎、模型、账户、交易所或后端；未替换原生入口、签名打包、远端CI、合并或发布。固定演示交易不能证明策略稳健性。后续接入需求见[接口契约](contract.md)。
