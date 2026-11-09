# 安装指导

从零把这套东西跑起来：**小米小爱音箱 → Open-XiaoAI 客户端 → 本机桥接 → Coopanion**。

全程分四段，可以分段停下来验证。第 1、2 段在音箱上做，第 3、4 段在这台电脑上做。

---

## 目录

- [0. 你需要什么](#0-你需要什么)
- [1. 音箱：刷 Open-XiaoAI 补丁固件](#1-音箱刷-open-xiaoai-补丁固件)
- [2. 音箱：装 client](#2-音箱装-client)
- [3. 电脑：装这个 World 扩展](#3-电脑装这个-world-扩展)
- [4. 电脑：配置与启动](#4-电脑配置与启动)
- [5. 验收](#5-验收)
- [6. 排障](#6-排障)
- [附：完整卸载](#附完整卸载)

---

## 0. 你需要什么

| | 要求 |
| --- | --- |
| 音箱 | 小米小爱音箱 **Pro（OH2P）** 或 **LX06**。其它型号没验证过。 |
| 电脑 | Windows / macOS / Linux 都行，跑得动 Coopanion。 |
| Coopanion | 0.2.18 或更新（扩展 API 版本 6）。 |
| Node | **22 或以上**（扩展声明 `engines.node >= 22`）。Coopanion 自带的不算，它跑的是自己的进程。 |
| Python | **3.9 或以上**，只用标准库，不需要 pip 装任何东西。 |
| 网络 | 音箱和电脑在**同一个局域网**。桥接要能被音箱主动连上。 |

> **重要**：这套东西不是即插即用的。音箱必须先刷固件才能 SSH、才能跑 client。刷机不可逆（有回滚手段，但要动手），请先看完第 1 段再决定。

---

## 1. 音箱：刷 Open-XiaoAI 补丁固件

这一步照官方文档做，别照我这抄——版本和工具都会更新。

**官方文档**：<https://github.com/idootop/open-xiaoai/blob/main/docs/flash.md>

要点（以 OH2P 为例）：

1. 下载刷机工具 **Amlogic_Flash_Tool_v6.0.0** 和对应型号的补丁固件（Releases 里的 `OH2P_*` / `LX06_*`）。
2. 装驱动：跑工具目录里的 `AMLLogic driver installer.exe`。装完设备管理器里会出现 `WorldCup Device`（VID `1B8E`，PID `C003`）。
3. 固件重命名为 `root_patched.squashfs`。
4. 用底部 Type-C 口把音箱连到电脑（OH2P 不用拆机）。
5. 打开工具目录的 `bin`，在这里执行：

   ```bash
   # 拔掉音箱电源，重新插上，然后立刻执行（要抢在启动早期）
   ./update.exe identify
   # 看到 "This firmware version is 0-7-0-16..." 才算进烧录模式
   ```

   ```bash
   ./update.exe bulkcmd "setenv bootdelay 15"
   ./update.exe bulkcmd "setenv boot_part boot0"
   ./update.exe bulkcmd "saveenv"
   ```

   ```bash
   ./update.exe partition system0 root_patched.squashfs
   # 看到 mwrite success 就成了
   ```

6. 拔掉数据线和电源，重新上电。

> **Windows 上没有 Git Bash？** 官方文档说的「右键 → Git Bash Here」只是要一个能跑 `./update.exe` 的 shell。直接在这个目录打开 PowerShell / cmd 也行，把 `./update.exe` 写成 `.\update.exe`。

> **回滚**：`./update.exe bulkcmd "setenv boot_part boot1"` 然后重启，回到原厂系统。

刷完之后音箱的 IP 会变，去路由器后台找一下，或者扫一遍局域网：

```powershell
# Windows PowerShell，扫 22 端口
1..254 | ForEach-Object -Parallel {
  $c = New-Object Net.Sockets.TcpClient
  if ($c.ConnectAsync("192.168.1.$_", 22).Wait(200)) { "192.168.1.$_" }
  $c.Close()
} -ThrottleLimit 64
```

```bash
# macOS / Linux
nmap -p 22 --open 192.168.1.0/24
```

---

## 2. 音箱：装 client

SSH 上去（**默认密码 `open-xiaoai`**）：

```bash
ssh -o HostKeyAlgorithms=+ssh-rsa root@<音箱IP>
```

> 老版本 OpenSSH 客户端不加 `-o HostKeyAlgorithms=+ssh-rsa` 会报 `no matching host key type found`——音箱只提供 `ssh-rsa`。

进去之后三步：

```bash
# 1. 建目录
mkdir -p /data/open-xiaoai

# 2. 告诉它桥接在哪（把 IP 换成你电脑的局域网地址）
echo 'ws://<你电脑的IP>:4399' > /data/open-xiaoai/server.txt

# 3. 下载并启动 client（会自动装到 /data/open-xiaoai/client）
curl -sSfL https://gitee.com/idootop/artifacts/releases/download/open-xiaoai-client/init.sh | sh
```

`init.sh` 是前台运行的。想让它开机自启：

```bash
curl -L -o /data/init.sh \
  https://gitee.com/idootop/artifacts/releases/download/open-xiaoai-client/boot.sh
chmod +x /data/init.sh
reboot
```

重启大约 40 秒后 client 会自动重连。

> **client 从哪来？** 它不在 GitHub Releases 里，托管在 Gitee：<https://gitee.com/idootop/artifacts/releases/download/open-xiaoai-client/>（`client`、`init.sh`、`boot.sh`）。

> **怎么确认装好了？** `ls -l /data/open-xiaoai/` 应该能看到 `client`、`server.txt`；`ps | grep client` 能看到进程。它现在连不上桥接是正常的——桥接还没起。

---

## 3. 电脑：装这个 World 扩展

### 3.1 找到 Coopanion 的扩展目录

| | 路径 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\..\..\` 不确定就看下面 |
| 通用办法 | 见下方说明 |

最稳的找法：Coopanion 的数据目录在安装目录旁的 `data/`，扩展目录是 `data/extensions/`。Windows 上通常是：

```
<Coopanion 安装目录>\data\extensions\
```

打开它，里面应该已经有 `package.json`、`node_modules\`、`world-owners.json` 这些。**如果没看到，说明你找错地方了，别继续往下走。**

### 3.2 把包放进去

把本仓库整个目录放到扩展目录的 `node_modules/` 下，命名为 `cortico-world-open-xiaoai`：

```
<Coopanion>\data\extensions\                      ← 扩展目录
├── package.json                                   ← 要改
├── world-owners.json                              ← 要改
└── node_modules\
    └── cortico-world-open-xiaoai\                 ← 把仓库放这儿
        ├── package.json
        ├── src\
        ├── bridge\
        └── assets\
```

> **Windows 用户可以用目录联接**，这样改了源码立刻生效，不用反复复制：
>
> ```powershell
> cmd /c mklink /J "C:\...\data\extensions\node_modules\cortico-world-open-xiaoai" "D:\你的clone目录\cortico-world-open-xiaoai"
> ```
>
> macOS / Linux 用 `ln -s`。

### 3.3 登记到 `extensions/package.json`

打开扩展目录的 `package.json`，在 `dependencies` 里加一行：

```json
{
  "name": "cortico-extensions",
  "private": true,
  "dependencies": {
    "cortico-world-open-xiaoai": "0.1.0",
    "...": "其它包别动"
  }
}
```

> 版本号写什么都行，加载器只读**键名**，具体实体从 `node_modules/` 找。

### 3.4 登记到 `world-owners.json`

这个文件记录「World id → 包名」，卸载时用来清配置。加上：

```json
{
  "open-xiaoai": "cortico-world-open-xiaoai"
}
```

（在原有内容里追加这一条，别覆盖别的。）

### 3.5 验证装对了

**简单办法**：启动 Coopanion，看 `<Coopanion>\data\logs\core.log` 里有没有：

```
INFO  core: World 已启动: open-xiaoai
```

有就是装上了。没出现、或者出现 `World id 已被占用` / `扩展包内缺少` 之类的，往下看排障。

**进阶办法**（本机装了 Node 22+ 时可以跑，用框架自带的 dry-mount 做静态检查）：

```bash
cd <Coopanion>/resources/app
node --import tsx --import file:///<Coopanion>/resources/app/build/cortico/src/extensions/runtime.ts \
  --input-type=module \
  -e "await import('file:///<扩展目录>/node_modules/cortico-world-open-xiaoai/src/selfcheck.ts')"
```

全绿（`failures: []`、`warnings: []`）再往下走。

> 注意路径都要写成 `file:///` 开头的 URL，直接给 `D:\...` 会报 `ERR_UNSUPPORTED_ESM_URL_SCHEME`。
>
> 发行版里没有 `pnpm check:extension` 这个脚本（那是框架源码仓库才有的），所以上面用等价的 dry-mount 调用。

---

## 4. 电脑：配置与启动

### 4.1 改部署配置

找到部署目录的 `config.json`（`<Coopanion>\data\home\<部署名>\config.json`，通常部署名叫 `companion`），在 `worlds` 里加一段：

```json
{
  "worlds": {
    "open-xiaoai": {
      "enabled": true,
      "bridgePort": 4399,
      "adminPort": 4400,
      "pythonPath": "",
      "extraArgs": "",
      "autoRestart": true,
      "wakeWords": "小智,小智同学,管家",
      "followupSeconds": 12,
      "reopenAfterSpeak": true,
      "interruptMode": "kill",
      "deviceHint": "<音箱IP>"
    }
  }
}
```

改之前**先备份**。用 `ConvertTo-Json` 之类重新序列化整个文件有风险，最好手工编辑。

### 4.2 选大脑

桥接支持两种大脑，在 `bridge/config.example.json` 里看，运行时配置会自动生成在部署目录的 `open-xiaoai/bridge.json`：

**A. 直连 DeepSeek**（简单）

```json
{ "brain": { "backend": "deepseek" } }
```

然后给个 key：设环境变量 `DEEPSEEK_API_KEY`，或写进 `bridge.json` 的 `llm.api_key`。

**B. 交给 Coopanion**（推荐，能带上全部工具）

```json
{
  "brain": { "backend": "coopanion" },
  "coopanion": {
    "host": "127.0.0.1",
    "port": 17788,
    "name": "音箱",
    "owner": "",
    "fallback": true
  }
}
```

`owner` 填 Coopanion 里那个 World 的 botName（留空则接受任何人的回复）。`fallback: true` 表示 Coopanion 那边没回应时回落 DeepSeek，保证音箱永远有话说。

> 通道细节：桥接走 `ws://127.0.0.1:17788/ws/providers/world%3Aconsole/panels/chat` 注入文本，要求对方**用 `console_send` 回复**（否则它可能用桌宠气泡说话，你收不到）。

### 4.3 启动

```powershell
# 如果从带 ELECTRON_RUN_AS_NODE=1 的 shell（比如某些 IDE/Agent 终端）里启动，
# 必须先删掉这个变量，否则 Electron 退化成纯 Node 模式，进程秒退、无任何日志
Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue

Start-Process "C:\Path\To\Coopanion.exe" -WorkingDirectory "C:\Path\To\Coopanion"
```

启动后看 `<Coopanion>\data\logs\core.log`，应该出现：

```
INFO  worlds.open-xiaoai: 启动桥接
INFO  worlds.open-xiaoai: 桥接管理口已就绪
INFO  core: World 已启动: open-xiaoai
```

### 4.4 开关

界面上「小爱音箱」那个 World 直接开关。或者走 API：

```bash
curl -X POST http://127.0.0.1:17788/api/worlds/activation \
  -H 'Content-Type: application/json' \
  -d '{"id":"open-xiaoai","enabled":false}'
```

> **注意**：直接改 `config.json` 的 `enabled` **不会**触发启停，装配层不监听文件。必须走界面或上面这个 API。

---

## 5. 验收

按顺序确认这些，哪一步不对就停在那一步查：

**① 端口在听**

```powershell
Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in 4399,4400 }
```

应该两条：`0.0.0.0:4399` 和 `127.0.0.1:4400`。

**② 音箱连上来了**

```bash
# 桥接日志，在你部署目录下
tail -f <Coopanion>/data/home/companion/data/open-xiaoai/logs/bridge-$(date +%Y%m%d).log
```

看到 `✅ 设备已连接: 192.168.x.x:xxxxx` 就成了。

**③ 让它说句话**

对着音箱说：

```
小爱同学，小智，你好
```

> 唤醒词是**「小爱同学，<你的唤醒词>」**。音箱只认小米自己的唤醒词引擎，我们改不了——「小智」只是把这句话从「交给小爱」改成「交给桥接」的开关。

---

## 6. 排障

### 音箱连不上桥接

| 检查 | 怎么做 |
| --- | --- |
| `server.txt` 对不对 | `cat /data/open-xiaoai/server.txt`，必须是 `ws://<电脑IP>:4399` |
| 电脑 IP 变了没 | 桥接日志会显示实际连上来的地址；`server.txt` 里的 IP 要改成同一个 |
| 桥接端口开着没 | 见验收 ① |
| 防火墙 | Windows 防火墙可能拦了 4399。加个入站规则。 |
| client 在跑没 | `ps \| grep client` |

### 电脑上找不到 Python

在 World 配置里填「Python 路径」直接指定，别依赖自动查找。

### 音箱喊了没反应 / 喊完就关麦

**设备侧 ASR 长时间运行后会退化**（我实测连续跑 13 天出现），现象是：喊了「小爱同学」灯亮，但不等你说话就灭了。

判断方法：SSH 上去看设备日志里有没有 `onRecognizeResult`：

```bash
tail -f /tmp/log/messages | grep -E "onRecognizeResult|first_round_7s_no_asr"
```

看到 `first_round_7s_no_asr_timeout` 且带 `has no asr`，就是这个问题。**重启音箱**（`sync; reboot`）即可恢复，不用改任何配置。

### 小爱和桥接抢着说话

在 World 配置里把「打断小爱的方式」调一下：
- `kill`：重启音箱播放器，掐得干净但重一点（**推荐**）
- `duck`：只暂停播放，轻但小爱可能接着说

### 音箱听到自己的声音

桥接播报时要带上 `-n` 参数（`tts_play.sh -n`）告知设备"这是 TTS"，设备会关掉唤醒词引擎避免自激。这个已经内置了。

### 想看桥接在干嘛

```bash
# 管理口（只绑 127.0.0.1）
curl -s http://127.0.0.1:4400 -d '{"status":true}'
```

或者直接问模型：`xiaoai_status` 工具会把线路现状拼成几行中文。

---

## 附：完整卸载

1. 界面里关掉「小爱音箱」这个 World。
2. 删 `<Coopanion>\data\extensions\node_modules\cortico-world-open-xiaoai\`。
3. 从 `extensions/package.json` 的 `dependencies` 里删掉那一行。
4. 从 `world-owners.json` 里删掉 `"open-xiaoai"`。
5. 从部署的 `config.json` 的 `worlds` 里删掉 `"open-xiaoai"` 段。
6. 重启 Coopanion。

音箱那边不受影响，client 会一直尝试重连。想彻底断开就在音箱上停掉 client。

---

## 许可

MIT，见 [LICENSE](LICENSE)。上游 <https://github.com/idootop/open-xiaoai> 同为 MIT（Copyright © 2024 Del Wang）。
