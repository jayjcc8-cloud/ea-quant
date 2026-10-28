/**
 * The manual draft editor: one controlled presentation of the fields the host
 * registered for the current draft revision.
 *
 * This file holds no state, reads nothing and decides nothing. Every value it
 * shows comes in through `values`, every keystroke leaves through `onChange`
 * with the raw string the user typed, and saving or giving up are the owner's
 * `onSave` and `onCancel`. In particular it does not parse, clamp, round, or
 * reject a value: an out-of-range or malformed number is the host's to refuse,
 * and a refusal arrives back here as `error`.
 *
 * The set of editable fields is the host's own: `kind`, `json_type`, the
 * registered bounds and `editable` are reported to the user as they were given.
 * A field the host does not allow is shown, named and explained rather than
 * hidden, so the list on screen is the whole list that was registered.
 *
 * The control is chosen from what the field is, not from what its value happens
 * to look like: an integer parameter gets a native number input that only the
 * browser's own stepper moves by ones, every other editable field gets a text
 * input that asks for a decimal keypad. Neither one converts the string on the
 * way out, so what is saved is what was typed.
 */
type DraftEditorField = {
  path: string
  kind: string
  json_type: string
  current: string | number
  minimum: string | number | null
  maximum: string | number | null
  editable: boolean
}

export type DraftEditorFormProps = {
  presentation?: 'studio'
  fields: DraftEditorField[]
  values: Record<string, string>
  revision: number
  busy: boolean
  disabled: boolean
  dirty: boolean
  error: string | null
  notice: string | null
  onChange: (path: string, value: string) => void
  onSave: () => void
  onCancel: () => void
}

/** The one field the user knows by its own name; every parameter keeps the exact
 * name it was registered under. */
const pathLabel = (path: string) => (path === 'initial_cash' ? '初始资金' : path)
const studioLabels: Record<string, string> = {
  initial_cash: '初始资金', 'parameters.target_quantity': '目标持仓数量',
  'parameters.entry_delay_bars': '入场等待（行情条数）',
  'parameters.fast_window': '快均线窗口', 'parameters.slow_window': '慢均线窗口',
}

/** The value an untouched field shows: its current one, as written. */
const currentText = (field: DraftEditorField) =>
  field.current === null || field.current === undefined ? '' : String(field.current)

/** A registered bound, as the host wrote it. A zero is a bound, not an absence. */
const boundText = (value: string | number | null) =>
  value === null || value === undefined ? null : String(value)

/** The bounds the field really carries, in the user's words. No bound is invented
 * for a field that declares none, and only a declared bound is stated. */
function boundNote(field: DraftEditorField): string {
  const minimum = boundText(field.minimum)
  const maximum = boundText(field.maximum)
  if (minimum !== null && maximum !== null) return `已登记范围：不小于 ${minimum}，不大于 ${maximum}`
  if (minimum !== null) return `已登记下限：不小于 ${minimum}`
  if (maximum !== null) return `已登记上限：不大于 ${maximum}`
  return field.path === 'initial_cash' ? '须为正数；金额精度由场景币种校验' : '该已注册字段没有登记范围'
}

export function DraftEditorForm({
  presentation,
  fields,
  values,
  revision,
  busy,
  disabled,
  dirty,
  error,
  notice,
  onChange,
  onSave,
  onCancel,
}: DraftEditorFormProps) {
  return <section aria-label="手工编辑草稿" className="draft-editor">
    {!presentation && <h2 className="draft-editor-title">参数与规则</h2>}
    <p className="muted draft-editor-revision" id="draft-editor-revision">
      {`基于草稿修订 ${revision}。保存仅生成新修订；实验需另行预览确认。`}
    </p>
    <div className="draft-editor-fields">
      {fields.map((field, index) => {
        // Every control is named after the field it edits, and stays distinct even
        // if the host ever reports the same path twice.
        const inputId = index === 0 ? `draft-field-${field.path}` : `draft-field-${field.path}-${index}`
        const boundsId = `${inputId}-bounds`
        const stateId = `${inputId}-state`
        const value = values[field.path] ?? currentText(field)
        const integer = field.json_type === 'integer'
        return <div className="draft-editor-field" key={inputId}>
          <label htmlFor={inputId}>
            <span className="draft-editor-path">{presentation ? studioLabels[field.path] ?? pathLabel(field.path) : pathLabel(field.path)}</span>
            {!presentation && <span className="draft-editor-kind">{`${field.kind} · ${field.json_type}`}</span>}
          </label>
          <input
            id={inputId}
            className="draft-editor-input"
            type={field.editable && integer ? 'number' : 'text'}
            inputMode={field.editable ? (integer ? 'numeric' : 'decimal') : undefined}
            step={field.editable && integer ? 1 : undefined}
            value={value}
            readOnly={!field.editable}
            disabled={busy || disabled}
            aria-describedby={field.editable ? boundsId : stateId}
            onChange={event => onChange(field.path, event.target.value)}
          />
          {field.editable
            ? <p className="muted draft-editor-bound" id={boundsId}>{boundNote(field)}</p>
            : <p className="muted draft-editor-bound" id={stateId}>
                该字段只读：{`${field.json_type}`} 类型在此不支持修改。它保留宿主登记的值。
              </p>}
        </div>
      })}
    </div>
    <details className="provenance draft-editor-contract">
      <summary>这里能改什么</summary>
      {presentation && <dl>{fields.map(field => <div key={field.path}><dt>{field.path}</dt><dd>{field.kind} · {field.json_type} · {field.editable ? '可编辑' : '只读'}</dd></div>)}</dl>}
      <p className="muted">
        可以调整的是初始资金与宿主已登记的可编辑参数。策略标识、标的、执行成本与风险模型是已注册场景
        自己的设置：它们在这里不能被自由修改，由场景合同提供，本表单不开放任意经济模型或新的策略语义。
      </p>
    </details>
    <div className="draft-editor-actions">
      <button type="button" className="primary" onClick={onSave}
        disabled={busy || disabled || !dirty}>保存草稿</button>
      <button type="button" onClick={onCancel}
        disabled={busy || disabled || !dirty}>取消编辑</button>
    </div>
    {error && <p className="notice error" role="alert">{error}</p>}
    {notice && <p className="notice" role="status">{notice}</p>}
  </section>
}
