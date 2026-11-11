import { spawnSync } from 'node:child_process'
import { copyFileSync, existsSync, mkdirSync, readFileSync, realpathSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { isDeepStrictEqual } from 'node:util'

const name = '@local/dsh-paper-desk'
const source = dirname(fileURLToPath(import.meta.url))
const json = file => JSON.parse(readFileSync(file, 'utf8').replace(/^\uFEFF/, ''))

function selected(manifest) {
  const bundles = manifest?.dsh?.profile?.bundles ?? []
  if (!Array.isArray(bundles) || bundles.some(id => typeof id !== 'string')) {
    throw new Error('Invalid profile bundle list.')
  }
  return bundles
}

async function install() {
  const [, , executable, home, profile, mode, previousPort] = process.argv
  if (!executable || !home || !profile || !/^[a-zA-Z0-9][a-zA-Z0-9_-]*$/.test(profile)) {
    throw new Error('Use install.ps1 with a valid DSH home and profile name.')
  }
  const dshHome = resolve(home)
  const profileDir = join(dshHome, 'profiles', profile)
  const manifestPath = join(profileDir, 'package.json')
  const desktopCli = join(dirname(executable), 'resources', 'app.asar', 'dsh', 'node_modules',
    '@deepseek-ai', 'dsh-desktop-host', 'lib', 'cli.js')
  const env = { ...process.env, DSH_HOME: dshHome, ELECTRON_RUN_AS_NODE: '1' }
  delete env.DSH_PROFILE_DIR
  delete env.DSH_PROFILE
  function cli(...args) {
    const result = spawnSync(executable, ['--expose-internals', desktopCli, ...args], {
      env, encoding: 'utf8', windowsHide: true, maxBuffer: 32 * 1024 * 1024
    })
    if (result.error) throw result.error
    if (result.status !== 0) throw new Error(`DSH exited ${result.status}: ${result.stderr || result.stdout}`)
    return result.stdout
  }

  const packageFile = join(source, 'package.json')
  const manifest = json(packageFile)
  if (manifest.name !== name || !manifest.dsh?.bundle?.patch || !manifest.dsh?.client) {
    throw new Error('The paper desk plugin manifest is incomplete.')
  }
  for (const file of [manifest.dsh.bundle.patch, ...manifest.files]) {
    if (!existsSync(join(source, file))) throw new Error('Missing plugin file: ' + file)
  }
  const sourceRequire = createRequire(packageFile)
  sourceRequire.resolve(name)
  sourceRequire.resolve(name + '/client')
  const runtime = cli('--version').trim()
  console.log(`DSH ${runtime} | ${profileDir}\nPlugin source: ${source}`)
  const before = existsSync(manifestPath) ? json(manifestPath) : null
  if (profile === 'desktop' && !before) throw new Error('Initialize this desktop profile in DSH first.')
  const previous = selected(before)
  if (mode === '--check') return
  const files = ['package.json', 'pnpm-lock.yaml', 'pnpm-workspace.yaml', 'cordis.patch.yml']
  const backup = join(dshHome, 'paper-desk', 'backups', profile + '-' + new Date().toISOString().replaceAll(':', '-') + '-' + process.pid)
  mkdirSync(backup, { recursive: true })
  for (const file of files) {
    if (existsSync(join(profileDir, file))) copyFileSync(join(profileDir, file), join(backup, file))
  }
  console.log(`Configuration backup: ${backup}`)

  // Use an absolute local link so the existing checkout remains the source of both code and data configuration.
  // DSH's own CLI coordinates package management and reconciliation with the desktop profile.
  cli('plugin', '--profile', profile, 'add', 'link:' + source.replaceAll('\\', '/'))
  const added = selected(json(manifestPath))
  const expected = before ? [...previous.filter(id => id !== name)] : added.filter(id => id !== name)
  if (!isDeepStrictEqual(added.filter(id => id !== name), expected)) {
    throw new Error('Unrelated enabled bundles changed. Inspect the configuration backup.')
  }
  if (!added.includes(name)) {
    cli('plugin', '--profile', profile, 'pkg', 'set', '--json', 'dsh.profile.bundles=' + JSON.stringify([...expected, name]))
  }
  const saved = json(manifestPath)
  const active = selected(saved)
  if (active.filter(id => id === name).length !== 1 ||
      !isDeepStrictEqual(active.filter(id => id !== name), expected) || !saved.dependencies?.[name]) {
    throw new Error('DSH failed to save the plugin dependency and enabled bundle.')
  }
  const loaded = createRequire(manifestPath)
  for (const [entry, target] of [[name, manifest.exports['.']], [name + '/client', manifest.exports['./client']]]) {
    if (realpathSync(loaded.resolve(entry)) !== realpathSync(join(source, target))) {
      throw new Error('DSH resolved a different plugin checkout: ' + entry)
    }
  }
  // Desktop composition is reserved for the running Electron host; its CLI permits
  // package management but rejects all --profile desktop boot/dump commands.
  if (profile !== 'desktop' && !cli('--profile', profile, '--dump-config').includes(name)) {
    throw new Error('The paper desk plugin is missing from the composed DSH configuration.')
  }
  if (mode === '--reload') {
    const { reloadPaperDesk } = await import('./reload.mjs')
    await reloadPaperDesk({ dshHome, profile, profileDir, source, name, previousPort, cli })
  } else console.log(`Installed and enabled ${name} in ${profile}. Reopen DSH to load the updated plugin.`)
}

try {
  await install()
} catch (error) {
  console.error(error.message)
  process.exitCode = 1
}
