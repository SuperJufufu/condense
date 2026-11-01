#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
condense.py 的离线回归测试 —— 不需要 GPU、不需要真的 Ollama。

用一个假 Ollama 服务器（标准库 http.server）模拟：
  * 中文日志的真实 token 密度比先验高得多（REAL_TPC = 2.4 token/字符）；
  * prompt 超过 num_ctx 时返回和 Ollama 一样的
    HTTP 400 "request (N tokens) exceeds the available context size (M tokens)"。

验证 4 件事（这正是 2026-10-06 那个未修 bug 的验收标准）：
  1. 密度先验：中文文本的先验明显高于纯 ASCII；
  2. 密度校准：一次真实调用后，估算密度被实测值顶上去了；
  3. Map / Reduce 阶段撞上下文时能二分重跑，而不是整个任务 die()；
  4. 全程有界收敛（调用次数不会爆炸），并且真的产出摘要文件。

跑法：
    cd C:\\Users\\Administrator\\Desktop\\condense
    python tests\\test_reduce_overflow.py
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 默认测同目录的上级 condense.py；可用 CONDENSE_PY 指到别的副本（例如旧的 Hermes 副本）
# 来做「旧版必然失败 / 新版通过」的对照。
CONDENSE = Path(os.environ.get("CONDENSE_PY") or (HERE.parent / "condense.py"))

REAL_TPC = 2.4  # 假 Ollama 认账的真实密度（故意比脚本先验更密）

STATE = {"chat": 0, "overflow": 0, "max_prompt_tokens": 0, "max_prompt_chars": 0}
FAILS = []


class MockOllama(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音
        pass

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/version"):
            self._send(200, {"version": "mock-0.0.0"})
        elif self.path.startswith("/api/tags"):
            self._send(200, {"models": [{"name": "openbmb/minicpm5-2b:latest"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:  # noqa: BLE001
            payload = {}
        messages = payload.get("messages") or [{}]
        prompt = (messages[0] or {}).get("content", "") or ""
        ctx = int((payload.get("options") or {}).get("num_ctx") or 4096)
        tokens = int(len(prompt) * REAL_TPC) + 40

        STATE["chat"] += 1
        STATE["max_prompt_tokens"] = max(STATE["max_prompt_tokens"], tokens)
        STATE["max_prompt_chars"] = max(STATE["max_prompt_chars"], len(prompt))

        if tokens > ctx:
            STATE["overflow"] += 1
            self._send(400, {"error": (
                f"request ({tokens} tokens) exceeds the available context size "
                f"({ctx} tokens), try increasing it"
            )})
            return

        text = "要点：" + prompt.replace("\n", " ")[-60:]
        self._send(200, {
            "model": "mock",
            "message": {"role": "assistant", "content": text},
            "done_reason": "stop",
            "eval_count": 20,
            "prompt_eval_count": tokens,
            "eval_duration": 200_000_000,
            "total_duration": 300_000_000,
        })


def check(cond: bool, msg: str) -> None:
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        FAILS.append(msg)


def load_module(host: str):
    os.environ["OLLAMA_HOST"] = host
    spec = importlib.util.spec_from_file_location("condense_under_test", CONDENSE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_main(mod, args):
    argv, code, buf = sys.argv, 0, io.StringIO()
    sys.argv = ["condense.py"] + args
    try:
        with contextlib.redirect_stdout(buf):
            mod.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        sys.argv = argv
    return code, buf.getvalue()


def cjk_log(n_lines: int) -> str:
    return "".join(
        f"2026-10-07 01:42:{i % 60:02d} 错误码 E{1000 + i} "
        f"路径 C:\\Users\\Administrator\\Desktop\\condense\\condense.py "
        f"第 {i} 行：读取配置失败，正在重试，耗时 {i * 7} 毫秒。\n"
        for i in range(n_lines)
    )


def ascii_log(n_lines: int) -> str:
    return "".join(
        f"2026-10-07T01:42:{i % 60:02d}Z ERROR code=E{1000 + i} "
        f"path=C:/Users/Administrator/Desktop/condense/condense.py "
        f"line={i} retry in {i * 7}ms\n"
        for i in range(n_lines)
    )


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockOllama)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{port}"
    print(f"假 Ollama 已启动：{host}   （真实密度 = {REAL_TPC} token/字符）\n")

    tmp = Path(tempfile.mkdtemp(prefix="condense_test_"))

    # --- 0. 纯函数单元检查 ---
    print("[0] 单元检查")
    mod = load_module(host)
    prior_fn = getattr(mod, "_prior_tokens_per_char", None)
    overflow_fn = getattr(mod, "_looks_like_overflow", None)
    if prior_fn is None:
        check(False, "存在密度先验函数 _prior_tokens_per_char（旧版写死 0.75，没有这个函数）")
    else:
        cjk_prior = prior_fn("这是一段中文测试文本，包含标点。")
        ascii_prior = prior_fn("hello world this is plain english")
        check(cjk_prior > 1.2, f"中文先验 {cjk_prior:.2f} > 1.2（旧版写死 0.75）")
        check(ascii_prior < 0.8, f"纯 ASCII 先验 {ascii_prior:.2f} < 0.8")
    if overflow_fn is None:
        check(False, "存在上下文溢出的识别函数 _looks_like_overflow（旧版直接 die）")
    else:
        check(overflow_fn(
            "request (13650 tokens) exceeds the available context size (8192 tokens), try increasing it"
        ), "能识别 Ollama 真实的 400 报错文本")
        check(not overflow_fn('{"error":"model not found"}'),
              "普通错误不会被误判成上下文溢出")

    # --- 1. 中文日志 + 小 ctx：Map 和 Reduce 都必须自救 ---
    print("\n[1] 中文日志 + ctx=2048（真实密度 2.4，脚本先验必然低估）")
    src = tmp / "cjk.log"
    src.write_text(cjk_log(80), encoding="utf-8")
    out = tmp / "digest_cjk.md"
    snap = dict(STATE)
    code, log = run_main(mod, [str(src), "--ctx", "2048", "--out", str(out)])
    used = STATE["chat"] - snap["chat"]
    overflowed = STATE["overflow"] - snap["overflow"]
    check(code == 0, f"退出码 0（旧版会 [错误] HTTP 400 后 exit 1），实际 {code}")
    check(out.exists() and out.stat().st_size > 0, f"产出了摘要文件（{out.stat().st_size if out.exists() else 0} 字节）")
    check(overflowed > 0, f"确实触发了 {overflowed} 次 HTTP 400 —— 兜底路径被真实走到")
    check(0 < used < 200, f"二分递归收敛，调用次数有界（{used} 次）")
    dens = getattr(mod, "_DENSITY", {})
    check(dens.get("last", 0) > 2.0,
          f"密度被实测校准：最近实测 {dens.get('last', 0):.2f} token/字符，"
          f"估算采用 {dens.get('tpc', 0):.2f}")
    check("撞上下文" in log or "截断" in log, "日志里能看到二分/截断的自救记录")

    # --- 2. 纯 ASCII：不应被过度切碎 ---
    print("\n[2] 纯 ASCII + ctx=4096（密度低，不该乱切）")
    src2 = tmp / "ascii.log"
    src2.write_text(ascii_log(120), encoding="utf-8")
    out2 = tmp / "digest_ascii.md"
    snap2 = dict(STATE)
    code2, _ = run_main(mod, [str(src2), "--ctx", "4096", "--out", str(out2)])
    used2 = STATE["chat"] - snap2["chat"]
    check(code2 == 0, f"退出码 0，实际 {code2}")
    check(out2.exists() and out2.stat().st_size > 0, "产出了摘要文件")
    check(used2 < 200, f"调用次数有界（{used2} 次）")

    # --- 3. 密度极端低估：手动把先验压到 0.2，逼出最坏情况 ---
    print("\n[3] 强制 --tpc 0.2（先验严重低估）+ ctx=2048：仍不许崩")
    out3 = tmp / "digest_forced.md"
    snap3 = dict(STATE)
    code3, _ = run_main(mod, [str(src), "--ctx", "2048", "--tpc", "0.2", "--out", str(out3)])
    used3 = STATE["chat"] - snap3["chat"]
    check(code3 == 0, f"退出码 0，实际 {code3}")
    check(out3.exists() and out3.stat().st_size > 0, "产出了摘要文件")
    check(0 < used3 < 400, f"调用次数有界（{used3} 次）")

    # --- 4. dry-run 不该连模型 ---
    print("\n[4] --dry-run 不调用模型")
    snap4 = dict(STATE)
    code4, _ = run_main(mod, [str(src), "--ctx", "2048", "--dry-run", "--limit", "3"])
    check(code4 == 0 and STATE["chat"] == snap4["chat"], "dry-run 退出码 0 且零次模型调用")

    srv.shutdown()
    print("\n" + "=" * 62)
    if FAILS:
        print(f"❌ 失败 {len(FAILS)} 项：")
        for f in FAILS:
            print("   - " + f)
        return 1
    print("✅ 全部通过 —— Reduce 撞上下文的老 bug 已修复（不会再整个任务退出）")
    print(f"   临时文件：{tmp}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
