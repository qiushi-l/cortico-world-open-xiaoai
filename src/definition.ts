import { describeTools, type WorldDefinition } from 'cortico/world.ts';
import { OPEN_XIAOAI_DEFAULTS, type OpenXiaoaiConfigSection } from './config.ts';
import {
  OPEN_XIAOAI_TOOL_DECLS,
  OPEN_XIAOAI_TOOL_SUMMARIES,
  OpenXiaoaiWorld,
} from './world.ts';

export const OPEN_XIAOAI: WorldDefinition<OpenXiaoaiConfigSection> = {
  id: 'open-xiaoai',
  label: '小爱音箱',
  defaults: () => ({ ...OPEN_XIAOAI_DEFAULTS }),
  // 只构造，不连接：装配层会为未启用的 World 也调用一次 create。
  create: (ctx) =>
    new OpenXiaoaiWorld({
      cfg: ctx.cfg,
      timezone: ctx.timezone,
      dataDir: ctx.dataDir,
    }),
  describe: {
    tools: describeTools([...OPEN_XIAOAI_TOOL_DECLS], OPEN_XIAOAI_TOOL_SUMMARIES),
    events: [
      {
        type: 'xiaoai.connected',
        description: {
          zh: '音箱连上桥接，可以听它说话了。',
          en: 'The speaker connected to the bridge and can be heard.',
        },
      },
      {
        type: 'xiaoai.disconnected',
        description: {
          zh: '音箱与桥接断开，现在听不到它。',
          en: 'The speaker dropped off the bridge and can no longer be heard.',
        },
      },
      {
        type: 'xiaoai.offline',
        description: {
          zh: '音箱已经一分钟以上没连上来。',
          en: 'The speaker has been unreachable for over a minute.',
        },
      },
    ],
  },
};
