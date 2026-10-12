/** Server ownership shared only by this isolated installed-browser harness. */
import { spawn, execFileSync, type ChildProcess } from 'node:child_process'
import { readFileSync, writeFileSync } from 'node:fs'

function pidFile(): string {
  const file = process.env.EA_WEB_SERVER_PID_FILE
  if (!file) throw new Error('server replacement requires the isolated installed harness')
  return file
}

export function installedServerPid(): number | null {
  const value = readFileSync(pidFile(), 'utf8').trim()
  if (!value) return null
  const pid = Number(value)
  if (!Number.isSafeInteger(pid) || pid <= 1) throw new Error('invalid installed-server PID')
  return pid
}

export function spawnInstalledServer(executable: string, args: string[], env = process.env): ChildProcess {
  if (installedServerPid() !== null) {
    const old = installedServerPid()!
    try {
      process.kill(old, 0)
      throw new Error('the previous installed server must exit before replacement')
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ESRCH') throw error
    }
  }
  const server = spawn(executable, args, { stdio: 'ignore', env })
  if (!server.pid) throw new Error('installed server did not start')
  writeFileSync(pidFile(), String(server.pid))
  return server
}

async function stopCurrentServer(): Promise<void> {
  const pid = installedServerPid()
  if (pid === null) return
  try {
    process.kill(pid, 0)
    const command = execFileSync('ps', ['-p', String(pid), '-o', 'command='], { encoding: 'utf8' })
    if (!process.env.EA_WEB_BIN || !command.includes(process.env.EA_WEB_BIN)) {
      throw new Error('refusing to stop a process outside this installed fixture')
    }
    process.kill(pid, 'SIGTERM')
    for (let attempt = 0; attempt < 200; attempt += 1) {
      try { process.kill(pid, 0) } catch (error) {
        if ((error as NodeJS.ErrnoException).code === 'ESRCH') {
          writeFileSync(pidFile(), '')
          return
        }
        throw error
      }
      await new Promise(resolve => setTimeout(resolve, 50))
    }
    throw new Error('installed fixture server did not exit')
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ESRCH') throw error
    writeFileSync(pidFile(), '')
  }
}

/** Restore a model-free server between tests, including after a failed restart. */
export async function resetInstalledServer(): Promise<void> {
  await stopCurrentServer()
  const server = spawnInstalledServer(process.env.EA_WEB_BIN!, JSON.parse(process.env.EA_WEB_ARGS!))
  for (let attempt = 0; attempt < 200; attempt += 1) {
    if (server.exitCode !== null || server.signalCode !== null) throw new Error('replacement server exited before readiness')
    try {
      if ((await fetch(`${process.env.EA_WEB_BASE_URL}/api/health`)).ok) return
    } catch { /* starting */ }
    await new Promise(resolve => setTimeout(resolve, 50))
  }
  throw new Error('replacement server did not become ready')
}
