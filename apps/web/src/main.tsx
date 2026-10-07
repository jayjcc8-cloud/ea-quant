import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './app'
import { startAppearance } from './appearance'
import './styles.css'

// The saved appearance is applied to the root element before the first paint, so a
// refresh comes back in the theme the user chose instead of flashing the default
// one. The runtime created here is the one the Settings panel writes through.
startAppearance()

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>)
