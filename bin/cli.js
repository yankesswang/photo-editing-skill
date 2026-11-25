#!/usr/bin/env node
'use strict';

// photo-editing-skill installer / runner.
//   npx photo-editing-skill                  install into Claude Code (~/.claude/skills/photo-edit)
//   npx photo-editing-skill install --codex  install into Codex (~/.codex/skills/photo-edit)
//   npx photo-editing-skill pe doctor        run the engine via `uv run scripts/pe.py doctor`

const fs = require('fs');
const os = require('os');
const path = require('path');
const childProcess = require('child_process');

const PKG_ROOT = path.resolve(__dirname, '..');
const PKG = require(path.join(PKG_ROOT, 'package.json'));
const SKILL_NAME = 'photo-edit';
const PAYLOAD = ['SKILL.md', 'scripts', 'styles', 'references'];
const MARKER = '.photo-editing-skill.json';

const HOME = os.homedir();
const TARGETS = {
  claude: { label: 'Claude Code', dir: path.join(HOME, '.claude', 'skills', SKILL_NAME) },
  codex: { label: 'Codex', dir: path.join(HOME, '.codex', 'skills', SKILL_NAME) },
  gemini: { label: 'Gemini CLI', dir: path.join(HOME, '.gemini', 'skills', SKILL_NAME) },
};

const HELP = `photo-editing-skill v${PKG.version}

Usage:
  npx photo-editing-skill [install] [targets] [--force]
  npx photo-editing-skill uninstall [targets]
  npx photo-editing-skill pe <pe.py args...>      run the engine with uv (e.g. "pe doctor")
  npx photo-editing-skill path                    print the bundled skill folder

Targets (default: --claude):
  --claude          ~/.claude/skills/${SKILL_NAME}
  --codex           ~/.codex/skills/${SKILL_NAME}
  --gemini          ~/.gemini/skills/${SKILL_NAME}
  --all             all of the above
  --project         ./.claude/skills/${SKILL_NAME} (current project only)
  --dir <path>      install the skill folder at <path>

Options:
  --force           overwrite a folder that was not installed by this tool
  -h, --help        show this help
  -v, --version     show version
`;

function fail(msg) {
  process.stderr.write(`error: ${msg}\n`);
  process.exit(1);
}

function copyRecursive(src, dest) {
  const st = fs.statSync(src);
  if (st.isDirectory()) {
    fs.mkdirSync(dest, { recursive: true });
    for (const name of fs.readdirSync(src)) {
      if (name === '__pycache__' || name === '.DS_Store' || name.endsWith('.pyc')) continue;
      copyRecursive(path.join(src, name), path.join(dest, name));
    }
  } else {
    fs.copyFileSync(src, dest);
    fs.chmodSync(dest, st.mode);
  }
}

function removeRecursive(p) {
  if (!fs.existsSync(p)) return;
  if (fs.rmSync) fs.rmSync(p, { recursive: true, force: true });
  else fs.rmdirSync(p, { recursive: true });
}

function parseTargets(args) {
  const out = [];
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === '--all') Object.keys(TARGETS).forEach((k) => out.push(TARGETS[k]));
    else if (a === '--claude' || a === '--codex' || a === '--gemini') out.push(TARGETS[a.slice(2)]);
    else if (a === '--project') {
      out.push({ label: 'Claude Code (project)', dir: path.resolve('.claude', 'skills', SKILL_NAME) });
    } else if (a === '--dir') {
      const d = args[++i];
      if (!d) fail('--dir needs a path');
      out.push({ label: 'custom', dir: path.resolve(d.replace(/^~(?=$|[\\/])/, HOME)) });
    } else if (a === '--force') {
      // handled by caller
    } else fail(`unknown option: ${a}\n\n${HELP}`);
  }
  if (!out.length) out.push(TARGETS.claude);
  return out;
}

function isOurs(dir) {
  return fs.existsSync(path.join(dir, MARKER));
}

function install(args) {
  const force = args.includes('--force');
  for (const t of parseTargets(args)) {
    if (fs.existsSync(t.dir)) {
      if (!isOurs(t.dir) && !force) {
        fail(`${t.dir} already exists and was not installed by photo-editing-skill. Re-run with --force to replace it.`);
      }
      removeRecursive(t.dir);
    }
    fs.mkdirSync(t.dir, { recursive: true });
    for (const item of PAYLOAD) copyRecursive(path.join(PKG_ROOT, item), path.join(t.dir, item));
    fs.writeFileSync(
      path.join(t.dir, MARKER),
      JSON.stringify({ package: PKG.name, version: PKG.version, installedAt: new Date().toISOString() }, null, 2) + '\n'
    );
    process.stdout.write(`✓ Installed ${SKILL_NAME} v${PKG.version} for ${t.label}: ${t.dir}\n`);
  }
  checkUv();
  process.stdout.write(
    '\nNext: ask your assistant, e.g. "Use the photo-edit skill on my photos folder: cull the best shots and show me a style board."\n'
  );
}

function uninstall(args) {
  for (const t of parseTargets(args)) {
    if (!fs.existsSync(t.dir)) {
      process.stdout.write(`- Not installed for ${t.label}: ${t.dir}\n`);
      continue;
    }
    if (!isOurs(t.dir) && !args.includes('--force')) {
      fail(`${t.dir} was not installed by photo-editing-skill. Re-run with --force to remove it anyway.`);
    }
    removeRecursive(t.dir);
    process.stdout.write(`✓ Removed ${t.dir}\n`);
  }
}

function hasUv() {
  const r = childProcess.spawnSync('uv', ['--version'], { stdio: 'ignore', shell: process.platform === 'win32' });
  return r.status === 0;
}

function checkUv() {
  if (hasUv()) return;
  process.stdout.write(
    '\n! uv was not found. The engine needs Python 3.9+ and uv:\n' +
      '    macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh\n' +
      '    Windows:     powershell -c "irm https://astral.sh/uv/install.ps1 | iex"\n'
  );
}

function runPe(args) {
  if (!hasUv()) {
    checkUv();
    process.exit(1);
  }
  // Pass leading --with options through to uv, everything else to pe.py.
  const uvArgs = ['run'];
  let i = 0;
  while (i < args.length && args[i] === '--with' && args[i + 1]) {
    uvArgs.push('--with', args[i + 1]);
    i += 2;
  }
  uvArgs.push(path.join(PKG_ROOT, 'scripts', 'pe.py'));
  const r = childProcess.spawnSync('uv', uvArgs.concat(args.slice(i)), {
    stdio: 'inherit',
    shell: process.platform === 'win32',
  });
  process.exit(r.status === null ? 1 : r.status);
}

function main() {
  const argv = process.argv.slice(2);
  const cmd = argv[0] && !argv[0].startsWith('-') ? argv[0] : 'install';
  const rest = argv[0] && !argv[0].startsWith('-') ? argv.slice(1) : argv;

  if (argv.includes('-h') || argv.includes('--help')) {
    if (cmd !== 'pe') return process.stdout.write(HELP);
  }
  if (argv[0] === '-v' || argv[0] === '--version') return process.stdout.write(PKG.version + '\n');

  switch (cmd) {
    case 'install':
      return install(rest);
    case 'uninstall':
    case 'remove':
      return uninstall(rest);
    case 'pe':
    case 'run':
      return runPe(rest);
    case 'path':
      return process.stdout.write(PKG_ROOT + '\n');
    case 'help':
      return process.stdout.write(HELP);
    default:
      fail(`unknown command: ${cmd}\n\n${HELP}`);
  }
}

main();
