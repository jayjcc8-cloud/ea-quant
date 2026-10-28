const paths = {
  idea: 'M9 18h6M10 21h4M8 14a6 6 0 1 1 8 0c-1 1-1 2-1 4H9c0-2 0-3-1-4Z',
  draft: 'M6 3h8l4 4v14H6zM14 3v5h4M9 12h6M9 16h6',
  experiment: 'M9 3h6M10 3v7L4 19a1 1 0 0 0 1 2h14a1 1 0 0 0 1-2l-6-9V3M8 15h8',
  evidence: 'M5 3h10l4 4v14H5zM14 3v5h5M8 13l2 2 5-5M8 18h8',
  search: 'M16 16l5 5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z',
  layers: 'm12 3 10 5-10 5L2 8Zm-10 9 10 5 10-5M2 16l10 5 10-5',
  spark: 'M12 2c0 6-4 10-10 10 6 0 10 4 10 10 0-6 4-10 10-10-6 0-10-4-10-10Z',
  arrow: 'M4 12h16m-6-6 6 6-6 6',
  panel: 'M3 4h18v16H3zM9 4v16',
} as const

/** Presentation-only outline icons; the surrounding control supplies its name. */
export function WorkbenchIcon({ name }: { name: keyof typeof paths }) {
  return <svg className="workbench-icon" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false"><path d={paths[name]} /></svg>
}
