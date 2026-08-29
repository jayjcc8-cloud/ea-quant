import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './app'
// @ts-expect-error Vite resolves stylesheet assets at build time.
import './styles.css'

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>)
