import { useEffect, useState } from 'react'
import type { ApiAdapter } from './app'
import type { DesktopStatus } from './desktop-settings'

export function WorkspaceRecoveryGuide() {
  return <details className="provenance"><summary>手动备份、恢复与升级</summary>
    <ol>
      <li>先发布需要保留的数据、保存研究参数和设置；复制未发送文字及导入记录。等待回测结束，若有模拟请显式停止并确认。结果未知时先查原请求，不重发。</li>
      <li>记录当前工作区的完整绝对路径及关于页构建信息，然后正常退出 QUINE。确认所有使用该工作区的 QUINE 都已关闭，再用访达复制整个工作区文件夹作为冷备份；不要只复制 workspace 子目录或运行中的数据库。</li>
      <li>备份包含数据、报告和可能的模型凭据，存放于私人目录，不上传诊断。保留旧应用副本。升级时仅替换应用，重开同一路径工作区，先核对研究、数据版本、历史报告和模型状态，不自动运行研究。</li>
      <li>恢复前退出应用，把现有工作区改名留存，再将完整冷备份复制回原绝对路径；不要合并目录或覆盖正在使用的工作区。路径改变可能使冻结输入不可用，跨路径迁移未获验证。若设置曾切换工作区，确认下次启动位置与记录一致。</li>
      <li>升级后如提示实现身份不兼容，可继续从回测记录查看原报告；要执行当前实现，应另建研究、重新预览确认以形成新证据。不要编辑旧报告、摘要或身份文件。回退使用升级前冷备份与旧应用，不能假定旧版能读新版写入的数据。</li>
    </ol><p>这里是人工冷备份流程，不是自动备份或迁移工具；验证范围见随候选的验收记录。系统强退、磁盘损坏和跨版本格式迁移不在本轮保证范围。</p>
  </details>
}

export function DesktopAbout({ api }: { api: ApiAdapter }) {
  const [status, setStatus] = useState<DesktopStatus | null>(null)
  const [error, setError] = useState('')
  useEffect(() => { let active = true; void api.desktopSettings?.().then(value => { if (active) setStatus(value) }).catch(() => { if (active) setError('桌面身份读取失败；未用前端版本猜测。') }); return () => { active = false } }, [api])
  const app = status?.application
  // A deliberate allowlist: never serialize settings, endpoint URLs, logs, user paths or names.
  const diagnostic = JSON.stringify({ product: 'QUINE', desktop_version: app?.desktop_version ?? null, engine_version: app?.engine_version ?? null, build_id: app?.build_id ?? null, source_revision: app?.source_revision ?? null, source_dirty: app?.source_dirty ?? null, built_at: app?.built_at ?? null, workspace: status ? '[redacted]' : null, workspace_switch_pending: status ? status.workspace !== status.next_workspace : null, startup_error_present: status ? !!status.startup_error : null, model_configuration_error_present: status ? !!status.model_error : null }, null, 2)
  return <section className="panel"><h2>关于 QUINE</h2><p>桌面本地研究与确定性回测。实盘未接入；窗口最小 850 × 600，原生完整键盘、中文 IME 与系统文字放大仍需发布验收。</p>
    {error && <p role="alert">{error}</p>}
    <dl className="summary-list"><div><dt>桌面版本</dt><dd>{app?.desktop_version ?? '未知（构建来源未记录）'}</dd></div><div><dt>引擎版本</dt><dd>{app?.engine_version ?? '未知'}</dd></div><div><dt>构建标识</dt><dd>{app?.build_id ?? '未知（构建来源未记录）'}</dd></div><div><dt>源码基线</dt><dd>{app?.source_revision ?? '未知'}{app?.source_dirty === true ? ' · 含未提交修改' : ''}</dd></div><div><dt>当前工作区（本机信息）</dt><dd>{status?.workspace ?? '未读取'}</dd></div></dl>
    <details><summary>可分享的脱敏诊断</summary><p>仅含版本、构建与布尔状态；不含工作区路径、凭据、端点、研究内容或原始日志。展开后全选复制提交即可。</p><textarea aria-label="可分享的脱敏诊断" rows={10} readOnly value={diagnostic} onFocus={event => event.currentTarget.select()} /></details>
    <WorkspaceRecoveryGuide />
  </section>
}
