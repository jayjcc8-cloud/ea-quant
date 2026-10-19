import { createContext, useContext, type ReactNode } from 'react'

export type WorkspaceViewName = 'work' | 'ai'
export type FeedPosition = { top: number; following: boolean }
export const WorkspaceView = createContext<{
  show: (view: WorkspaceViewName) => void
  positions: Map<string, FeedPosition>
}>({ show: () => {}, positions: new Map() })
export function useWorkspaceView() { return useContext(WorkspaceView).show }

/** Shell-owned navigation, rendered once by the active work surface. */
export const WorkspaceChrome = createContext<ReactNode>(null)
