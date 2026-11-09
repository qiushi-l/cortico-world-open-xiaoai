import { fileURLToPath } from 'node:url';
import { nowIso } from 'cortico/core/util.ts';
import type {
  EventEnvelope,
  ToolDef,
  World,
  WorldHost,
  WorldWebUiDecl,
} from 'cortico/core/types.ts';
import { BridgeProcess, type BridgeStatus } from './bridge.ts';
import {
  OPEN_XIAOAI_CONFIG_GROUP,
  type OpenXiaoaiConfigSection,
  normalizeInterruptMode,
  parseWakeWords,
} from './config.ts';

/** 状态轮询间隔；只读管理口，开销很小。 */
const POLL_MS = 8_000;
/** 连续这么久连不上管理口，就推一条事件告诉模型「音箱这条路断了」。 */
const OFFLINE_NOTIFY_MS = 60_000;

/** 工具名与声明共用一份，`describe` 与 `tools()` 不会漂移。 */
export const OPEN_XIAOAI_TOOL_DECLS: ReadonlyArray<Omit<ToolDef, 'handler'>> = [
  {
    name: 'xiaoai_speak',
    description:
      '让音箱念一句话（用音箱自己的 TTS，不需要经过语音唤醒）。适合主动提醒、报时、把结果读出来。',
    parameters: {
      type: 'object',
      properties: {
        text: {
          type: 'string',
          description: '要念出来的文字。写口语，能直接朗读，别用 markdown 或表情。',
        },
      },
      required: ['text'],
    },
    tags: ['speak'],
  },
  {
    name: 'xiaoai_status',
    description:
      '查音箱这条线路的现状：桥接进程是否在跑、音箱是否连上来、当前用哪套脑子、有没有 ASR 异常计数。',
    parameters: { type: 'object', properties: {} },
    tags: ['read'],
  },
  {
    name: 'xiaoai_shell',
    description:
      '在音箱上执行一条 shell 命令并取回输出。只在排障时用，普通对话不要调；命令是只读排查（ls、cat、ubus call …）最安全。',
    parameters: {
      type: 'object',
      properties: {
        command: { type: 'string', description: '要在音箱上执行的命令，例如 `ubus call microphone status {}`。' },
        timeout_s: { type: 'integer', description: '超时秒数，默认 20。', minimum: 1, maximum: 120 },
      },
      required: ['command'],
    },
    tags: ['act'],
  },
];

export interface OpenXiaoaiOptions {
  cfg: OpenXiaoaiConfigSection;
  timezone: string;
  dataDir?: string;
}

/**
 * 小爱音箱（Open-XiaoAI）World。
 *
 * 这个类只做「看护 + 转发」：
 *   - 看护：按配置起停 bridge/server.py，配置改了重启
 *   - 转发：把状态变化变成事件，把模型的话变成 xiaoai_speak
 *
 * 真正的语音链路全在桥接进程里（它跟音箱的 client 直接对话）。
 * 构造期间不建连接、不读密钥、不 await 任何外部资源。
 */
export class OpenXiaoaiWorld implements World {
  readonly id = 'open-xiaoai';

  private readonly opts: OpenXiaoaiOptions;
  private host: WorldHost | null = null;
  private bridge: BridgeProcess | null = null;
  private status: BridgeStatus | null = null;
  private online = false;
  private lastOnlineAt = 0;
  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private notifiedOffline = false;

  constructor(opts: OpenXiaoaiOptions) {
    this.opts = opts;
  }

  /**
   * 只报值，文案一律来自 ENV_PROMPT.md。
   * 前缀重建时会被反复调用，因此这里只读内存、不做 I/O。
   */
  envPromptVars(): Record<string, string> {
    const cfg = this.opts.cfg;
    if (!cfg.enabled) {
      return {
        'xiaoai.state': '这个 World 当前是关闭的。',
        'xiaoai.device': '',
        'xiaoai.brain': '',
        'xiaoai.wake': '',
      };
    }
    const devices = this.status?.devices ?? [];
    const state = this.online
      ? `桥接在跑，音箱已连上（${devices.join('、') || '地址未知'}）。`
      : this.bridge?.running
        ? '桥接在跑，但音箱还没连上来：现在说话不会被接住。'
        : '桥接没在跑：现在说话不会被接住。';

    return {
      'xiaoai.state': state,
      'xiaoai.device': cfg.deviceHint.trim(),
      'xiaoai.brain': this.status?.model ?? '',
      'xiaoai.wake': parseWakeWords(cfg.wakeWords).join('、'),
    };
  }

  tools(): ToolDef[] {
    return [this.speakTool(), this.statusTool(), this.shellTool()];
  }

  webUi(): WorldWebUiDecl {
    const cfg = this.opts.cfg;
    return {
      label: '小爱音箱',
      lamps: [
        {
          label: '桥接',
          state: this.bridge?.running ? 'online' : 'offline',
          hint: this.bridge?.running ? 'server.py 在跑' : '进程没起来',
        },
        {
          label: '音箱',
          state: this.online ? 'online' : this.bridge?.running ? 'loading' : 'offline',
          hint: this.online ? '设备已连上桥接' : '设备没连上',
        },
        {
          label: '大脑',
          state: this.status?.llm === 'live' ? 'online' : 'offline',
          hint: this.status?.model ?? '未配置',
        },
      ],
      badges: [
        { label: '端口', value: cfg.bridgePort, tone: 'plain' },
        { label: '追问窗口', value: `${cfg.followupSeconds}s`, tone: 'plain' },
        { label: '打断', value: normalizeInterruptMode(cfg.interruptMode), tone: 'plain' },
      ],
      // 不声明 panels：自定义面板要配一个浏览器 bundle（webUiClient），
      // 本包不带 dist/。状态改由 xiaoai_status 工具与 lamps/badges 给。
      links: [{ label: '打开 Coopanion 控制台', href: 'http://127.0.0.1:17788/', inheritTheme: true }],
      config: [OPEN_XIAOAI_CONFIG_GROUP],
      promptDocs: [
        {
          key: 'worlds.open-xiaoai.envPrompt',
          title: '小爱音箱 · 环境提示词',
          description: '说明这台音箱是怎么接进来的、什么时候会有人从它那说话、怎么让它出声。',
          path: ENV_PROMPT_FILE,
          role: 'envPrompt',
          vars: [
            { name: 'xiaoai.state', description: '桥接与设备的连接状态。' },
            { name: 'xiaoai.device', description: '主人在配置里备注的音箱地址。' },
            { name: 'xiaoai.brain', description: '桥接当前用的脑子（模型名）。' },
            { name: 'xiaoai.wake', description: '当前生效的唤醒词。' },
          ],
        },
      ],
    };
  }

  async start(host: WorldHost): Promise<void> {
    this.host = host;
    const cfg = this.opts.cfg;

    this.bridge = new BridgeProcess({
      cfg,
      dataDir: this.opts.dataDir ?? process.cwd(),
      log: host.log,
    });
    this.bridge.onExit((code) => {
      if (cfg.autoRestart) {
        host.log.warn('桥接退出，准备重启', { code });
        setTimeout(() => {
          void this.bridge?.start().catch((err: unknown) => {
            host.log.warn('重启桥接失败', { error: String(err) });
          });
        }, 2000);
      }
    });

    await this.bridge.start();
    await this.refresh(host);

    this.pollTimer = setInterval(() => {
      void this.refresh(this.host ?? host).catch(() => undefined);
    }, POLL_MS);
  }

  async stop(): Promise<void> {
    if (this.pollTimer) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
    const bridge = this.bridge;
    this.bridge = null;
    this.online = false;
    this.status = null;
    await bridge?.stop();
  }

  // ---- 内部 ----

  /** 轮询管理口；状态翻转时推事件，配置变了就让桥接重启。 */
  private async refresh(host: WorldHost): Promise<void> {
    const bridge = this.bridge;
    if (!bridge) return;

    // cfg 是活引用：WebUI 改了旋钮，这里每轮比对一次翻译后的配置文本。
    // syncConfig() 内部文本相同时直接返回 false，不会白重启。
    try {
      if (await bridge.syncConfig()) await this.refreshAfterConfig(host);
    } catch (err) {
      host.log.warn('同步桥接配置失败', { error: String(err) });
    }

    const status = await bridge.status();
    const wasOnline = this.online;
    const now = Date.now();

    if (status) {
      this.status = status;
      this.online = status.devices.length > 0;
      if (this.online) {
        this.lastOnlineAt = now;
        this.notifiedOffline = false;
      }
    } else {
      this.online = false;
    }

    if (this.online && !wasOnline) {
      await this.push(host, 'xiaoai.connected', '[小爱音箱] 音箱连上来了，可以听它说话了。');
    } else if (!this.online && wasOnline) {
      await this.push(host, 'xiaoai.disconnected', '[小爱音箱] 音箱断开了，现在听不到它。');
    } else if (
      !this.online &&
      !this.notifiedOffline &&
      this.lastOnlineAt > 0 &&
      now - this.lastOnlineAt > OFFLINE_NOTIFY_MS
    ) {
      this.notifiedOffline = true;
      await this.push(
        host,
        'xiaoai.offline',
        '[小爱音箱] 已经一分钟没连上音箱了。要查就调 xiaoai_status。',
      );
    }
  }

  /** 配置改动导致桥接重启后：在日志里说一声，状态轮询会接着刷新。 */
  private async refreshAfterConfig(host: WorldHost): Promise<void> {
    host.log.info('桥接配置已更新并重启');
  }

  private async push(host: WorldHost, type: string, text: string): Promise<void> {
    const event: EventEnvelope = {
      type,
      ts: nowIso(this.opts.timezone),
      source: this.id,
      origin: 'external',
      text,
      senderKey: this.id,
    };
    try {
      await host.pushEvent(event, { trigger: 'piggyback' });
    } catch (err) {
      host.log.warn('音箱事件投递失败', { error: String(err) });
    }
  }

  private speakTool(): ToolDef {
    return {
      ...OPEN_XIAOAI_TOOL_DECLS[0],
      handler: async (args) => {
        const text = String(args.text ?? '').trim();
        if (text === '') return '没给要念的文字。';
        const bridge = this.bridge;
        if (!bridge?.running) return '桥接没在跑，音箱现在念不了。先看 xiaoai_status。';
        try {
          const raw = (await bridge.command({ speak: text }, 20_000)) as Record<string, unknown>;
          if (raw?.ok !== true) {
            const reason = String(raw?.error ?? '未知原因');
            return reason.includes('没有设备')
              ? '音箱还没连上桥接，这句话没念出去。等它连上（或看 xiaoai_status）再试。'
              : `交给音箱失败：${reason}`;
          }
          return `已经交给音箱念了：${text}`;
        } catch (err) {
          return `交给音箱失败：${String(err)}`;
        }
      },
    };
  }

  private statusTool(): ToolDef {
    return {
      ...OPEN_XIAOAI_TOOL_DECLS[1],
      handler: async () => {
        const cfg = this.opts.cfg;
        const lines: string[] = [];
        lines.push(`启用：${cfg.enabled ? '是' : '否'}`);
        lines.push(`桥接进程：${this.bridge?.running ? `在跑（pid ${this.bridge.pid ?? '?'}）` : '没起来'}`);
        lines.push(`桥接端口：${cfg.bridgePort}，管理口：127.0.0.1:${cfg.adminPort}`);
        const status = this.status;
        if (!status) {
          lines.push('管理口：连不上（桥接可能刚起或已挂）');
          const tail = this.bridge?.tailText(12) ?? '';
          if (tail) lines.push(`进程输出尾巴：\n${tail}`);
        } else {
          lines.push(`音箱：${status.devices.join('、') || '还没连上来'}`);
          const brain = status.brain || '未知';
          const model = status.model || '未配置';
          const llm = status.llm === 'live' ? '可用' : status.llm || '未知';
          lines.push(`大脑：${brain}，模型 ${model}（${llm}）`);
          lines.push(`正在说话：${status.speaking ? '是' : '否'}`);
          if (status.wake_without_asr > 0) {
            lines.push(
              `ASR 异常计数：${status.wake_without_asr}（唤醒后没识别出字。这是设备长时间运行后的已知退化，重启音箱可恢复）`,
            );
          }
        }
        if (cfg.deviceHint.trim()) lines.push(`备注地址：${cfg.deviceHint.trim()}`);
        return lines.join('\n');
      },
    };
  }

  private shellTool(): ToolDef {
    return {
      ...OPEN_XIAOAI_TOOL_DECLS[2],
      handler: async (args) => {
        const command = String(args.command ?? '').trim();
        if (command === '') return '没给命令。';
        const timeoutS = Number(args.timeout_s ?? 20);
        const bridge = this.bridge;
        if (!bridge?.running) return '桥接没在跑，连不上音箱。';
        try {
          const raw = (await bridge.command(
            { run_shell: command, timeout: timeoutS },
            (timeoutS + 10) * 1000,
          )) as Record<string, unknown>;
          if (raw?.ok !== true) return `执行失败：${String(raw?.error ?? '未知错误')}`;
          const result = raw.result as Record<string, unknown> | undefined;
          const out = String(result?.stdout ?? '').trim();
          const err = String(result?.stderr ?? '').trim();
          const code = result?.exit_code;
          return [
            `exit=${String(code)}`,
            out ? `stdout:\n${out}` : 'stdout:（空）',
            err ? `stderr:\n${err}` : '',
          ]
            .filter(Boolean)
            .join('\n');
        } catch (err) {
          return `执行失败：${String(err)}`;
        }
      },
    };
  }
}

/** prompt 模板路径（打包后与 world.ts 同级）。 */
export const ENV_PROMPT_FILE = fileURLToPath(new URL('./ENV_PROMPT.md', import.meta.url));

/** 工具声明的自描述摘要（definition.ts 里用）。 */
export const OPEN_XIAOAI_TOOL_SUMMARIES: Record<string, { zh: string; en: string }> = {
  xiaoai_speak: {
    zh: '让音箱念一句话，用音箱自己的 TTS，不需要语音唤醒。',
    en: 'Speaks a sentence through the speaker using its own TTS, no wake word needed.',
  },
  xiaoai_status: {
    zh: '查桥接进程、设备连接、市面模型与 ASR 异常计数。',
    en: 'Reports the bridge process, device connection, active brain and ASR anomaly count.',
  },
  xiaoai_shell: {
    zh: '在音箱上跑一条命令取回输出，排障用。',
    en: 'Runs one shell command on the speaker and returns its output; for troubleshooting.',
  },
};
