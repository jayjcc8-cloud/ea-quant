import type { ObservationReading, PaperObservation } from './paper-observation'

/**
 * Presentation only. Every word, tone and timestamp below is the supplied
 * observation rendered as given; this component reads no clock, holds no state and
 * decides nothing about readiness, permission or freshness.
 */
export function PaperRuntimeStatus({ observation }: { observation: PaperObservation }) {
  const reading = (title: string, value: ObservationReading) => (
    <div>
      <span className="paper-runtime-label">{title}</span>
      <span className={`paper-runtime-value tone-${value.tone}`}>
        <span className="paper-runtime-icon" aria-hidden="true" />
        {value.text}
      </span>
    </div>
  )
  return <section className="paper-runtime-status" aria-label="运行观测">
    <div className="paper-runtime-readings">
      <div>
        <span className="paper-runtime-label">运行状态</span>
        <span className="paper-runtime-value">
          <span className="paper-runtime-icon" aria-hidden="true" />
          {observation.stateLabel}
        </span>
      </div>
      {reading('进程 / 租约', observation.process)}
      {reading('依赖就绪', observation.readiness)}
      {reading('操作性许可', observation.permission)}
    </div>
    <div className="paper-runtime-body">
      <p className="paper-runtime-context">{observation.context}</p>
      <div className="paper-runtime-times">
        <span>本次读取：{observation.observedAt}</span>
        <span>健康投影：{observation.healthAt}</span>
        <span>状态发布：{observation.statusAt}</span>
      </div>
      {!!observation.reasons.length && <div className="paper-runtime-reasons">
        <span className="paper-runtime-label">读取时受阻原因</span>
        <ul>{observation.reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul>
      </div>}
      <p className="paper-runtime-caveat">仅反映操作性门槛，不保证具体订单获准。</p>
      <details className="paper-runtime-details">
        <summary>观测详情</summary>
        <p className="paper-runtime-note">首个阻断项不代表全部检查；具体订单仍须独立检查。</p>
        <dl>{observation.diagnostics.map(row => <div key={row.label}><dt>{row.label}</dt><dd>{row.value}</dd></div>)}</dl>
      </details>
    </div>
  </section>
}
