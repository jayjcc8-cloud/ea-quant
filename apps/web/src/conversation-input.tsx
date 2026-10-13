/** Shared controlled input for the research and read-only runtime conversations. */
export function ConversationInput({ id, label, value, onChange, placeholder, rows = 2, disabled = false }: {
  id: string; label: string; value: string; onChange: (value: string) => void
  placeholder: string; rows?: number; disabled?: boolean
}) {
  return <div className="field"><label htmlFor={id}>{label}</label>
    <textarea id={id} rows={rows} value={value} maxLength={4000} disabled={disabled}
      placeholder={placeholder} onChange={event => onChange(event.target.value)} />
  </div>
}
