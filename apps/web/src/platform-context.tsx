import { createContext, useContext } from 'react'
export type PlatformId = 'binance' | 'okx'
export const platformNames = { binance: 'Binance', okx: 'OKX' } as const
// Browser scope only. It is never copied into a research, dataset or frozen input.
export const PlatformContext = createContext<{ platform: PlatformId | null; select: (id: PlatformId | null) => void }>({ platform: null, select: () => {} })
export const usePlatform = () => useContext(PlatformContext)
export function savedPlatform(): PlatformId | null {
  try { const id = sessionStorage.getItem('quine.platform-filter'); return id === 'binance' || id === 'okx' ? id : null } catch { return null }
}
