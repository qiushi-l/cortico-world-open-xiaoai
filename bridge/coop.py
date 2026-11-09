#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Coopanion 控制台客户端（纯标准库）
----------------------------------
把「往 Coopanion 控制台投一句话、把 bot 用 console_send 回的那句话取回来」封装成一个
可以直接 `await` 的函数，供桥接替换 DeepSeek 直连用。

通道：ws://127.0.0.1:17788/ws/providers/world%3Aconsole/panels/chat
  ->  服务端 sys: "已连接。请发送 {type:"hello", name:"你的名字"} ..."
  <-  {"type":"hello","name":"音箱"}
  ->  sys: "你好,音箱。"   随后回放历史消息（带 "history": true）
  <-  {"type":"msg","text":"..."}     ← 注入
  ->  {"type":"msg","from":"<bot>","text":"..."}  ← console_send 回复

要点：
  * hello 之后会回放历史，必须跳过带 history 的帧，否则会把旧消息当成本轮回复。
  * Coopanion 的 bot 可能默认用 pet_say 说话（桌宠气泡 + PC 喇叭），所以注入文本里必须写明
    "请用 console_send 回复"，否则收不到回包。
  * 一条回复可能分多帧到达，用「首帧之后再静默 quiet_ms 就收工」来拼。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import time
import uuid

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA

DEFAULT_CONSOLE_PATH = "/ws/providers/world%3Aconsole/panels/chat"


# --------------------------------------------------------------------------
# 最小 WebSocket 客户端（客户端 -> 服务端必须加掩码）
# --------------------------------------------------------------------------
def _client_frame(opcode: int, payload: bytes) -> bytes:
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack("!H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack("!Q", n)
    return bytes(header) + mask + masked


async def _read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    hdr = await reader.readexactly(2)
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
    return opcode, payload


async def ws_connect(host: str, port: int, path: str, origin: str | None = None):
    reader, writer = await asyncio.open_connection(host, port)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
    )
    if origin:
        req += f"Origin: {origin}\r\n"
    req += "\r\n"
    writer.write(req.encode())
    await writer.drain()
    status = await reader.readline()
    if b"101" not in status:
        writer.close()
        raise RuntimeError(f"handshake failed on {path}: {status!r}")
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
    return reader, writer


# --------------------------------------------------------------------------
# 控制台客户端
# --------------------------------------------------------------------------
class ConsoleClient:
    """一条长连接，串行提问。断线自动重连。"""

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.host = cfg.get("host", "127.0.0.1")
        self.port = int(cfg.get("port", 17788))
        self.path = cfg.get("path", DEFAULT_CONSOLE_PATH)
        self.name = cfg.get("name", "音箱")
        self.owner = cfg.get("owner", "")
        # 首帧之后再静默这么久，就认为这条回复说完了
        self.quiet_ms = int(cfg.get("quiet_ms", 1200))
        self.connect_timeout = float(cfg.get("connect_timeout_s", 10))
        self.reply_timeout = float(cfg.get("reply_timeout_s", 120))
        self.prefix = cfg.get(
            "prefix",
            "（来自小米音箱，是主人在用语音跟你说话。"
            "请只用 console_send 回复，不要说多余的话，也别调其它工具，"
            "回复要口语化、能直接念出来，控制在两句话以内）",
        )
        # 每次提问都补一句真实时间：模型自己算"明天上午"经常错一天，
        # 在这里钉死"现在几点"，设闹钟/约时间才不会差一天。
        self.time_hint = cfg.get("time_hint", True)

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._frames: asyncio.Queue = asyncio.Queue()
        self._pump_task: asyncio.Task | None = None
        self._connected = False
        self.last_error = ""

    # ---- 连接管理 ----
    @property
    def online(self) -> bool:
        return self._connected and self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        if self.online:
            return
        origin = f"http://{self.host}:{self.port}"
        reader, writer = await asyncio.wait_for(
            ws_connect(self.host, self.port, self.path, origin), timeout=self.connect_timeout
        )
        self._reader, self._writer = reader, writer
        self._frames = asyncio.Queue()
        self._connected = True
        # 起读循环
        if self._pump_task and not self._pump_task.done():
            self._pump_task.cancel()
        self._pump_task = asyncio.create_task(self._pump())

        # 握手：等 sys -> 发 hello -> 等 sys 问候
        await self._wait_sys(5)
        await self._send({"type": "hello", "name": self.name})
        await self._wait_sys(5)

    async def close(self) -> None:
        self._connected = False
        if self._pump_task and not self._pump_task.done():
            self._pump_task.cancel()
        if self._writer is not None:
            try:
                self._writer.write(_client_frame(OP_CLOSE, b""))
                await self._writer.drain()
            except Exception:
                pass
            try:
                self._writer.close()
            except Exception:
                pass
        self._reader = self._writer = None

    async def _pump(self) -> None:
        """把服务端来的帧塞进队列；控制帧就地处理。"""
        assert self._reader is not None
        try:
            while True:
                opcode, payload = await _read_frame(self._reader)
                if opcode == OP_PING:
                    self._writer.write(_client_frame(OP_PONG, payload))
                    await self._writer.drain()
                    continue
                if opcode == OP_CLOSE:
                    break
                if opcode not in (OP_TEXT, OP_BINARY):
                    continue
                try:
                    obj = json.loads(payload.decode("utf-8", "replace"))
                except Exception:
                    continue
                await self._frames.put(obj)
        except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
            pass
        except asyncio.CancelledError:
            raise
        finally:
            self._connected = False

    async def _send(self, obj: dict) -> None:
        if self._writer is None:
            raise RuntimeError("未连接")
        self._writer.write(_client_frame(OP_TEXT, json.dumps(obj, ensure_ascii=False).encode()))
        await self._writer.drain()

    async def _wait_sys(self, timeout: float) -> dict:
        """跳过（并丢弃）历史回放，直到拿到一条 sys/非历史帧。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                obj = await asyncio.wait_for(self._frames.get(), timeout=max(0.1, deadline - time.time()))
            except asyncio.TimeoutError:
                break
            if obj.get("history"):
                continue
            if obj.get("type") != "msg":
                return obj
        return {}

    # ---- 主入口 ----
    def _now_hint(self) -> str:
        from datetime import datetime

        now = datetime.now()
        week = "周一周二周三周四周五周六周日"[now.weekday() * 2: now.weekday() * 2 + 2]
        return f"（现在的时间是 {now.strftime('%Y-%m-%d %H:%M')} {week}）"

    async def ask(self, text: str) -> str:
        """投一句话，返回 bot 用 console_send 说的话。失败抛异常。"""
        text = (text or "").strip()
        if not text:
            return ""
        async with self._lock:
            if not self.online:
                await self.connect()
            # 清掉可能残留的旧帧
            while not self._frames.empty():
                self._frames.get_nowait()
            payload = f"{self.prefix}{text}"
            if self.time_hint:
                payload += self._now_hint()
            t0 = time.time()
            await self._send({"type": "msg", "text": payload})
            reply = await self._collect(deadline=time.time() + self.reply_timeout)
            if not reply:
                raise TimeoutError(f"bot {time.time() - t0:.0f}s 内没有用 console_send 回复")
            return reply

    async def _collect(self, deadline: float) -> str:
        parts: list[str] = []
        quiet = self.quiet_ms / 1000.0
        while time.time() < deadline:
            wait = quiet if parts else min(quiet, max(0.05, deadline - time.time()))
            try:
                obj = await asyncio.wait_for(self._frames.get(), timeout=wait)
            except asyncio.TimeoutError:
                if parts:  # 已经开始回了，静默够久 -> 收工
                    break
                if time.time() >= deadline:
                    break
                continue
            if obj.get("history"):
                continue
            if obj.get("type") != "msg":
                continue
            who = obj.get("from")
            if who == self.name:  # 自己的回声
                continue
            if self.owner and who and who != self.owner:
                continue
            chunk = (obj.get("text") or "").strip()
            if chunk:
                parts.append(chunk)
        return "".join(parts).strip()


# --------------------------------------------------------------------------
# 便捷函数（一次性用法）
# --------------------------------------------------------------------------
async def ask_once(text: str, cfg: dict | None = None) -> str:
    c = ConsoleClient(cfg)
    try:
        await c.connect()
        return await c.ask(text)
    finally:
        await c.close()
