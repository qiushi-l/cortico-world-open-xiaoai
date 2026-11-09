import { spawn, type ChildProcess } from 'node:child_process';
import { createWriteStream, existsSync, mkdirSync, type WriteStream } from 'node:fs';
import { readFile, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { delimiter, dirname, join, resolve } from 'node:path';
import { connect } from 'node:net';
import { fileURLToPath } from 'node:url';
import type { Logger } from 'cortico/core/types.ts';
import {
  type OpenXiaoaiConfigSection,
  normalizeInterruptMode,
  parseWakeWords,
} from './config.ts';

/**
 * 扩展包根目录：从本文件的 URL 往上两级（src/bridge.ts → 包根）。
 *
 * 不能依赖装配层给的 `packageDir`——实测那是**部署目录**，不是扩展包目录，
 * 照着找 `bridge/server.py` 必然找不到。
 */
export const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

/** 管理口单次请求的超时：桥接里最慢的是 run_shell（设备上跑脚本）。 */
const ADMIN_TIMEOUT_MS = 15_000;
/** 启动后等管理口可用；到点先让 Coo 继续启动，后台继续等。 */
const ADMIN_READY_TIMEOUT_MS = 12_000;
/** 自动重启的最小间隔，防止配置写错时疯狂重启。 */
const RESTART_BACKOFF_MS = 5_000;
const MAX_LOG_LINES = 400;

export interface BridgeStatus {
  ok: boolean;
  devices: string[];
  llm: string;
  model: string;
  speaking: boolean;
  brain: string;
  wake_without_asr: number;
  /** 进程层面的补充信息（管理口给不了的部分）。 */
  running: boolean;
  pid: number | null;
}

/**
 * 桥接进程的看护者。
 *
 * 只做三件事：按配置起 `server.py`、把配置变化落盘并重启、往管理口发一行 JSON 问状态。
 * **不做**设备通信——那是 Python 那边的活。
 *
 * 构造时不碰任何外部资源（装配层会为未启用的 World 也构造一次）。
 */
export class BridgeProcess {
  private child: ChildProcess | null = null;
  private logStream: WriteStream | null = null;
  private readonly log: Logger;
  private readonly packageDir: string;
  private readonly dataDir: string;
  private readonly cfg: OpenXiaoaiConfigSection;
  private writtenCfg = '';
  private lastStartAt = 0;
  private lastStatus: BridgeStatus | null = null;
  private stopping = false;
  private tail: string[] = [];
  private onExitCb: ((code: number | null) => void) | null = null;

  constructor(opts: {
    cfg: OpenXiaoaiConfigSection;
    dataDir: string;
    log: Logger;
  }) {
    this.cfg = opts.cfg;
    this.packageDir = PACKAGE_ROOT;
    this.dataDir = opts.dataDir;
    this.log = opts.log;
  }

  onExit(cb: (code: number | null) => void): void {
    this.onExitCb = cb;
  }

  get running(): boolean {
    return this.child !== null && this.child.exitCode === null && !this.stopping;
  }

  get pid(): number | null {
    return this.running ? (this.child?.pid ?? null) : null;
  }

  private get serverPy(): string {
    return join(this.packageDir, 'bridge', 'server.py');
  }

  private get stateDir(): string {
    // 状态落在部署目录，不进扩展包：升级包会整目录替换。
    return join(this.dataDir, 'open-xiaoai');
  }

  private get configFile(): string {
    return join(this.stateDir, 'bridge.json');
  }

  private get logDir(): string {
    return join(this.stateDir, 'logs');
  }

  /** 今天这轮桥接日志；读不到时返回空串。 */
  async readLog(lines = 120): Promise<string> {
    // 优先读 Python 自己写的日志；它没起来时退回 stdout 备份，否则面板会一片空白。
    for (const suffix of ['.log', '.out.log']) {
      try {
        const text = await readFile(join(this.logDir, `bridge-${stampDay()}${suffix}`), 'utf8');
        const trimmed = text.trim();
        if (trimmed !== '') return trimmed.split(/\r?\n/).slice(-lines).join('\n');
      } catch {
        // 换下一个
      }
    }
    return '';
  }

  /** 进程自己打印的尾巴（管理口不通时用来判断为什么起不来）。 */
  tailText(lines = 40): string {
    return this.tail.slice(-lines).join('\n');
  }

  /** 找 Python：配置 > PATH > DSH 内置运行时。 */
  private resolvePython(): string | null {
    const configured = this.cfg.pythonPath?.trim();
    if (configured && existsSync(configured)) return configured;

    const exts = process.platform === 'win32' ? ['.exe', ''] : [''];
    const dirs = (process.env.PATH ?? '').split(delimiter).filter(Boolean);
    for (const name of ['python', 'python3']) {
      for (const dir of dirs) {
        for (const ext of exts) {
          const candidate = join(dir, name + ext);
          if (existsSync(candidate)) return candidate;
        }
      }
    }

    // 最后兜底：DSH 自带的那份（跨机器不一定在，仅当存在时用）。
    const dsh = join(
      homedir(),
      '.dsh',
      'dsh-runtimes',
      'dsh-primary-runtime',
      'dependencies',
      'python',
      process.platform === 'win32' ? 'python.exe' : 'bin/python3',
    );
    return existsSync(dsh) ? dsh : null;
  }

  /** 把 World 的配置翻译成桥接认的 config.json 文本。 */
  private buildBridgeConfig(example: string): string {
    let base: Record<string, unknown>;
    try {
      base = JSON.parse(example) as Record<string, unknown>;
    } catch {
      base = {};
    }
    const server = (base.server as Record<string, unknown>) ?? {};
    server.host = '0.0.0.0';
    server.port = this.cfg.bridgePort;
    server.admin_port = this.cfg.adminPort;
    base.server = server;

    const voice = (base.voice as Record<string, unknown>) ?? {};
    const wake = parseWakeWords(this.cfg.wakeWords);
    if (wake.length > 0) voice.wake_words = wake;
    voice.followup_s = this.cfg.followupSeconds;
    voice.reopen_after_speak = this.cfg.reopenAfterSpeak;
    voice.interrupt_mode = normalizeInterruptMode(this.cfg.interruptMode);
    base.voice = voice;

    return JSON.stringify(base, null, 2);
  }

  private ensureDirs(): void {
    mkdirSync(this.logDir, { recursive: true });
  }

  async start(): Promise<void> {
    if (this.running) return;
    this.stopping = false;

    const now = Date.now();
    if (now - this.lastStartAt < RESTART_BACKOFF_MS) {
      this.log.warn('桥接重启太频繁，稍后再说', { sinceMs: now - this.lastStartAt });
      return;
    }
    this.lastStartAt = now;

    const python = this.resolvePython();
    if (!python) {
      this.log.error('找不到 Python，桥接起不来；请在配置里填 Python 路径');
      return;
    }
    if (!existsSync(this.serverPy)) {
      this.log.error('扩展包内缺少 bridge/server.py', { path: this.serverPy });
      return;
    }

    this.ensureDirs();

    let example = '{}';
    try {
      example = await readFile(join(this.packageDir, 'bridge', 'config.example.json'), 'utf8');
    } catch {
      this.log.warn('没读到 config.example.json，按最小配置起桥接');
    }
    const text = this.buildBridgeConfig(example);
    if (text !== this.writtenCfg) {
      await writeFile(this.configFile, text, 'utf8');
      this.writtenCfg = text;
    }

    const args = [
      this.serverPy,
      '--config',
      this.configFile,
      '--log-dir',
      this.logDir,
      '--port',
      String(this.cfg.bridgePort),
      ...this.cfg.extraArgs.split(/\s+/).filter(Boolean),
    ];

    this.log.info('启动桥接', { python, port: this.cfg.bridgePort });
    this.tail = [];
    // Python 自己会写 bridge-<day>.log（--log-dir），这里把 stdout/stderr 另存一份，
    // 只用来兜住「Python 日志还没起来就崩了」以及未捕获回溯，避免同一条日志写两遍同一个文件。
    this.logStream = createWriteStream(join(this.logDir, `bridge-${stampDay()}.out.log`), {
      flags: 'a',
    });
    const child = spawn(python, args, {
      cwd: join(this.packageDir, 'bridge'),
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });
    this.child = child;

    const sink = (buf: Buffer): void => {
      const textLine = buf.toString('utf8');
      this.logStream?.write(textLine);
      for (const line of textLine.split(/\r?\n/)) {
        if (line.trim() !== '') this.tail.push(line);
      }
      if (this.tail.length > MAX_LOG_LINES) this.tail.splice(0, this.tail.length - MAX_LOG_LINES);
    };
    child.stdout?.on('data', sink);
    child.stderr?.on('data', sink);

    child.on('error', (err) => {
      this.log.error('桥接进程起不来', { error: String(err) });
    });
    child.on('exit', (code) => {
      this.logStream?.end();
      this.logStream = null;
      this.child = null;
      if (this.stopping) {
        this.log.info('桥接已停止', { code });
      } else {
        this.log.warn('桥接进程退出', { code });
      }
      this.onExitCb?.(code);
    });

    await this.waitAdmin(ADMIN_READY_TIMEOUT_MS);
  }

  /** 等管理口能应答；超时只记日志，后台继续跑。 */
  private async waitAdmin(timeoutMs: number): Promise<void> {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const status = await this.status().catch(() => null);
      if (status) {
        this.lastStatus = status;
        this.log.info('桥接管理口已就绪', { devices: status.devices.length });
        return;
      }
      await sleep(400);
    }
    this.log.warn('桥接启动超时，先继续，后台还在跑', { timeoutMs });
  }

  async stop(): Promise<void> {
    const child = this.child;
    this.stopping = true;
    if (!child) return;
    try {
      // Windows 上 python 可能再拉子进程，整棵树一起收。
      if (process.platform === 'win32' && child.pid) {
        spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], {
          stdio: 'ignore',
          windowsHide: true,
        });
      } else {
        child.kill('SIGTERM');
      }
    } catch (err) {
      this.log.warn('停止桥接时出错', { error: String(err) });
    }
    await sleep(600);
    this.child = null;
  }

  /** 配置变了就重启；没变什么都不做。 */
  async syncConfig(): Promise<boolean> {
    const examplePath = join(this.packageDir, 'bridge', 'config.example.json');
    let example = '{}';
    try {
      example = await readFile(examplePath, 'utf8');
    } catch {
      return false;
    }
    const text = this.buildBridgeConfig(example);
    if (text === this.writtenCfg) return false;
    this.log.info('桥接配置变了，重启生效');
    await this.stop();
    this.lastStartAt = 0; // 这是配置驱动的重启，不受退避限制
    await this.start();
    return true;
  }

  /** 往管理口发一行 JSON，读回一行 JSON。 */
  async request(payload: Record<string, unknown>, timeoutMs = ADMIN_TIMEOUT_MS): Promise<unknown> {
    return new Promise((resolve, reject) => {
      const socket = connect({ host: '127.0.0.1', port: this.cfg.adminPort });
      let buf = '';
      let settled = false;
      const done = (err: Error | null, value?: unknown): void => {
        if (settled) return;
        settled = true;
        socket.destroy();
        if (err) reject(err);
        else resolve(value);
      };
      const timer = setTimeout(() => done(new Error('管理口请求超时')), timeoutMs);
      socket.setEncoding('utf8');
      socket.on('connect', () => {
        socket.write(JSON.stringify(payload) + '\n');
      });
      socket.on('data', (chunk: string) => {
        buf += chunk;
        const nl = buf.indexOf('\n');
        if (nl < 0) return;
        clearTimeout(timer);
        try {
          done(null, JSON.parse(buf.slice(0, nl)));
        } catch (err) {
          done(new Error(`管理口回了非 JSON：${String(err)}`));
        }
      });
      socket.on('error', (err) => {
        clearTimeout(timer);
        done(err instanceof Error ? err : new Error(String(err)));
      });
      socket.on('close', () => {
        clearTimeout(timer);
        if (buf.trim() === '') done(new Error('管理口断开且没有回包'));
      });
    });
  }

  async status(): Promise<BridgeStatus | null> {
    try {
      const raw = (await this.request({ status: true }, 3000)) as Record<string, unknown>;
      const status: BridgeStatus = {
        ok: raw?.ok === true,
        devices: Array.isArray(raw?.devices) ? (raw.devices as string[]) : [],
        llm: String(raw?.llm ?? ''),
        model: String(raw?.model ?? ''),
        speaking: raw?.speaking === true,
        brain: String(raw?.brain ?? ''),
        wake_without_asr: Number(raw?.wake_without_asr ?? 0),
        running: this.running,
        pid: this.pid,
      };
      this.lastStatus = status;
      return status;
    } catch {
      return null;
    }
  }

  /** 状态快照（管理口不通时用上次的 + 进程信息）。 */
  snapshot(): BridgeStatus {
    const base = this.lastStatus ?? {
      ok: false,
      devices: [],
      llm: '',
      model: '',
      speaking: false,
      brain: '',
      wake_without_asr: 0,
      running: false,
      pid: null,
    };
    return { ...base, running: this.running, pid: this.pid };
  }

  /** 管理口的一条指令（speak / run_shell / ask / recover）。 */
  async command(payload: Record<string, unknown>, timeoutMs?: number): Promise<unknown> {
    return this.request(payload, timeoutMs);
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function stampDay(d = new Date()): string {
  const p = (n: number): string => String(n).padStart(2, '0');
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}`;
}
