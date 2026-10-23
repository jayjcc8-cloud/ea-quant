# 新前端所需的后端能力契约

本契约从 `apps/web/studio/src/model.ts`、页面动作与浏览器验收推导。它不引用旧 API，不指定 URL、HTTP/RPC 或数据库。当前实现是浏览器内 `ScenarioAdapter`；下列服务端能力均未接入。本轮的固定曲线、交易、任务进度和 Agent 回复不能作为研究结果。

## 通用约定

- 对象 ID 为不透明字符串，类型相互独立：Research、Proposal、DatasetVersion、Experiment、Report、Batch、Holdout、Candidate、Simulation、Request、Connection、AgentTask。显示名称不能替代 ID。
- 对象查询返回 `id / version / updatedAt / capabilities`。时间使用带时区的 ISO 8601；市场日期另外给出市场日历和时区。价格、数量、费用与资金使用明确币种、精度和十进制定点表达；比例字段标注百分比单位。
- 可修改对象写入携带 `expectedVersion`。不匹配返回 `version_conflict`，附当前版本、当前内容和原提交基线；不得覆盖用户缓冲。冻结报告、实验输入、数据版本、已经记录的决定不可原地重写。
- 成功动作返回 `{objectId, version, requestId?, taskId?, resultRef?}`，异步接受不等于执行成功。字段错误返回 `{code, message, fieldErrors, requestId?, retryable, currentVersion?}`。页面在操作区域显示原错误细节，保留用户输入。
- 任何可能执行的请求在提交前生成稳定 `idempotencyKey`。同 key、同规范化 payload 返回原 Request 和原结果；同 key 不同 payload 返回冲突。提交状态不明必须按 key 或 requestId 查询，不能以新 key 自动重试。
- Request 独立于业务对象：`operation / objectId / submittedAt / queriedAt / state(pending,unknown,confirmed,failed) / resultRef / error`。网络断开只改变可观测状态，不凭空生成服务端失败事实。
- 任务进度：`taskId / requestId / phase / completedUnits / totalUnits? / sequence / observedAt / resultRef?`。轮询或订阅均可，支持序号断点恢复；进度与最终报告身份相互关联，客户端不推算成功。
- 集合查询统一支持 `query / type / status / dateRange / sort / cursor / limit`，返回 `items / nextCursor / total? / asOf`。排序稳定，以 ID 为次排序键；总数不等于本页条数。前端当前使用本地筛选和分页，未来不得以仅当前页筛选冒充全量。
- 读取可由 AbortSignal 取消，取消不清空已有输入。已经接受的执行通过独立停止请求处理，客户端取消等待不撤销服务端动作。确认层的“取消”只关闭确认，不发送执行请求。

## 动作与对象能力

| 用户动作 | 输入及版本条件 | 输出与可见结果 | 错误、幂等与恢复 |
| --- | --- | --- | --- |
| 查询首页、研究和全部记录 | 工作区、查询条件、游标 | 研究摘要、当前修订、最近活动、派生待办数、可继续动作 | 失败保留上次结果并显式重试 |
| 保存新研究 | goal、可选 datasetVersionId/templateId、创建键 | researchId、r1；允许尚未绑定 | 不运行实验；目标空值就地校验 |
| 编辑目标与笔记 | researchId、expectedVersion、goal/notes | 更新后研究与版本 | 缓冲与正式保存分开，冲突返回当前值 |
| 绑定资源、保存参数 | researchId、expectedRevision、完整参数/成本/区间、不可变资源 ID | 新 DraftRevision，修订历史 | 同模板重新绑定保留正在编辑值；更换模板默认参数需明确告知；数值非有限值、区间越界禁止保存 |
| 查看草稿历史 | researchId、修订或游标 | 只读快照及来源 | 历史不覆盖当前草稿 |
| 生成提案 | researchId、baseRevision、意图、模型连接 ID、任务键 | proposalId/taskId、冻结基线、建议差异、来源引用 | 旧提案保留；生成不应用、不运行；失败不自动重试 |
| 应用/拒绝提案 | proposalId、expectedProposalVersion、expectedDraftRevision | 应用返回新修订；拒绝只变提案状态 | 基线过期阻止应用，返回原基线/建议/当前；重新生成是新显式动作 |
| 查询/提交实验确认 | researchId、expectedRevision、冻结输入指纹、执行键 | 可复用历史证据，或 experimentId/requestId/taskId | 冻结数据、参数、费用、滑点、区间；后续编辑不改变实验。重复点击返回同一身份 |
| 查询实验状态 | experimentId/taskId/requestId 或幂等 key | 进度、最后观测、报告或结构化失败 | unknown 仅查原请求；恢复同一身份 |
| 停止实验、重试失败实验 | experimentId、停止键；重试引用明确失败对象及新键 | 停止 Request；重试新实验与原输入关联 | 停止确认前不显示已停止；unknown 不允许“重试为新实验” |
| 打开报告、交易、输入、文件 | reportId；交易方向/查询/游标 | 独立冻结输入、权益/基准序列、指标定义、交易、文件引用 | 同一报告各视图同源；没有完成证据不能展示完成报告；导出注明模拟/正式来源 |
| 比较两个实验 | 两个 experimentId/reportId | 可比较性检查、输入差异、同口径指标与曲线 | 数据/区间不兼容时说明原因，不拼接为同条件比较 |
| 保存参数批次 | researchId、expectedRevision、选择的不同参数组合、可选 savedBatchId/version | batchId、冻结公共输入与组合；saved | 2–10组合范围是当前前端约束；重复/非法组合整批拒绝。保存不执行 |
| 审阅并启动批次 | savedBatchId、expectedVersion、执行键 | 原批次成员列表、每个实验请求身份 | 刷新后可继续同一 savedBatch；运行状态由成员汇总，部分失败单独呈现；失败成员重试保留关联 |
| 保存/执行留出 | sourceReportId、后续 datasetVersionId、起止区间；savedHoldoutId/version、执行键 | holdoutId、独立实验/报告、来源报告引用 | 参数与成本从来源报告冻结，区间不重叠；保存不执行；重新调参必须另建实验 |
| 创建候选、查看证据 | sourceReportId、holdoutId | pending candidateId、两份证据 | 同证据关系防重复；缺失报告不能决定 |
| 接受/拒绝候选 | candidateId、expectedVersion、decision、1–500字reason、决定键 | 不可覆盖的人工决定、决定时间 | 取消不写；决定不创建模拟、不授予任何执行权限 |
| 保存模拟配置 | candidateId、config、可选 simulationId/expectedConfigVersion | saved instance、configVersion、冻结来源输入 | 仅已接受候选；校验资金成本限额；已启动配置不可覆盖；保存不启动 |
| 独立确认模拟启动 | simulationId、expectedConfigVersion、启动键 | start Request、确认后的 processStatus | 摘要必须与保存版本相同；重复点击/刷新恢复同一启动请求 |
| 读取模拟概览/持仓/成交/事件 | simulationId、观测游标、事件类型/查询/分页 | processStatus、observedAt、freshness、现金/数量/报价/权益、带身份记录 | 资金单位一致；持仓市值+现金=同一时刻权益；没有成交则为空，不能用示例填充 |
| 停止模拟、查询结果 | simulationId、停止键；原 requestId | pending/unknown 请求，随后确认状态与新观测 | 请求停止不等于 stopped；不自动平仓；刷新保留请求、最后快照；陈旧不是已停止 |
| 数据目录/版本/样本 | query、source、versionId、样本游标 | 版本元数据、字段、市场/时区、质量、指纹、样本 | 版本只读；目录结构校验与研究充分性分开 |
| CSV预检与发布 | 文件内容或上传令牌、字段映射、标的/周期/时区、校验摘要、发布键 | 行级问题或新 datasetVersionId、内容指纹 | 当前仅本地≤2MB CSV；必须检查六字段、有效升序唯一日期、数字与OHLC；问题禁止发布，不覆盖旧版本 |
| 模板查询/登记/来源 | templateId、名称/规则/来源元数据、版本 | 可检查模板版本与默认参数 | 当前登记仅元数据，不执行用户代码；真实执行能力另行定义 |
| 模型配置测试与保存 | 非秘密配置快照、connectionId/expectedVersion；未来凭据引用 | 独立 TestResult 与所测配置指纹；保存后版本和默认连接 | 测试成功只适用于原快照，不能覆盖新输入或给新配置贴成功标签；当前只演示超时/重试，无密钥收集 |
| Agent提问/过程/引用 | objectType/id/version、证据引用、prompt、模型连接、任务键 | taskId、消息、活动事件、可导航引用 | 不能用当前草稿解释旧报告；执行与提案另行确认；输出须区分演示与真实模型任务 |
| 待办、稍后处理、恢复 | 对象筛选、attentionId、个人延后偏好 | 从真实对象派生的列表和计数 | “稍后”不改变提案/候选/请求事实，有恢复入口 |
| 全局搜索与命令 | query、类型、游标 | 精确对象目标/版本和 command intent | Enter仅导航至目标或确认页；IME组合期间不导航、不执行 |
| 外观偏好 | theme、density、fontSize、scale、reducedMotion、配置版本 | 立即应用的偏好 | 不必触发工作区/服务重启；系统主题订阅可取消 |
| 工作区位置与诊断 | 下次启动位置；诊断勾选项 | 当前与下次位置分别返回；脱敏可预览诊断包 | 实际迁移另行授权；当前前端只保存位置说明，浏览器刷新不迁移；导出排除密钥及个人路径 |

## 实盘与交易所预览边界

本轮的 `live.preview.save/stop/query` 只更新隔离场景里的预览对象。资金和候选在配置、工作台、详情间一致，停止预览有原请求身份；它们不是生产执行接口。实际启动控件不可用，页面与确认层常驻“功能预览 · 未接入”。

未来账户读取、行情订阅、订单执行必须分别表达 `connected / authorized / scope / observedAt / error`。账户接入不能隐含授予下单权限。真实执行的授权、风险检查、账本、恢复与审计不在本轮实现或验证范围，不能把离线 `ScenarioAdapter` 当作其安全实现。

## 当前演示适配器的明确替代点

1. `fixtures.ts` 固定元数据、曲线和交易 → 后端不可变证据与真实计算结果。
2. `ScenarioAdapter.read/execute/testConnection` → 上述能力接口；纯状态测试保留为消费者约束。
3. 浏览器 localStorage + sessionStorage → 工作区持久化与编辑缓冲；后端补多客户端版本/权限语义。
4. `tick` → 服务端任务观测；本地进度不是引擎进度。
5. 预设 Agent 消息/活动 → 可追溯真实模型任务，仍不绕过独立执行确认。
6. 本地列表/过滤/分页 → 稳定服务端集合查询；CSV实际执行数据量限制另行协商。
