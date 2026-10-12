import fs from 'node:fs/promises';
import path from 'node:path';
import { spawn } from 'node:child_process';

/**
 * 跑子进程并等它结束。
 *
 * 这里刻意不用 `execFileSync`：同步派生进程在部分受控/沙箱环境里会直接失败
 * （Node 报 `spawnSync … EBUSY`），而异步 `spawn` 不受影响。`stdio: 'inherit'`
 * 时同步返回空输出，与原来 execFileSync 的 stdio 透传行为一致。
 */
function run(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, options);
    let stdout = '';
    let stderr = '';
    child.stdout?.on('data', (chunk) => { stdout += chunk; });
    child.stderr?.on('data', (chunk) => { stderr += chunk; });
    child.on('error', reject);
    child.on('exit', (code, signal) => {
      if (code === 0) {
        resolve({ stdout, stderr });
        return;
      }
      reject(new Error(`\`${command} ${args.join(' ')}\` 退出码 ${code ?? signal}\n${stderr}`.trim()));
    });
  });
}

const root = path.resolve(import.meta.dirname, '..');
const companion = path.join(root, 'vendor/agent-companion');
const output = path.join(companion, 'dist-embed');
const mode = process.argv[2];

if (!['dev', 'build', 'copy'].includes(mode)) {
  throw new Error('Usage: node scripts/prepare-agent-companion.mjs <dev|build|copy>');
}

const version = JSON.parse(await fs.readFile(path.join(root, 'vendor/agent-companion.version.json'), 'utf8'));
if (!/^[0-9a-f]{40}$/.test(version.revision) || version.interfaceVersion !== 1) {
  throw new Error('Invalid pinned Agent Companion version manifest');
}
console.log(`Agent Companion source: ${version.revision}, host interface: ${version.interfaceVersion}`);

const target = process.env.AGENT_COMPANION_TARGET
  || (await run('rustc', ['-vV'])).stdout.match(/^host: (.+)$/m)?.[1];
if (!target) throw new Error('Could not determine Rust target; set AGENT_COMPANION_TARGET');
const suffix = target.includes('windows') ? '.exe' : '';

if (mode !== 'copy') {
  // The archived source carries its own lockfiles, keeping the plugin, UI and
  // runtime on one revision. A clean checkout has no installed dependencies.
  // Windows 的 npm 是 .cmd，Node 20+ 禁止直接 spawn（EINVAL），必须经 shell 解析。
  //
  // 这里用 `npm install` 而非 `npm ci`：`ci` 会先整体清空 node_modules，
  // 在受限/沙箱环境里这一步会被安全删除守卫拦下，而且它的回滚路径本身就走
  // 同步 spawn（本机会 EBUSY）。`install` 是幂等的增量修补，两种环境下都能过。
  const shell = process.platform === 'win32';
  await run('npm', ['install', '--no-audit', '--no-fund'], { cwd: companion, stdio: 'inherit', shell });
  await run('npm', ['run', 'build:embed'], { cwd: companion, stdio: 'inherit', shell });
  await run('cargo', [
    'build', '--release', '--locked', '--manifest-path', path.join(companion, 'Cargo.toml'),
    '-p', 'agent-studio-runtime', '--target', target,
  ], { cwd: root, stdio: 'inherit', env: { ...process.env, CARGO_TARGET_DIR: path.join(root, 'target') } });
  const binaries = path.join(root, 'src-tauri/binaries');
  await fs.mkdir(binaries, { recursive: true });
  await fs.copyFile(
    path.join(root, 'target', target, 'release', `agent-studio-runtime${suffix}`),
    path.join(binaries, `agent-studio-runtime-${target}${suffix}`),
  );
}

for (const page of ['desktop.html', 'desktop-settings.html']) {
  await fs.access(path.join(output, page));
}
if (mode === 'copy' || mode === 'dev') {
  const destination = mode === 'dev'
    ? path.join(root, 'public/companion')
    : path.join(root, 'dist/companion');
  await fs.rm(destination, { recursive: true, force: true });
  await fs.mkdir(path.dirname(destination), { recursive: true });
  await fs.cp(output, destination, { recursive: true });
  console.log(`Agent Companion assets copied to ${path.relative(root, destination)}/`);
  if (mode === 'copy') {
    // 拷完立刻校验落地结果。桌面端悬浮窗读的是 `companion/desktop.html`，
    // 少了这一步就是「dist 里没有 companion/」，但它不会让任何构建命令失败——
    // 只会让运行时的悬浮窗变成一片空白，而空白页从不上报命中区域，
    // 于是整窗永久点击穿透、完全点不到，且没有任何报错。
    // 在打包之前把它喊出来，胜过在用户面前静默坏掉。
    //
    // 注意本文件用的是 `node:fs/promises`，没有 `existsSync` / `readdirSync`，
    // 想判存在只能用 `access()` 兜异常。
    const exists = async (target) => {
      try {
        await fs.access(target);
        return true;
      } catch {
        return false;
      }
    };
    for (const page of ['desktop.html', 'desktop-settings.html']) {
      const landed = path.join(destination, page);
      if (!(await exists(landed))) {
        throw new Error(
          `Agent Companion 资源未落地：缺少 ${path.relative(root, landed)}\n` +
          `悬浮窗会因页面空白而整块点击穿透。请确认 vite 的 outDir 是 dist，并重新执行构建。`,
        );
      }
    }
    const assets = path.join(destination, 'assets');
    const entries = (await exists(assets)) ? await fs.readdir(assets) : [];
    if (!entries.some(name => name.endsWith('.js'))) {
      throw new Error(`Agent Companion 资源未落地：${path.relative(root, assets)} 里没有脚本，悬浮窗无法启动。`);
    }
    console.log(`Agent Companion assets verified in ${path.relative(root, destination)}/ (${entries.length} assets)`);
  }
}
