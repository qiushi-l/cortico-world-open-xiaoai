#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本机运维小工具：通过桥接的管理口（127.0.0.1:4400）指挥音箱。

用法：
  python devctl.py status
  python devctl.py shell "uname -a"
  python devctl.py speak "音箱已接入桥接服务"
  python devctl.py ask "用一句话介绍你自己"
"""
from __future__ import annotations

import argparse
import json
import socket


def call(req: dict, port: int = 4400, timeout: float = 60) -> dict:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode("utf-8", "replace") or "{}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=4400)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p_shell = sub.add_parser("shell")
    p_shell.add_argument("script")
    p_shell.add_argument("--timeout", type=float, default=60)
    p_shellf = sub.add_parser("shellf", help="把本地文件内容当作脚本发到音箱执行（避免引号转义地狱）")
    p_shellf.add_argument("path")
    p_shellf.add_argument("--timeout", type=float, default=60)
    p_speak = sub.add_parser("speak")
    p_speak.add_argument("text")
    p_ask = sub.add_parser("ask")
    p_ask.add_argument("text")
    sub.add_parser("recover", help="小爱被卡住（唤不醒）时重启 mico_aivs_lab 兜底")
    args = ap.parse_args()

    if args.cmd == "status":
        req = {"status": True}
    elif args.cmd == "recover":
        req = {"recover": True}
    elif args.cmd == "shell":
        req = {"run_shell": args.script, "timeout": args.timeout}
    elif args.cmd == "shellf":
        with open(args.path, "r", encoding="utf-8") as f:
            req = {"run_shell": f.read(), "timeout": args.timeout}
    elif args.cmd == "speak":
        req = {"speak": args.text}
    else:
        req = {"ask": args.text}
    print(json.dumps(call(req, args.port), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
