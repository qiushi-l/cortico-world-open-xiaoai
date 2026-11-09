import type { ConfigGroup } from 'cortico/core/types.ts';

/**
 * `worlds.open-xiaoai` 段。
 *
 * 这个 World 自己不碰音箱：它管的是本地那个 Python 桥接进程（`bridge/server.py`），
 * 桥接再去接音箱的 client 和大脑。所以这里的配置分三类：
 *   1. 桥接进程怎么起（python 路径、监听端口、管理口）
 *   2. 谁在听写（唤醒词、追问窗口、打断方式）
 *   3. 音箱在哪个网段（给状态页做提示用）
 */
export interface OpenXiaoaiConfigSection {
  /** 挂载开关；默认关闭，由部署或 bot 打开。 */
  enabled: boolean;
  /** 桥接监听的端口，音箱 client 的 server.txt 要指向它。 */
  bridgePort: number;
  /** 本机管理口端口（只绑 127.0.0.1），扩展用它查状态、发指令。 */
  adminPort: number;
  /** Python 解释器路径；留空则依次尝试 PATH 里的 python / 内置运行时。 */
  pythonPath: string;
  /** 启动桥接时额外的命令行参数，空格分隔。 */
  extraArgs: string;
  /** 桥接挂了自动拉起。 */
  autoRestart: boolean;
  /** 唤醒词，逗号分隔。命中后这句才归本 World 处理。 */
  wakeWords: string;
  /** 回答完之后多少秒内，紧跟的下一句仍算追问（不用再喊唤醒词）。 */
  followupSeconds: number;
  /** 播报完是否主动重新开麦，让用户可以直接接着说。 */
  reopenAfterSpeak: boolean;
  /** 打断小爱的方式：kill 直接重启播放器，duck 只暂停。 */
  interruptMode: InterruptModeName;
  /** 音箱的局域网地址，只用于本页显示提示。 */
  deviceHint: string;
}

export type InterruptModeName = 'kill' | 'duck';

export const INTERRUPT_MODES: readonly InterruptModeName[] = ['kill', 'duck'];

export const OPEN_XIAOAI_DEFAULTS: OpenXiaoaiConfigSection = {
  enabled: false,
  bridgePort: 4399,
  adminPort: 4400,
  pythonPath: '',
  extraArgs: '',
  autoRestart: true,
  wakeWords: '小智,小智同学,管家',
  followupSeconds: 12,
  reopenAfterSpeak: true,
  interruptMode: 'kill',
  deviceHint: '',
};

/** 把配置里读到的任意值收敛成合法值；WebUI 存进来的东西不保证类型。 */
export function normalizeInterruptMode(raw: unknown): InterruptModeName {
  return raw === 'duck' ? 'duck' : 'kill';
}

/** 解析逗号分隔的唤醒词，去掉空项与首尾空白。 */
export function parseWakeWords(raw: unknown): string[] {
  if (typeof raw !== 'string') return [];
  return raw
    .split(/[,，]/)
    .map((s) => s.trim())
    .filter((s) => s !== '');
}

/** World 的配置组；`worlds.open-xiaoai.` 段内的旋钮，路径必须落在自己的段里。 */
export const OPEN_XIAOAI_CONFIG_GROUP: ConfigGroup = {
  id: 'world:open-xiaoai',
  owner: 'world:open-xiaoai',
  schema: {
    type: 'object',
    title: '小爱音箱 · Open-XiaoAI',
    description:
      '桥接进程把音箱听到的话变成事件，把回答念回音箱。这台 World 负责起停它，并把你改的这些参数同步给桥接。',
    properties: {
      'worlds.open-xiaoai.bridgePort': {
        type: 'integer',
        title: '桥接端口',
        minimum: 1024,
        maximum: 65535,
        multipleOf: 1,
        'x-hot': false,
        description:
          '桥接监听给音箱 client 连的端口。音箱上 /data/open-xiaoai/server.txt 要写成 ws://<这台电脑的局域网 IP>:<这个端口>。改完要重启本 World。',
      },
      'worlds.open-xiaoai.adminPort': {
        type: 'integer',
        title: '管理口端口',
        minimum: 1024,
        maximum: 65535,
        multipleOf: 1,
        'x-hot': false,
        description: '管理口只绑 127.0.0.1，本 World 用它查设备状态、让它说话。改完要重启本 World。',
      },
      'worlds.open-xiaoai.pythonPath': {
        type: 'string',
        title: 'Python 路径',
        'x-hot': false,
        'x-path': { kind: 'file', extensions: ['exe'] },
        description:
          '跑 bridge/server.py 的解释器（需要 3.9 以上，只用标准库）。留空则自动找：PATH 里的 python，再用 DSH 内置运行时。',
      },
      'worlds.open-xiaoai.extraArgs': {
        type: 'string',
        title: '额外启动参数',
        'x-hot': false,
        description: '追加给 server.py 的命令行参数，空格分隔。例如 --dry-run 只打印、不动音箱。',
      },
      'worlds.open-xiaoai.autoRestart': {
        type: 'boolean',
        title: '掉线自动拉起',
        'x-hot': true,
        description: '桥接进程意外退出时自动重启。关掉就只在启用/禁用时起停。',
      },
      'worlds.open-xiaoai.wakeWords': {
        type: 'string',
        title: '唤醒词',
        'x-hot': true,
        description:
          '命中这些词的那句话才交给本 World，其余留给小爱自己。多个用逗号分隔。注意：音箱只认小米自己的唤醒词，实际说法仍是「小爱同学，<唤醒词>，今天几点」。',
      },
      'worlds.open-xiaoai.followupSeconds': {
        type: 'integer',
        title: '追问窗口',
        minimum: 0,
        maximum: 120,
        multipleOf: 1,
        'x-suffix': '秒',
        'x-hot': true,
        description:
          '回答完之后这么多秒内，紧跟着说的话即使不带唤醒词也算在跟本 World 聊。设备侧「重新开麦」的窗口实测约 14 秒。',
      },
      'worlds.open-xiaoai.reopenAfterSpeak': {
        type: 'boolean',
        title: '念完重新开麦',
        'x-hot': true,
        description:
          '播报结束后主动让音箱进入聆听态，用户可以接着说不必再喊唤醒词。关掉则每句都要重新唤醒。',
      },
      'worlds.open-xiaoai.interruptMode': {
        type: 'string',
        title: '打断小爱的方式',
        enum: [...INTERRUPT_MODES],
        'x-hot': true,
        description:
          'kill：重启音箱播放器，掐得干净但重一点；duck：只暂停播放，轻但小爱可能接着说。',
      },
      'worlds.open-xiaoai.deviceHint': {
        type: 'string',
        title: '音箱地址（备注）',
        'x-hot': false,
        description: '只用于本页显示，例如 192.168.1.7。桥接本身不靠它连设备（设备会主动连上来）。',
      },
    },
  },
};
