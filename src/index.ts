/** 包入口：默认导出 `WorldDefinition`，加载器按 `cortico.kind === 'world'` 认它。 */
import { OPEN_XIAOAI } from './definition.ts';

export default OPEN_XIAOAI;
export { OPEN_XIAOAI };
export type { OpenXiaoaiConfigSection, InterruptModeName } from './config.ts';
