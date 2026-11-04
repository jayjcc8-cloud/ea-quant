import { useCallback, useEffect, useState, type SetStateAction } from 'react'

export const reason = (error: unknown) => error instanceof Error ? error.message : '读取失败'
export function saved(key: string): string { try { return sessionStorage.getItem(key) ?? '' } catch { return '' } }

export function useRead<T>(read: () => Promise<T>, refreshKey = '') {
  const [version, setVersion] = useState(0)
  const [loaded, setLoaded] = useState<{ read: typeof read; version: number; refreshKey: string; value: T | null; error: string } | null>(null)
  useEffect(() => {
    let active = true
    void read().then(value => { if (active) setLoaded({ read, version, refreshKey, value, error: '' }) }).catch(e => { if (active) setLoaded({ read, version, refreshKey, value: null, error: reason(e) }) })
    return () => { active = false }
  }, [read, version, refreshKey])
  const setValue = useCallback((next: SetStateAction<T | null>) => setLoaded(current => ({ read, version, refreshKey, error: '', value: typeof next === 'function' ? (next as (current: T | null) => T | null)(current?.read === read && current.version === version && current.refreshKey === refreshKey ? current.value : null) : next })), [read, version, refreshKey])
  const current = loaded?.read === read && loaded.version === version && loaded.refreshKey === refreshKey ? loaded : null
  return { value: current?.value ?? null, setValue, error: current?.error ?? '', reload: () => setVersion(n => n + 1) }
}
