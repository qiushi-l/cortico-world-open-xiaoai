#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Open-XiaoAI 音箱 <- 桥接服务 (bridge) -> DeepSeek
-----------------------------------------------
纯标准库实现（无第三方依赖）：
  * RFC6455 WebSocket 服务端（设备端 client 主动连进来，格式见
    packages/client-rust/src/services/connect/*.rs）
  * 监听设备上报的 instruction / playing / kws 事件
  * 命中唤醒词后：打断小爱 -> 调 DeepSeek -> 用设备自带 TTS 播报
  * 通过 RPC 指挥设备：run_shell / get_version / start_play / stop_play ...

协议（Rust 侧 enum AppMessage，外部标签序列化）：
  {"Request":  {"id","command","payload"}}     服务端 -> 设备
  {"Response": {"id","code","msg","data"}}     设备 -> 服务端
  {"Event":    {"id","event","data"}}          设备 -> 服务端
  {"Stream":   {"id","tag","bytes":[...],"data"}}  二进制帧里的 JSON

用法：
  python server.py [--port 4399] [--config config.json] [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
import struct
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

HERE = Path(__file__).resolve().parent
# 日志目录：默认在脚本旁边；作为 Coopanion 扩展运行时由 --log-dir 指到部署目录，
# 避免往扩展包目录里写东西（升级包会被覆盖）。
LOG_DIR = HERE / "logs"

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# 设备端脚本（来自 packages/client-rust/src/services/speaker.rs）
# 注意：restart mico_aivs_lab 会把它打成"僵尸收听态"（一直向上游推麦流、永不结束，
# 之后就再也唤不醒），所以默认不用它来打断，只在人工 recover 时用。
CMD_RECOVER_XIAOAI = "/etc/init.d/mico_aivs_lab restart >/dev/null 2>&1"
# 小爱自己的回答走 mediaplayer（实测 status 1=播放、0=停止），我们的播报走
# tts_play.sh -> mibrain text_to_speech -> miplayer，mediaplayer 状态不受影响，
# 所以"暂停 mediaplayer"只掐小爱、不影响我们的播报，也不动对话引擎。
CMD_DUCK_XIAOAI = (
    "ubus -t 3 call mediaplayer player_play_operation "
    "'{\"action\":\"pause\",\"media\":\"common\"}' >/dev/null 2>&1; true"
)
CMD_TTS_TEMPLATE = '/usr/sbin/tts_play.sh "$(cat %s)"'
TTS_TMP_FILE = "/tmp/oxa_bridge_tts.txt"
# 播报完重新开麦：pnshelper 的 src=1 event=0 走 "src alarm" 分支，
# 让 mipns-xiaomi 走 mic switch:1 -> VPM_STATUS_WAKEUP -> VPM_STATUS_ASR_START
# -> "local mic on, idle ---> preparing ---> prepared!"，prepared 实测持续约 14 秒。
# 千万别用 src=3 event=7（那是 wakeup mute ON，会把唤醒彻底按死）。
CMD_WAKEUP_MIC = 'ubus -t 3 call pnshelper event_notify \'{"src":1,"event":0}\' >/dev/null 2>&1; true'
# 硬掐断：整条 mediaplayer 服务重启。实测（22:07）能立刻把小爱已排队的回答全部丢掉
# （status 1 -> 0 且不再续播），而且**不影响我们自己的播报**——我们的 TTS 走
# mibrain text_to_speech -> miplayer，独立进程；实测 TTS 播到第 3 秒时重启 mediaplayer，
# 我们的 13.5s 语音照样完整播完、exit 0。
# 唯一副作用：mediaplayer 起来后音量回到默认值，所以先读音量、重启后再写回。
CMD_KILL_XIAOAI = (
    "V=$(ubus call mediaplayer player_get_play_status | tr -d ' \\t\\n'"
    " | grep -o 'volume[^0-9]*[0-9]*' | head -1 | grep -o '[0-9]*$');"
    ' [ -z "$V" ] && V=40;'
    " /etc/init.d/mediaplayer restart >/dev/null 2>&1;"
    ' sleep 1;'
    ' ubus call mediaplayer player_set_volume "{\\"volume\\":$V,\\"media\\":\\"common\\",\\"beep\\":0}" >/dev/null 2>&1;'
    ' echo "killed volume=$V"'
)

DEFAULT_CONFIG: dict[str, Any] = {
    "server": {"host": "0.0.0.0", "port": 4399},
    "llm": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "api_key": "",
        "system_prompt": (
            "你是住在这台小米小爱音箱里的语音助手。回答要口语化、简短，"
            "控制在一两句内（不超过 80 字），不要用 markdown、列表、括号注释或表情符号。"
            "用户是用说的，所以输出必须是可以直接朗读的自然口语。"
        ),
        "max_history": 16,
        "timeout_s": 40,
    },
    "voice": {
        # 必须是说话人真会喊的词。音箱只认小米自己的唤醒词，这里匹配的是 ASR
        # 转写结果里的子串——「小爱同学，<唤醒词>，今天天气」这句话里出现唤醒词才接管。
        "wake_words": ["小智", "小智同学", "管家"],
        "interrupt_on_wake": True,
        "interrupt_mode": "duck",
        # 小爱的回答在 ASR 出结果后 0.5~2s 才真正开始播，所以单发一枪打不中：
        # 小爱的回答有时在 ASR 出结果后 4~5s 才真正开始播，所以守卫要一直压着，
        # 直到看到 Playing（说明它开口了、已被我们 pause）或到 duck_max_s 为止。
        "duck_interval_ms": 150,
        "duck_max_s": 12,
        "followup_s": 20,
        "skip_phrase_prefix": True,
        # 播报完主动发 pnshelper event 0 重新开麦，实现"不用再喊小爱同学"的连续对话
        "reopen_after_speak": True,
        "reopen_delay_ms": 150,
        # 追问窗口内也要交给小爱的"小爱本职"指令（放歌/闹钟/音量…）
        "xiaoai_keywords": [
            "播放", "放一首", "放个", "放首", "放首歌", "首歌", "来一首", "来点音乐",
            "下一首", "上一首", "暂停", "继续播放", "声音大", "声音小", "调大", "调小",
            "音量", "静音", "闹钟", "提醒", "倒计时", "日程", "备忘",
            "打电话", "发消息", "打开", "关闭", "空调", "窗帘", "灯",
        ],
        "stop_words": ["停", "停止", "别说了", "闭嘴", "退下", "不用了", "安静"],
    },
    "home_assistant": {"enabled": False, "base_url": "", "token": ""},
    # 大脑后端：deepseek（直连 API）或 coopanion（交给 Coopanion 桌面世界，带全套工具）
    "brain": {"backend": "deepseek"},
    "coopanion": {
        "host": "127.0.0.1",
        "port": 17788,
        "name": "音箱",
        # 回复者名字：Coopanion 里那个世界的 botName。留空则接受任何人的 console_send 回复。
        "owner": "",
        "quiet_ms": 1200,
        "reply_timeout_s": 120,
        "fallback": True,
    },
}

log = logging.getLogger("bridge")


# --------------------------------------------------------------------------
# WebSocket (RFC6455) 服务端实现
# --------------------------------------------------------------------------
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WSClosed(Exception):
    pass


def _accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


async def ws_handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> str:
    headers: dict[str, str] = {}
    request_line = (await reader.readline()).decode("latin-1").strip()
    while True:
        line = await reader.readline()
        if not line or line in (b"\r\n", b"\n"):
            break
        k, _, v = line.decode("latin-1").partition(":")
        headers[k.strip().lower()] = v.strip()
    key = headers.get("sec-websocket-key")
    if not key:
        raise WSClosed("no Sec-WebSocket-Key")
    resp = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {_accept_key(key)}\r\n\r\n"
    )
    writer.write(resp.encode("latin-1"))
    await writer.drain()
    return request_line


async def ws_read_frame(reader: asyncio.StreamReader) -> tuple[int, bool, bytes]:
    """返回 (opcode, fin, payload)，payload 已解掩码。"""
    hdr = await reader.readexactly(2)
    fin = bool(hdr[0] & 0x80)
    opcode = hdr[0] & 0x0F
    masked = bool(hdr[1] & 0x80)
    length = hdr[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4) if masked else None
    payload = await reader.readexactly(length) if length else b""
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, fin, payload


async def ws_read_message(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    """读一条完整消息（拼装分片），返回 (opcode, payload)。"""
    buf = bytearray()
    opcode0: Optional[int] = None
    while True:
        opcode, fin, payload = await ws_read_frame(reader)
        if opcode == OP_PING:
            raise _Ping(payload)
        if opcode == OP_CLOSE:
            raise WSClosed("close")
        if opcode0 is None:
            opcode0 = opcode
        buf += payload
        if fin:
            return opcode0 or OP_TEXT, bytes(buf)


class _Ping(Exception):
    def __init__(self, payload: bytes):
        self.payload = payload


def ws_frame(opcode: int, payload: bytes) -> bytes:
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack("!H", n)
    else:
        header.append(127)
        header += struct.pack("!Q", n)
    return bytes(header) + payload  # 服务端 -> 客户端不加掩码


# --------------------------------------------------------------------------
# 设备连接：RPC + 事件回调
# --------------------------------------------------------------------------
class Device:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, peer: str):
        self.reader = reader
        self.writer = writer
        self.peer = peer
        self.pending: dict[str, asyncio.Future] = {}
        self.on_event: Optional[Callable[[str, Any], Any]] = None
        self.on_binary: Optional[Callable[[dict], Any]] = None
        self.closed = False
        self._seq = 0
        self.last_rx = time.time()

    # ---- 发送 ----
    async def send_obj(self, obj: dict) -> None:
        if self.closed:
            raise WSClosed("already closed")
        self.writer.write(ws_frame(OP_TEXT, json.dumps(obj, ensure_ascii=False).encode()))
        await self.writer.drain()

    async def send_stream(self, tag: str, data: bytes, extra: dict | None = None) -> None:
        if self.closed:
            raise WSClosed("already closed")
        msg = {"id": str(uuid.uuid4()), "tag": tag, "bytes": list(data)}
        if extra:
            msg["data"] = extra
        self.writer.write(ws_frame(OP_BINARY, json.dumps(msg).encode()))
        await self.writer.drain()

    async def rpc(self, command: str, payload: Any = None, timeout: float = 30.0) -> dict:
        self._seq += 1
        rid = f"{int(time.time() * 1000)}-{self._seq}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        await self.send_obj({"Request": {"id": rid, "command": command,
                                         "payload": payload if payload is not None else None}})
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(rid, None)

    async def shell(self, script: str, timeout: float = 30.0) -> dict:
        """返回 {'stdout','stderr','exit_code'}"""
        res = await self.rpc("run_shell", script, timeout=timeout)
        data = res.get("data") or {}
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                data = {}
        return data

    # ---- 接收循环 ----
    async def run(self) -> None:
        try:
            while True:
                try:
                    opcode, payload = await ws_read_message(self.reader)
                except _Ping as p:
                    self.writer.write(ws_frame(OP_PONG, p.payload))
                    await self.writer.drain()
                    self.last_rx = time.time()
                    continue
                self.last_rx = time.time()
                if opcode == OP_TEXT:
                    await self._dispatch_text(payload.decode("utf-8", "replace"))
                elif opcode == OP_BINARY:
                    if self.on_binary:
                        try:
                            await self.on_binary(json.loads(payload.decode("utf-8", "replace")))
                        except Exception:
                            log.exception("binary handler failed")
        except (WSClosed, asyncio.IncompleteReadError, ConnectionResetError, OSError):
            pass
        finally:
            self.closed = True
            for fut in self.pending.values():
                if not fut.done():
                    fut.cancel()
            self.pending.clear()

    async def _dispatch_text(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except Exception:
            log.warning("非 JSON 文本帧: %s", text[:200])
            return
        if "Response" in msg:
            resp = msg["Response"]
            fut = self.pending.get(str(resp.get("id")))
            if fut and not fut.done():
                fut.set_result(resp)
            return
        if "Event" in msg:
            ev = msg["Event"]
            name = ev.get("event")
            log.debug("event %s: %s", name, str(ev.get("data"))[:400])
            if self.on_event:
                try:
                    await self.on_event(name, ev.get("data"))
                except Exception:
                    log.exception("event handler failed (%s)", name)
            return
        if "Request" in msg:
            # 设备端发来的请求（本桥接不主动处理，回空成功）
            req = msg["Request"]
            await self.send_obj({"Response": {"id": req.get("id"), "code": 0, "msg": "ok"}})


# --------------------------------------------------------------------------
# DeepSeek 大脑（http.client 走线程，零依赖）
# --------------------------------------------------------------------------
class Brain:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.key = os.environ.get("DEEPSEEK_API_KEY", "") or cfg.get("api_key", "")
        self.base = (os.environ.get("DEEPSEEK_BASE_URL") or cfg.get("base_url") or "https://api.deepseek.com").rstrip("/")
        self.model = cfg.get("model", "deepseek-chat")
        self.system = cfg.get("system_prompt", "")
        self.max_history = int(cfg.get("max_history", 16))
        self.timeout = float(cfg.get("timeout_s", 40))
        self.history: list[dict] = []

    @property
    def live(self) -> bool:
        return bool(self.key)

    async def ask(self, text: str) -> str:
        if not self.live:
            return f"（本地回显模式）我听到了：{text}"
        try:
            return await asyncio.to_thread(self._ask_sync, text)
        except Exception as e:
            log.exception("DeepSeek 调用失败")
            return f"抱歉，大模型那边出了点问题：{type(e).__name__}"

    def _system_now(self) -> str:
        """每次提问都把当前时间塞进 system，避免模型对“明天/现在”一问三不知。"""
        from datetime import datetime

        now = datetime.now().astimezone()
        week = "周一周二周三周四周五周六周日"[now.weekday() * 2: now.weekday() * 2 + 2]
        stamp = now.strftime("%Y年%m月%d日") + " " + week + now.strftime(" %H:%M")
        return (f"{self.system}\n当前时间：{stamp}。"
                "用户是用语音提问，回答要能直接念出来；不确定的事就说不确定，不要编。")

    def _ask_sync(self, text: str) -> str:
        import http.client
        import urllib.parse

        url = urllib.parse.urlparse(self.base)
        conn_cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        path = (url.path.rstrip("/") if url.path else "") + "/chat/completions"
        msgs = [{"role": "system", "content": self._system_now()}]
        msgs += self.history[-self.max_history:]
        msgs.append({"role": "user", "content": text})
        body = json.dumps({"model": self.model, "messages": msgs, "stream": False,
                          "temperature": 0.6, "max_tokens": 400}).encode()
        conn = conn_cls(url.hostname, url.port or (443 if url.scheme == "https" else 80), timeout=self.timeout)
        try:
            conn.request("POST", path, body=body, headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            })
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", "replace")
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}: {raw[:200]}")
            data = json.loads(raw)
            answer = data["choices"][0]["message"]["content"].strip()
        finally:
            conn.close()
        self.history.append({"role": "user", "content": text})
        self.history.append({"role": "assistant", "content": answer})
        self.history = self.history[-self.max_history:]
        return answer


# --------------------------------------------------------------------------
# Coopanion 大脑（把话交给 Coopanion 里的一个 World，由它带全部工具来回答）
# --------------------------------------------------------------------------
class CoopBrain:
    """把用户的话注入 Coopanion 控制台，取回它用 console_send 说的话。

    与 Brain 保持同一接口（ask/live/model），所以 respond() 不用改。
    失败时可以回落到 DeepSeek 直连（fallback=True，默认开），保证音箱永远有回应。
    """

    def __init__(self, cfg: dict, fallback_cfg: Optional[dict] = None):
        from coop import ConsoleClient  # 同目录模块

        self.cfg = cfg
        self.model = cfg.get("model") or "coopanion"
        self.client = ConsoleClient(cfg)
        self.fallback = Brain(fallback_cfg) if (cfg.get("fallback", True) and fallback_cfg) else None
        self.last_error = ""
        self.stats = {"ok": 0, "fail": 0, "fallback": 0}

    @property
    def live(self) -> bool:
        return True

    async def ask(self, text: str) -> str:
        try:
            answer = await self.client.ask(text)
            self.stats["ok"] += 1
            return answer
        except Exception as e:
            self.stats["fail"] += 1
            self.last_error = f"{type(e).__name__}: {e}"
            log.warning("Coopanion 调用失败: %s", self.last_error)
            try:
                await self.client.close()
            except Exception:
                pass
            if self.fallback is not None and self.fallback.live:
                self.stats["fallback"] += 1
                log.info("↩️  回落到 DeepSeek 直连")
                return await self.fallback.ask(text)
            return "抱歉，我这边脑子暂时连不上了。"


# --------------------------------------------------------------------------
# 桥接主体
# --------------------------------------------------------------------------
class Bridge:
    def __init__(self, cfg: dict, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.devices: list[Device] = []
        self.brain = self._make_brain(cfg)
        self.wake_words = cfg.get("voice", {}).get("wake_words", ["小智"])
        self.speaking = False
        self.speak_task: Optional[asyncio.Task] = None
        # 追问窗口：答完 N 秒内的下一句，即使没喊名字也算在跟我们说话
        self.followup_s = float(cfg.get("voice", {}).get("followup_s", 20))
        self.last_turn_at = 0.0
        # 唤醒后多少秒内说的话算跟我们说（用户每次必须先喊"小爱同学"）
        self.wake_window_s = float(cfg.get("voice", {}).get("wake_window_s", 15))
        self.wake_at = 0.0
        # ASR 健康自检（2026-11-09 02:29 实证）：设备长时间运行（当时 uptime 13 天）后
        # mipns 仍在上行音频，但 aivs 引擎不再把 binary 发给云端 → 云端永不回
        # RecognizeResult → 7 秒 first_round_7s_no_asr_timeout → 强制
        # Dialog.ExitContinuousDialog + mic switch: 0（现象就是"喊完小爱同学立刻闭嘴"）。
        # reboot 后同一套代码恢复正常，所以这里只做检测/告警，不自动重启。
        self._wake_without_asr = 0      # 连续"唤醒了但没识别出任何字"的次数
        self._last_health_warn = 0.0
        self._asr_health_rounds = int(cfg.get("health", {}).get("asr_dead_rounds", 3))
        self._asr_auto_reboot = bool(cfg.get("health", {}).get("auto_reboot", False))
        self._wake_pending_at = 0.0     # 唤醒时刻；10 秒内没等到文本就判定这轮 ASR 废了

    @staticmethod
    def _make_brain(cfg: dict):
        """backend=coopanion 时用 Coopanion，否则走 DeepSeek 直连。"""
        llm = cfg.get("llm", {})
        backend = str(cfg.get("brain", {}).get("backend") or llm.get("backend") or "deepseek").lower()
        if backend == "coopanion":
            coop_cfg = dict(cfg.get("coopanion", {}))
            coop_cfg.setdefault("fallback", True)
            log.info("🧠 大脑后端: Coopanion @ %s:%s",
                     coop_cfg.get("host", "127.0.0.1"), coop_cfg.get("port", 17788))
            return CoopBrain(coop_cfg, fallback_cfg=llm)
        return Brain(llm)

    # ---- 设备接入 ----
    async def handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = "%s:%s" % writer.get_extra_info("peername")[:2]
        try:
            await ws_handshake(reader, writer)
        except Exception as e:
            log.warning("握手失败 %s: %s", peer, e)
            writer.close()
            return
        dev = Device(reader, writer, peer)
        dev.on_event = lambda name, data: self.on_event(dev, name, data)
        dev.on_binary = lambda msg: self.on_binary(dev, msg)
        self.devices.append(dev)
        log.info("✅ 设备已连接: %s", peer)
        # 先起读循环，否则握手后立刻发的 RPC 响应没人读，会一直超时
        run_task = asyncio.create_task(dev.run())
        try:
            ver = await dev.rpc("get_version", None, timeout=6)
            log.info("   设备 client 版本: %s", ver.get("data"))
        except Exception as e:
            log.warning("   get_version 无响应: %s", type(e).__name__)
        try:
            await run_task
        finally:
            run_task.cancel()
            if dev in self.devices:
                self.devices.remove(dev)
            writer.close()
            log.info("❌ 设备已断开: %s", peer)

    async def on_binary(self, dev: Device, msg: dict) -> None:
        tag = msg.get("tag")
        n = len(msg.get("bytes") or [])
        log.debug("binary stream tag=%s bytes=%s", tag, n)
        # 占位：后续接 ASR/TTS 音频管线时在此处理 record 流

    # ---- 事件 ----
    async def on_event(self, dev: Device, name: str, data: Any) -> None:
        if name == "instruction":
            await self.on_instruction(dev, data)
        elif name == "playing":
            log.info("🎵 播放状态: %s", data)
            # 打断守卫期间看到 Playing = 小爱真的开口了（下一刻就会被我们 pause 掉）
            if str(data).lower() == "playing" and getattr(self, "_duck_task", None):
                self._duck_playing_at = time.time()
        elif name == "kws":
            log.info("👂 KWS: %s", data)

    async def on_instruction(self, dev: Device, data: Any) -> None:
        if not isinstance(data, dict):
            return
        line_raw = data.get("NewLine")
        if not line_raw:
            return
        try:
            line = json.loads(line_raw)
        except Exception:
            return
        header = line.get("header") or {}
        if header.get("namespace") != "SpeechRecognizer" or header.get("name") != "RecognizeResult":
            return
        payload = line.get("payload") or {}
        results = payload.get("results") or []
        text = (results[0].get("text") if results else "") or ""
        is_vad_begin = bool(payload.get("is_vad_begin"))
        is_final = bool(payload.get("is_final"))

        if not text and not is_vad_begin:
            # 唤醒词触发（小爱被叫醒但还没有识别文本）
            log.info("👂 检测到唤醒")
            if self.speaking:
                # 正在播报时自己的声音会被麦克风听见，忽略，避免自激
                log.info("   (正在播报，忽略这次唤醒)")
                return
            # 真机日志实证（22:10:23.621 识别到 → 22:10:23.678 唤醒，相隔 57ms）：
            # 每识别完一句，引擎都会立刻回一条空结果事件，这**不是**用户又喊了一声。
            # 以前这里无条件 stop_duck()，等于每轮刚开口就把自己的打断守卫撤掉，
            # 小爱的回答于是畅通无阻——这才是"两个一起说"的真凶。
            if getattr(self, "_turn_active", False):
                log.info("   (上一轮还在进行中，这条空事件忽略，守卫保留)")
                return
            # 用户每次都得先说"小爱同学"，所以把唤醒当成"这一步要跟我们说话"的起点：
            # 唤醒后 wake_window 秒内的第一句话默认交给我们（小爱本职指令除外）。
            # 注意：这里绝对不能 cancel 正在思考的那一轮，否则话还没说就被自己掐掉了。
            self.stop_duck()  # 新一轮开始，先撤掉上一轮的打断守卫
            self.wake_at = time.time()
            # 记下"这次唤醒之后该有字"；10 秒内没等到 is_final 文本就记一次 ASR 异常
            self._wake_pending_at = time.time()
            return

        if not text:
            return

        if is_final:
            self._wake_pending_at = 0.0     # 这一轮识别成功，ASR 正常
            self._wake_without_asr = 0
            log.info("🗣  识别到: %s", text)
            await self.on_command(dev, text)
        else:
            log.debug("   (中间结果) %s", text)

    # ---- 指令 ----
    def _match_wake(self, text: str) -> Optional[str]:
        for w in self.wake_words:
            if w and w in text:
                return w
        return None

    def _looks_like_xiaoai(self, text: str) -> bool:
        """判断这句是不是小爱的本职（放歌、闹钟、音量、家电控制…），是就别抢。"""
        for kw in self.cfg["voice"].get("xiaoai_keywords") or []:
            if kw and kw in text:
                return True
        return False

    def _is_stop(self, text: str) -> bool:
        t = (text or "").strip("，。！？、,.!? ")
        for w in self.cfg["voice"].get("stop_words") or []:
            if w and (t == w or t.endswith(w)):
                return True
        return False

    async def on_command(self, dev: Device, text: str) -> None:
        if self.speaking:
            # 播报时麦克风听到的是我们自己，忽略，避免"自己跟自己对话"的死循环
            log.info("🔇 播报中，忽略这句: %s", text)
            return
        if self._is_stop(text):
            log.info("🛑 停止播报指令: %s", text)
            self.stop_speaking()
            return
        wake = self._match_wake(text)
        armed = (time.time() - self.wake_at) <= self.wake_window_s
        followup = (time.time() - self.last_turn_at) <= self.followup_s
        if wake:
            pass
        elif self._looks_like_xiaoai(text):
            log.info("   → 这是小爱的本职指令，交给小爱")
            self.stop_duck()  # 让路：别把我们自己压着，小爱要说话/放歌
            self.wake_at = 0.0
            return
        elif armed:
            log.info("   → 唤醒后的第一句（%.0f 秒内），交给我们", self.wake_window_s)
        elif followup:
            log.info("   → 追问窗口内（%.0f 秒），继续交给我们", self.followup_s)
        else:
            log.info("   → 未命中唤醒词 %s，交给小爱", self.wake_words)
            self.stop_duck()
            return
        user_text = text
        if wake and self.cfg["voice"].get("skip_phrase_prefix", True) and user_text.startswith(wake):
            user_text = user_text[len(wake):].lstrip("，,。.、 ")
        if not user_text:
            user_text = text
        self.wake_at = 0.0
        self.last_turn_at = time.time()
        self._enqueue_turn(dev, user_text)

    # ---- 单轮串行 ----
    # 真机证据（2026-11-09 01:01:14→01:01:15）：用户连着说两句，同一秒内进了两个
    # is_final（两个不同 dialog_id），这里连开两个 respond → 两路 tts_play.sh 并行，
    # 01:01:19 与 01:01:25 两次「🔈 开始播报」叠着放。设备侧听到重叠播报后，在
    # 01:01:31.053 下发 Dialog.ExitContinuousDialog（force_exit:1）直接退出连续对话。
    # 修法：同一时刻只允许一轮在跑，后到的一句排队（只保留最新一句，避免堆积）。
    def _enqueue_turn(self, dev: Device, text: str) -> None:
        task = self.speak_task
        if task and not task.done() and getattr(self, "_turn_active", False):
            self._pending_text = text
            log.info("⏳ 上一轮还没结束，这句先排队: %s", text)
            return
        self._pending_text = None
        self.speak_task = asyncio.create_task(self._run_turn(dev, text))

    async def _run_turn(self, dev: Device, text: str) -> None:
        try:
            await self.respond(dev, text)
        finally:
            pending = getattr(self, "_pending_text", None)
            self._pending_text = None
            if pending:
                log.info("⏭  接着处理排队的一句: %s", pending)
                self.speak_task = asyncio.create_task(self._run_turn(dev, pending))

    def interrupt_cmd(self) -> str:
        mode = str(self.cfg["voice"].get("interrupt_mode") or "duck").lower()
        return CMD_RECOVER_XIAOAI if mode == "restart" else CMD_DUCK_XIAOAI

    async def respond(self, dev: Device, user_text: str) -> None:
        t0 = time.time()
        try:
            # 1) 开一个"打断守卫"：整轮期间反复暂停 mediaplayer，直到我们说完
            self.start_duck(dev)
            log.info("🧠 问 %s: %s", getattr(self.brain, "model", "模型"), user_text)
            answer = await self.brain.ask(user_text)
            log.info("💬 回答(%.1fs): %s", time.time() - t0, answer)
            self.speaking = True
            await self.speak(dev, answer)
            # 2) 播报完把麦克风重新打开，形成真正的"连续对话"：
            #    设备侧 pnshelper event 0（src=1 alarm 分支）会走 mic switch:1 →
            #    VPM_STATUS_ASR_START → "local mic on, idle ---> preparing ---> prepared"，
            #    实测 prepared 状态维持约 14 秒（02:05:54.39→02:06:08.45），够用户接一句。
            #    注意：绝不能发 event 7（那是 wakeup mute ON，会把唤醒彻底关掉）。
            if bool(self.cfg["voice"].get("reopen_after_speak", True)):
                await self.reopen_mic(dev)
        except asyncio.CancelledError:
            log.info("⏹  本轮被打断")
            raise
        except Exception:
            log.exception("respond 失败")
        finally:
            self.speaking = False
            self.last_turn_at = time.time()
            # 守卫不在这里停：它要一直压到"我们说完再守 2.5 秒"，否则小爱晚开口会漏
            self._turn_active = False
            self._turn_end_at = time.time()

    async def reopen_mic(self, dev: Device) -> None:
        """播报完把麦克风重新打开，让用户能接着说话（真正的连续对话）。

        设备上的 pnshelper event_notify {"src":1,"event":0} 走的是 "src alarm" 分支，
        实测会让 mipns-xiaomi 依次执行：
            notify_proc_dispatch mic switch: 1
            → mipns_asr_data_start / VPM_STATUS_WAKEUP / VPM_STATUS_ASR_START
            → "local mic on, idle ---> preparing ---> prepared!"
        并保持 prepared 约 14 秒（02:05:54.39 → 02:06:08.45），期间 recorder 正常出数据。
        """
        if self.dry_run:
            return
        delay = float(self.cfg["voice"].get("reopen_delay_ms", 150)) / 1000.0
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            res = await dev.shell(CMD_WAKEUP_MIC, timeout=10)
            log.info("🎤 重新开麦（exit=%s），用户可以直接接着说", res.get("exit_code"))
        except Exception as e:
            log.warning("重新开麦失败: %s", e)

    async def speak(self, dev: Device, text: str) -> None:
        if self.dry_run:
            log.info("🔈 [dry-run] 本应播报: %s", text)
            return
        # 设备上没有 base64 命令；用单引号转义后直接喂给 tts_play.sh（其用法：tts_play.sh "文本"）
        safe = (text or "").strip().replace("'", "'\\''")
        if not safe:
            return
        # 必须带 -n：让 tts_play.sh 往 pnshelper 发 event 14/15（播报开始/结束），
        # 设备才会走 vpm_engine_set_tts_start/end + process_flexkws_set_tts_start/end，
        # 播放期间关掉唤醒词引擎，避免音箱听见自己的声音（幽灵唤醒）、也避免冲突。
        script = f"/usr/sbin/tts_play.sh -n '{safe}'"
        log.info("🔈 开始播报: %s", safe[:40])
        res = await dev.shell(script, timeout=180)
        log.info("🔈 TTS exit=%s stdout=%s stderr=%s", res.get("exit_code"),
                 (res.get("stdout") or "").strip()[:120], (res.get("stderr") or "").strip()[:120])

    # ---- 打断守卫：整轮反复 pause，专治"小爱和我们同时说话" ----
    def start_duck(self, dev: Device) -> None:
        if self.dry_run:
            return
        self.stop_duck()
        self._duck_playing_at = 0.0
        self._turn_active = True
        self._turn_end_at = 0.0
        self._duck_task = asyncio.create_task(self._duck_loop(dev))

    def stop_duck(self) -> None:
        task = getattr(self, "_duck_task", None)
        self._duck_task = None
        self._turn_active = False
        if task and not task.done():
            task.cancel()

    async def _duck_loop(self, dev: Device) -> None:
        voice = self.cfg["voice"]
        mode = str(voice.get("interrupt_mode") or "kill").lower()
        interval = max(0.1, float(voice.get("duck_interval_ms", 200)) / 1000)
        post_s = float(voice.get("duck_post_s", 4))
        max_s = float(voice.get("duck_max_s", 60))
        max_kills = int(voice.get("max_kills_per_turn", 4))
        start = time.time()
        kills = 0
        pauls = 0
        try:
            if mode == "restart":
                await dev.shell(CMD_RECOVER_XIAOAI, timeout=25)
                return
            while True:
                now = time.time()
                if now - start > max_s:
                    log.info("⌛ 打断守卫到上限退出（kill %d / pause %d）", kills, pauls)
                    return
                turn_active = bool(getattr(self, "_turn_active", False))
                end_at = float(getattr(self, "_turn_end_at", 0.0) or 0.0)
                if not turn_active and end_at and now - end_at > post_s:
                    log.info("✅ 打断守卫收工（kill %d / pause %d，%s）", kills, pauls,
                             "掐掉过小爱的回答" if (kills or pauls) else "本轮没听到小爱开口")
                    return
                playing = float(getattr(self, "_duck_playing_at", 0.0) or 0.0)
                if playing and (kills + pauls) < max_kills:
                    # 小爱开口了：整条 mediaplayer 重启，连它后面排队的分段一起丢掉
                    self._duck_playing_at = 0.0
                    if mode in ("kill", "hybrid"):
                        kills += 1
                        log.info("⚔️  小爱要插话 → 掐断 mediaplayer（第 %d 次）", kills)
                        res = await dev.shell(CMD_KILL_XIAOAI, timeout=30)
                        log.info("⚔️  掐断结果: %s", (res.get("stdout") or "").strip()[:80])
                    else:
                        pauls += 1
                        log.info("✋ 小爱要插话 → pause（第 %d 次）", pauls)
                        await dev.shell(CMD_DUCK_XIAOAI, timeout=10)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            log.info("🛑 打断守卫被取消（kill %d / pause %d）", kills, pauls)
            raise
        except Exception as e:
            log.warning("打断守卫异常: %s", e)

    def stop_speaking(self) -> None:
        if self.speak_task and not self.speak_task.done():
            self.speak_task.cancel()
        self.speaking = False

    # ---- 管理口（给本机脚本下发指令用，只绑 127.0.0.1）----
    async def admin_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        resp: dict[str, Any] = {}
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=10)
            req = json.loads(raw.decode("utf-8", "replace") or "{}")
            dev = self.devices[-1] if self.devices else None
            if req.get("status"):
                # 状态查询不依赖设备在线，否则音箱没连上来时就看不到「桥接活着」
                resp = {"ok": True, "devices": [d.peer for d in self.devices],
                        "llm": "live" if self.brain.live else "local-echo",
                        "model": self.brain.model, "speaking": self.speaking,
                        "brain": type(self.brain).__name__,
                        "brain_stats": getattr(self.brain, "stats", None),
                        "brain_error": getattr(self.brain, "last_error", ""),
                        "wake_without_asr": self._wake_without_asr}
            elif dev is None:
                resp = {"ok": False, "error": "没有设备连接"}
            elif "run_shell" in req:
                dev_data = await dev.shell(req["run_shell"], timeout=float(req.get("timeout", 60)))
                resp = {"ok": True, "result": dev_data}
            elif "speak" in req:
                asyncio.create_task(self.speak(dev, req["speak"]))
                resp = {"ok": True, "queued": req["speak"]}
            elif "ask" in req:
                resp = {"ok": True, "answer": await self.brain.ask(req["ask"])}
            elif req.get("recover"):
                data = await dev.shell(CMD_RECOVER_XIAOAI, timeout=25)
                resp = {"ok": data.get("exit_code") == 0, "result": data}
            else:
                resp = {"ok": False, "error": "未知请求"}
        except Exception as e:
            resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        try:
            writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode())
            await writer.drain()
        except Exception:
            pass
        writer.close()

    async def _check_asr_health(self, now: float) -> None:
        """设备侧 ASR 死掉时主动告警。

        实证（2026-11-09 02:29，uptime 13 天）：设备被唤醒后 `mipns` 照常推音频，
        但 aivs 引擎不再发 binary 给云端，云端一个 RecognizeResult 都不回，7 秒后
        设备自己 `first_round_7s_no_asr_timeout` → `Dialog.ExitContinuousDialog`
        → `mic switch: 0`。用户侧表现就是"喊了小爱同学，不等下一句就把麦关了"。
        reboot 立刻恢复，所以这里只告警不自动重启（auto_reboot 默认关）。
        """
        # 唤醒后 10 秒仍没等到任何 final 文本 → 这一轮 ASR 废了
        if self._wake_pending_at and (now - self._wake_pending_at) > 10:
            self._wake_pending_at = 0.0
            self._wake_without_asr += 1
            log.warning("⚠️  唤醒后 10 秒没识别到任何字（连续第 %d 次）", self._wake_without_asr)
        if self._wake_without_asr < self._asr_health_rounds:
            return
        if now - self._last_health_warn < 120:
            return
        self._last_health_warn = now
        log.error("🚨 设备 ASR 疑似挂了：连续 %d 次唤醒都没识别出文字。"
                  "典型原因是音箱长时间运行后 aivs 引擎不再上行音频，"
                  "重启音箱（reboot）即可恢复。", self._wake_without_asr)
        if self._asr_auto_reboot and self.devices:
            log.error("   已配置 auto_reboot，正在重启音箱…")
            try:
                await self.devices[0].shell("sync; reboot", timeout=10)
            except Exception as e:
                log.warning("   重启失败: %s", type(e).__name__)

    async def heartbeat(self) -> None:
        """踢掉断电/重连后残留的僵尸连接，避免管理口拿到失效设备；顺带做 ASR 健康自检。"""
        while True:
            await asyncio.sleep(15)
            now = time.time()
            await self._check_asr_health(now)
            for dev in list(self.devices):
                if dev.closed or (now - dev.last_rx) > 45:
                    log.info("🧹 清理僵尸连接: %s (%.0fs 无数据)", dev.peer, now - dev.last_rx)
                    dev.closed = True
                    try:
                        dev.writer.close()
                    except Exception:
                        pass
                    if dev in self.devices:
                        self.devices.remove(dev)
                    continue
                try:
                    dev.writer.write(ws_frame(OP_PING, b"hb"))
                    await dev.writer.drain()
                except Exception:
                    dev.closed = True

    # ---- 启动 ----
    async def serve(self) -> None:
        srv_cfg = self.cfg.get("server", {})
        host, port = srv_cfg.get("host", "0.0.0.0"), int(srv_cfg.get("port", 4399))
        server = await asyncio.start_server(self.handle_connection, host, port)
        admin_port = int(srv_cfg.get("admin_port", 4400))
        admin = await asyncio.start_server(self.admin_conn, "127.0.0.1", admin_port)
        log.info("🚀 桥接已启动 ws://%s:%d  admin=127.0.0.1:%d  (大脑: %s, model=%s)",
                 host, port, admin_port, "已配置" if self.brain.live else "缺失-key-本地回显", self.brain.model)
        async with server, admin:
            await asyncio.gather(server.serve_forever(), admin.serve_forever(),
                                 self.heartbeat())


# --------------------------------------------------------------------------
def setup_logging(verbose: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Windows 控制台默认 GBK，日志里的 emoji/中文会炸；强制 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    fmt = "%(asctime)s %(levelname)-7s %(message)s"
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_DIR / f"bridge-{time.strftime('%Y%m%d')}.log", encoding="utf-8"),
        ],
    )


def load_config(path: Optional[str]) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path and Path(path).exists():
        user = json.loads(Path(path).read_text(encoding="utf-8"))
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def main() -> None:
    global LOG_DIR
    ap = argparse.ArgumentParser(description="Open-XiaoAI 桥接服务")
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--log-dir", default=None, help="日志目录（默认脚本同目录下的 logs/）")
    ap.add_argument("--dry-run", action="store_true", help="不真的调用设备 shell / 只打印")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    if args.log_dir:
        LOG_DIR = Path(args.log_dir)
    setup_logging(args.verbose)
    cfg = load_config(args.config)
    if args.port:
        cfg["server"]["port"] = args.port
    bridge = Bridge(cfg, dry_run=args.dry_run)
    try:
        asyncio.run(bridge.serve())
    except KeyboardInterrupt:
        log.info("bye")


if __name__ == "__main__":
    main()
