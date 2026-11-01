import { existsSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

function newestExistingFile(paths: string[]): string | undefined {
  return paths
    .filter(path => existsSync(path))
    .map(path => ({ path, modifiedAt: statSync(path).mtimeMs }))
    .sort((left, right) => right.modifiedAt - left.modifiedAt)[0]?.path
}

/** Resolve Codex without assuming that Electron inherited Codex Desktop's PATH. */
export function resolveCodexCommand(
  env: NodeJS.ProcessEnv = process.env,
  platform = process.platform,
): string {
  const override = env.CODEX_COMMAND?.trim()
  if (override) return override

  if (platform === 'win32') {
    const desktopBinRoot = env.LOCALAPPDATA
      ? join(env.LOCALAPPDATA, 'OpenAI', 'Codex', 'bin')
      : null
    if (desktopBinRoot && existsSync(desktopBinRoot)) {
      const desktopCandidates = readdirSync(desktopBinRoot, { withFileTypes: true })
        .filter(entry => entry.isDirectory())
        .map(entry => join(desktopBinRoot, entry.name, 'codex.exe'))
      const desktopCommand = newestExistingFile(desktopCandidates)
      if (desktopCommand) return desktopCommand
    }

    const npmCommand = env.APPDATA ? join(env.APPDATA, 'npm', 'codex.cmd') : null
    if (npmCommand && existsSync(npmCommand)) return npmCommand
  }

  return 'codex'
}

function quoteWindowsArgument(value: string): string {
  if (!/[\s"]/u.test(value)) return value
  return `"${value.replace(/(\\*)"/g, '$1$1\\"').replace(/(\\+)$/g, '$1$1')}"`
}

export function getCodexSpawn(
  command: string,
  args: string[],
  platform = process.platform,
  env: NodeJS.ProcessEnv = process.env,
): { command: string; args: string[] } {
  if (platform === 'win32' && /\.(?:cmd|bat)$/iu.test(command)) {
    return {
      command: env.ComSpec || 'cmd.exe',
      args: ['/d', '/s', '/c', [command, ...args].map(quoteWindowsArgument).join(' ')],
    }
  }
  return { command, args }
}
