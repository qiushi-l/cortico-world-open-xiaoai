# cortico-world-open-xiaoai · 小爱音箱

把一台刷了 [Open-XiaoAI](https://github.com/idootop/open-xiaoai) 的小米小爱音箱接进 [Coopanion](https://github.com/Pal-AI-Lab/Coopanion) 的 World 扩展。

装上之后，你对音箱说的话会送到这台电脑上的桥接进程，桥接把「带唤醒词的句子」交给你的 AI 伙伴；它回的话，由音箱自己的 TTS 念出来。

```
        你的声音
           │
           ▼
   ┌───────────────┐        语音识别结果          ┌──────────────────┐
   │  小米音箱      │  ─────────────────────────▶ │  本机桥接         │
   │  + Open-XiaoAI│        WS :4399             │  (bridge/server.py)│
   │    client     │  ◀───────────────────────── │                   │
   └───────────────┘        TTS 播报文本          └────────┬─────────┘
                                                          │
                                              ┌───────────┴───────────┐
                                              ▼                       ▼
                                      Coopanion（全部工具）    DeepSeek API
```

## 为什么需要它

音箱本身只能做「小爱同学」那套：放歌、定闹钟、开关家电。接上之后，**「小爱同学，小智，今天天气怎么样」** 这句话会绕过小爱的问答，交给你的 AI 伙伴来答——它能读文件、查资料、调工具，答案再由音箱念出来。

一台音箱，两个大脑，各管一摊。

## 功能

- **起停桥接**：按配置拉起 `bridge/server.py`（纯标准库 Python，零第三方依赖），禁用 World 时收干净。
- **状态事件**：桥接/设备在线状态翻转时推 `xiaoai.connected` / `xiaoai.disconnected` / `xiaoai.offline`。
- **三个工具**：`xiaoai_speak`（让音箱念一句话）、`xiaoai_status`（查线路现状）、`xiaoai_shell`（排障用，在音箱上跑只读命令）。
- **状态灯**：桥接 / 音箱 / 大脑三盏灯，加端口、追问窗口、打断方式三个徽标。
- **连续对话**：播报完主动让音箱重新进入聆听态，接着说不必再喊唤醒词（实测窗口约 14 秒）。
- **不抢话**：小爱要开口时把它的播放器掐掉，避免两个声音叠在一起。

真正的语音链路都在 Python 桥接里——它跟音箱的 client 直接对话，负责唤醒词匹配、追问窗口、重新开麦、打断小爱这些设备侧的活。World 只负责看护那个进程、把配置翻给它。

## 快速开始

**前置条件**：音箱已刷 Open-XiaoAI 补丁固件 · 音箱已装 client 且 `server.txt` 指向本机 · 本机有 Python 3.9+ 和 Node 22+。

### 从扩展市场装（推荐）

Coopanion → 控制台 → 扩展市场 → 搜「小爱音箱」或 `cortico-world-open-xiaoai` → 安装 → 重启 Coopanion。

### 或者手动装

```bash
# 1. 在 Coopanion 的扩展目录里装包
cd <Coopanion>/data/extensions
pnpm add cortico-world-open-xiaoai

# 2. 在 world-owners.json 里加一条（装的时候不会自动写）
"open-xiaoai": "cortico-world-open-xiaoai"

# 3. 重启 Coopanion，界面里就多了一个「小爱音箱」World
```

没有 pnpm 的话，把包解压到 `<Coopanion>/data/extensions/node_modules/cortico-world-open-xiaoai/`，
再在 `extensions/package.json` 的 dependencies 里加一行 `"cortico-world-open-xiaoai": "0.1.0"` 也一样。

**完整步骤见 [INSTALL.md](INSTALL.md)** —— 从刷固件、装 client，到配置、验收、排障，一步步写全了。

## 配置

都在「小爱音箱」这个 World 的设置里，写进部署 `config.json` 的 `worlds.open-xiaoai` 段：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 开关。关掉时桥接不跑，说话不会被接住。 |
| `bridgePort` | `4399` | 桥接给音箱 client 连的端口。 |
| `adminPort` | `4400` | 管理口，只绑 `127.0.0.1`。 |
| `pythonPath` | `""` | 解释器路径；留空自动找 PATH 里的 `python` / `python3`。 |
| `autoRestart` | `true` | 桥接意外退出时自动拉起。 |
| `wakeWords` | `小智,小智同学,管家` | 唤醒词，逗号分隔。命中才归本 World，其余留给小爱自己。 |
| `followupSeconds` | `12` | 追问窗口。设备侧「重新开麦」实测约 14 秒，所以别设太大。 |
| `reopenAfterSpeak` | `true` | 播报完主动重新开麦，用户可以直接接着说。 |
| `interruptMode` | `kill` | 打断小爱的方式：`kill`（重启播放器，干净但重）或 `duck`（只暂停）。 |

桥接自己的配置（模型、人设、大脑后端）在 [`bridge/config.example.json`](bridge/config.example.json) 里有全量默认值。World 只覆盖上面这几项，其余原样带过去。运行时配置和日志落在 `<部署目录>/open-xiaoai/`，不写进扩展包（升级包会整目录替换）。

## 唤醒词的限制

**别指望改这个就能换唤醒词。** 音箱只认小米自己的 KWS 引擎，我们改不了。实际说法永远是：

```
小爱同学，小智，现在几点了
```

`小智` 不是新的唤醒词，它只是把这句话从「交给小爱」改成「交给桥接」的开关。匹配的是 ASR 转写结果里的子串。

## 已知限制

- **只验证过 OH2P**（小米小爱音箱 Pro）。LX06 理论可行（同一套固件路线），但我没有设备。
- **必须先刷固件**。刷机不可逆（有回滚手段），这不是一个「装上就能用」的扩展。
- **设备侧 ASR 会退化**。长时间运行（实测 13 天）后可能出现「喊了唤醒词但不等你说话就关麦」，reboot 即恢复。详见 INSTALL.md 排障段。
- **没有自定义面板**。状态通过 lamps/badges + `xiaoai_status` 工具展示，不带 `dist/webui.js`（符合框架约定，官方 astrbot 扩展也是这样）。
- **改 `config.json` 的 `enabled` 不会触发启停**，必须走界面或 `/api/worlds/activation` API。

## 生态

- [Open-XiaoAI](https://github.com/idootop/open-xiaoai) —— 音箱侧的补丁固件和 client，本扩展的另一半
- [Coopanion](https://github.com/Pal-AI-Lab/Coopanion) —— 宿主框架

## 许可

MIT，见 [LICENSE](LICENSE)。上游 Open-XiaoAI 同为 MIT（Copyright © 2024 Del Wang）。
