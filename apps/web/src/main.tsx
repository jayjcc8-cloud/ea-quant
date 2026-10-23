import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './app'
import { startAppearance } from './appearance'
import { radianApi } from './radian-api'
import './styles.css'
import './workbench.css'

// The saved appearance is applied to the root element before the first paint, so a
// refresh comes back in the theme the user chose instead of flashing the default
// one. The runtime created here is the one the Settings panel writes through.
const appearance = startAppearance()
// The desktop's stable backend preference survives random-port WebView origins.
try {
  const status = await radianApi.desktopSettings()
  appearance.set({ ...appearance.get(), theme: status.theme })
} catch { /* Ordinary Web hosts keep the existing browser preference. */ }

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>)
