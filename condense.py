#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
condense.py --- 本地小模型预筛管道
================================================
把长文档 / 大日志先交给本机的小模型（默认 openbmb/minicpm5-2b）压成摘要，
再把摘要丢给云端大模型（DeepSeek / Codex / DSH）处理。

省的是**云端 input token 的钱**，不是提升能力。
纯本地运算，只连 127.0.0.1，不联外网。

用法:
    python condense.py 大文件.log
    python condense.py 大文件.log --out digest.md
    type 大文件.log | python condense.py -
    python condense.py 大文件.log --dry-run      (只看分块计划，不跑模型)

设计要点:
    * Ollama 默认 num_ctx 只有 4096，长文会被**静默截断**导致模型流畅地胡说。
      本脚本显式传 num_ctx，并按 num_ctx 反推安全分块大小。
    * minicpm5 默认开 thinking，批量活会先烧光预算再答题。
      本脚本显式 think=False。
    * 分块按**行边界**切，不切碎日志行。
    * 只依赖标准库，不需要 pip install。

2026-10-07 修复（Reduce 撞上下文）:
    旧版写死 TOKENS_PER_CHAR = 0.75，中文/数字密集的日志实测密度能到 2 以上，
    Reduce 阶段拼起来就撞穿 num_ctx，旧版直接 die() 退出，整个任务白跑。
    新版三重保险，无论密度估得准不准都能跑完：
      ① 先验：按字符类别（ASCII / CJK / 其他）给保守的 tokens/char；
      ② 校准：每次调用后用 Ollama 回的 prompt_eval_count 反推真实密度，
         估低了立刻上调（低估 = 撞上下文，宁可多切几刀）；
      ③ 兜底：真的被 Ollama 拒绝（HTTP 400 "exceeds the available context size"）
         时捕获 ContextOverflow，把内容二分重跑，而不是整个任务退出。
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------- 基本配置 ----------
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
DEFAULT_MODEL = "openbmb/minicpm5-2b"

# 走 localhost 必须绕开系统代理，否则 curl/urllib 会拿到误导性的错误。
# 等价于 curl 的 --noproxy '*'。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ---------- token 密度：先验 + 实测校准 ----------
_DENSITY = {"tpc": 0.75, "last": 0.0, "samples": 0}
_MAX_TPC = 3.0          # 再离谱的文本也不会超过 ~3 token/字符，防止先验失控
_MIN_PIECE_CHARS = 120  # 小于这个长度不允许再二分（保证递归收敛）

_OVERFLOW_MARKERS = (
    "exceeds the available context size",
    "exceeds the context size",
    "context length exceeded",
    "too many tokens",
    "number of tokens exceeds",
)


class ContextOverflow(RuntimeError):
    """Ollama 明确拒绝：prompt 超过 num_ctx。调用方应切小重跑，而不是退出。"""


def _prior_tokens_per_char(text: str) -> float:
    """按字符类别给 tokens/char 的保守先验（宁可高估）。"""
    if not text:
        return 0.75
    ascii_n = cjk_n = other_n = 0
    for ch in text:
        o = ord(ch)
        if o < 0x80:
            ascii_n += 1
        elif 0x2E80 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFFEF:
            cjk_n += 1
        else:
            other_n += 1
    n = float(len(text))
    tpc = 0.50 * (ascii_n / n) + 1.80 * (cjk_n / n) + 1.20 * (other_n / n)
    return min(_MAX_TPC, max(0.5, tpc))


def _calibrate(prompt: str, prompt_tokens: int) -> None:
    """用一次真实调用的 prompt_eval_count 校准 token 密度。"""
    if not prompt or prompt_tokens <= 0:
        return
    obs = prompt_tokens / float(len(prompt))
    _DENSITY["samples"] += 1
    _DENSITY["last"] = obs
    if obs > _DENSITY["tpc"]:
        # 实测更密 → 立刻上调（低估就是撞上下文）
        _DENSITY["tpc"] = min(_MAX_TPC, obs)
    else:
        # 实测更松 → 缓慢回落，但绝不低于本次实测
        _DENSITY["tpc"] = max(obs, _DENSITY["tpc"] * 0.9 + obs * 0.1)


def est_tokens(text: str) -> int:
    return int(len(text) * _DENSITY["tpc"]) + 32


def char_budget(ctx: int, reserve_ratio: float, template_chars: int = 0) -> int:
    """在 ctx 里留出 reserve_ratio 给输出和模板，返回能安全塞进去的字符数。"""
    reserve = max(512, int(ctx * reserve_ratio))
    usable = max(256, ctx - reserve)
    return max(120, int(usable / _DENSITY["tpc"]) - template_chars)


def _looks_like_overflow(body: str) -> bool:
    low = (body or "").lower()
    return any(m in low for m in _OVERFLOW_MARKERS)


def _split_half(text: str):
    """尽量按行边界把文本切成两半；切不动返回 None。"""
    if len(text) < 2 * _MIN_PIECE_CHARS:
        return None
    lines = text.splitlines(keepends=True)
    if len(lines) < 2:
        mid = len(text) // 2
        return (text[:mid], text[mid:]) if mid > 0 else None
    half = len(text) // 2
    acc, idx = 0, None
    for i, ln in enumerate(lines):
        acc += len(ln)
        if acc >= half:
            idx = i + 1
            break
    if idx is None:
        idx = len(lines) - 1
    idx = max(1, min(idx, len(lines) - 1))
    left, right = "".join(lines[:idx]), "".join(lines[idx:])
    if not left.strip() or not right.strip():
        return None
    return left, right


def _force_fit(model: str, kind: str, text: str, ctx: int, temperature: float,
               indent: str = "  ", min_chars: int = 64):
    """实在切不动时的最后一道：按字符折半直到 Ollama 接受（下限 min_chars）。"""
    size = len(text)
    last = ""
    while size >= min_chars:
        piece = text[:size]
        if kind == "map":
            prompt = MAP_PROMPT.format(chunk=piece)
        else:
            prompt = REDUCE_PROMPT.format(joined=piece)
        try:
            txt, st = chat(model, prompt, ctx, temperature)
            print(f"{indent}⚠ 无法再切，已按 {size}/{len(text)} 字符截断处理")
            return txt, st
        except ContextOverflow as e:
            last = str(e)
            size //= 2
    raise ContextOverflow(f"连 {min_chars} 字符都放不进 ctx={ctx}（模板本身就吃满了）：{last[:120]}")


def _post(path: str, payload: dict, timeout: int = 900) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA + path, data=data, headers={"Content-Type": "application/json"}
    )
    with _opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(path: str, timeout: int = 10) -> dict:
    req = urllib.request.Request(OLLAMA + path)
    with _opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def check_backend(model: str) -> None:
    """确认 Ollama 活着、模型在本地。坏了就直说，不要装作能跑。"""
    try:
        _get("/api/version")
    except Exception as e:  # noqa: BLE001
        die(f"连不上 Ollama ({OLLAMA})：{e}\n  提示：确认 Ollama 服务在跑（ollama list 能出结果）")
    try:
        tags = _get("/api/tags")
    except Exception as e:  # noqa: BLE001
        die(f"Ollama 响应异常：{e}")

    names = [m.get("name", "") for m in tags.get("models", [])]
    if model not in names and f"{model}:latest" not in names:
        die(
            f"本地没有模型 {model}\n"
            f"  现有：{', '.join(names) or '(空)'}\n"
            f"  安装：ollama pull {model}"
        )


def die(msg: str) -> None:
    print(f"[错误] {msg}", file=sys.stderr)
    sys.exit(1)


def chat(model: str, prompt: str, ctx: int, temperature: float, timeout: int = 900,
         reserve_output: bool = True, max_out: int = 0) -> tuple:
    """跑一轮，返回 (文本, 统计dict)。prompt 撞上下文时抛 ContextOverflow。"""
    options = {
        "num_ctx": ctx,
        "temperature": temperature,
        "top_p": 0.9,
        "repeat_penalty": 1.05,
    }
    if max_out > 0:
        options["num_predict"] = max_out
    elif reserve_output:
        # 让 prompt + 生成 不超过 num_ctx，避免输出被静默截断
        options["num_predict"] = max(128, ctx - est_tokens(prompt))

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,  # 关键：关掉思考链，否则批量活又慢又啰嗦
        "options": options,
    }
    t0 = time.time()
    try:
        r = _post("/api/chat", payload, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        if _looks_like_overflow(body):
            raise ContextOverflow(body[:300])
        die(f"Ollama 返回 HTTP {e.code}：{body[:400]}")
    except ContextOverflow:
        raise
    except Exception as e:  # noqa: BLE001
        die(f"调用失败：{e}")

    text = (r.get("message") or {}).get("content", "") or ""
    _calibrate(prompt, int(r.get("prompt_eval_count") or 0))
    stats = {
        "wall": time.time() - t0,
        "eval_count": r.get("eval_count", 0),
        "prompt_eval_count": r.get("prompt_eval_count", 0),
        "eval_duration": r.get("eval_duration", 0),
        "done_reason": r.get("done_reason", ""),
    }
    if stats["eval_duration"]:
        stats["tok_s"] = stats["eval_count"] / (stats["eval_duration"] / 1e9)
    else:
        stats["tok_s"] = 0.0
    return text.strip(), stats


# ---------- 分块 ----------
def probe_density(model: str, text: str, ctx: int, temperature: float) -> bool:
    """正式分块前，拿一小段真实文本量一次 token 密度。

    先验只是保险，实测才是准的：量过之后就不必为了保险把块切得过小
    （切小 = 多跑好几倍的 Map 调用）。返回是否量成功。
    """
    size = 2000
    while size >= 400:
        sample = text[:size]
        cut = sample.rfind("\n")
        if cut > 0:
            sample = sample[: cut + 1]
        try:
            chat(model, MAP_PROMPT.format(chunk=sample), ctx, temperature,
                 reserve_output=False, max_out=1)
            return True
        except ContextOverflow:
            size //= 2
    return False


def chunk_text(text: str, size: int, overlap: int) -> list:
    """按行边界切块，保留 overlap 个字符的尾部重叠，避免切断上下文。"""
    lines = text.splitlines(keepends=True)
    chunks, cur, curlen = [], [], 0

    def flush():
        nonlocal cur, curlen
        if cur:
            chunks.append("".join(cur))
        # 保留尾部若干行做重叠
        tail, tl = [], 0
        for prev in reversed(cur):
            if tl + len(prev) > overlap:
                break
            tail.insert(0, prev)
            tl += len(prev)
        cur, curlen = tail, tl

    for ln in lines:
        # 单行超长（压缩过的 JSON、base64 等）就地硬切
        while len(ln) > size:
            head, ln = ln[:size], ln[size:]
            if cur:
                chunks.append("".join(cur))
                cur, curlen = [], 0
            chunks.append(head)
        if curlen + len(ln) > size and cur:
            flush()
        cur.append(ln)
        curlen += len(ln)
    if cur:
        chunks.append("".join(cur))
    return [c for c in chunks if c.strip()]


MAP_PROMPT = """你是文档压缩器。把下面这段内容压缩成要点。

规则：
1. 保留所有具体数字、文件路径、错误码、时间戳、程序名、命令
2. 绝对不要编造原文没有的内容；原文没说就说"未提及"
3. 去掉重复行、寒暄、无信息量的噪声
4. 用中文，条目式，尽量短
5. 直接输出要点本身，不要写"这段内容讲述了……"这类开场白

内容：
---
{chunk}
---"""

REDUCE_PROMPT = """下面是同一份文档分段压缩后的要点。请合并成一份最终摘要。

规则：
1. 合并重复项，同一件事只说一次
2. 保留所有具体数字、路径、错误码
3. 绝对不要编造
4. 按主题分组，条目式
5. 直接输出，不要开场白

分段要点：
---
{joined}
---"""


# ---------- 带兜底的两阶段 ----------
def map_piece(model: str, text: str, ctx: int, temperature: float,
              indent: str = "        ", depth: int = 0) -> list:
    """压缩一块；撞上下文就二分重跑。返回 [(文本, stats, 原始字符数), ...]。"""
    prompt = MAP_PROMPT.format(chunk=text)
    try:
        txt, st = chat(model, prompt, ctx, temperature)
        return [(txt, st, len(text))]
    except ContextOverflow:
        halves = None if depth >= 12 else _split_half(text)
        if halves:
            left, right = halves
            print(f"{indent}↻ 撞上下文，二分：{len(text)} → {len(left)} + {len(right)} 字符")
            return (map_piece(model, left, ctx, temperature, indent, depth + 1)
                    + map_piece(model, right, ctx, temperature, indent, depth + 1))
        txt, st = _force_fit(model, "map", text, ctx, temperature, indent)
        return [(txt, st, len(text))]


def reduce_items(model: str, items: list, ctx: int, temperature: float,
                 indent: str = "  ", depth: int = 0) -> tuple:
    """把一组要点合并成一份摘要；撞上下文就二分递归。返回 (文本, stats)。"""
    if depth > 12:
        raise ContextOverflow("合并递归过深，放弃二分")
    joined = "\n\n".join(items)
    prompt = REDUCE_PROMPT.format(joined=joined)
    try:
        return chat(model, prompt, ctx, temperature)
    except ContextOverflow:
        if len(items) > 1:
            mid = max(1, len(items) // 2)
            print(f"{indent}↻ 合并撞上下文，二分 {len(items)} → {mid} + {len(items) - mid} 条")
            left, _ = reduce_items(model, items[:mid], ctx, temperature, indent, depth + 1)
            right, _ = reduce_items(model, items[mid:], ctx, temperature, indent, depth + 1)
            return reduce_items(model, [left, right], ctx, temperature, indent, depth + 1)
        halves = _split_half(items[0])
        if not halves:
            return _force_fit(model, "reduce", items[0], ctx, temperature, indent)
        print(f"{indent}↻ 单条要点仍超，按字符二分：{len(items[0])} 字符")
        left, _ = reduce_items(model, [halves[0]], ctx, temperature, indent, depth + 1)
        right, _ = reduce_items(model, [halves[1]], ctx, temperature, indent, depth + 1)
        return reduce_items(model, [left, right], ctx, temperature, indent, depth + 1)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="本地小模型预筛：长文档先压缩，再交给云端大模型",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("input", help="输入文件路径，或 - 表示从 stdin 读")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"本地模型 (默认 {DEFAULT_MODEL})")
    ap.add_argument("--ctx", type=int, default=8192, help="num_ctx，必须显式给 (默认 8192)")
    ap.add_argument("--chunk", type=int, default=0, help="每块字符数 (默认按 ctx 自动算)")
    ap.add_argument("--overlap", type=int, default=200, help="块间重叠字符数 (默认 200)")
    ap.add_argument("--out", default="", help="把摘要写到文件")
    ap.add_argument("--dry-run", action="store_true", help="只显示分块计划，不跑模型")
    ap.add_argument("--temperature", type=float, default=0.3, help="采样温度 (默认 0.3)")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个块（试跑用）")
    ap.add_argument("--tpc", type=float, default=0.0,
                    help="手动指定 tokens/字符密度 (默认按字符类别先验 + 实测校准)")
    ap.add_argument("--probe", dest="probe", action="store_true", default=True,
                    help="正式分块前先量一次真实 token 密度 (默认开)")
    ap.add_argument("--no-probe", dest="probe", action="store_false",
                    help="关掉密度探测，直接用字符类别先验")
    ap.add_argument("--auto", action="store_true",
                    help="一键模式：按体积自动选 ctx；未给 --out 时输出到 <输入>.digest.md")
    args = ap.parse_args()

    # 输入
    if args.input == "-":
        text = sys.stdin.read()
        src = "<stdin>"
    else:
        p = Path(args.input)
        if not p.exists():
            die(f"文件不存在：{p}")
        text = p.read_text(encoding="utf-8", errors="replace")
        src = str(p)

    if not text.strip():
        die("输入是空的")

    if args.tpc > 0:
        _DENSITY["tpc"] = args.tpc
    else:
        # 先验按本文档的字符构成给，比旧的写死 0.75 稳
        _DENSITY["tpc"] = max(_DENSITY["tpc"], _prior_tokens_per_char(text))

    # --auto：按体积自动挑 ctx（大文档给大窗口，少切几刀）
    auto_note = ""
    if args.auto:
        if args.ctx == 8192:
            kb = len(text) / 1000.0
            args.ctx = 8192 if kb <= 150 else (16384 if kb <= 600 else 32768)
        if not args.out and src != "<stdin>":
            args.out = src + ".digest.md"
        auto_note = f"（--auto：按 {len(text):,} 字符选了 ctx {args.ctx}）"

    probed = False

    def make_chunks():
        size = args.chunk if args.chunk > 0 else char_budget(args.ctx, 0.35, len(MAP_PROMPT))
        cs = chunk_text(text, size, args.overlap)
        if args.limit:
            cs = cs[: args.limit]
        return size, cs

    chunk_size, chunks = make_chunks()

    def print_header():
        if args.tpc > 0:
            note = "（手动 --tpc）"
        elif probed:
            note = "（模型实测）"
        else:
            note = "（按字符类别先验）"
        print(f"源文件      {src} {auto_note}")
        print(f"字符数      {len(text):,}   (约 {est_tokens(text):,} token 估算)")
        print(f"分块        {len(chunks)} 块 × ≤{chunk_size} 字符  (重叠 {args.overlap})")
        print(f"num_ctx     {args.ctx}   模型 {args.model}")
        print(f"token 密度  {_DENSITY['tpc']:.2f} token/字符 {note}")
        print()

    if args.dry_run:
        print_header()
        for i, c in enumerate(chunks, 1):
            head = c.strip().splitlines()[0][:70] if c.strip() else ""
            print(f"  块{i:>3}  {len(c):>6} 字符   | {head}")
        print("\n(--dry-run：未调用模型)")
        return

    check_backend(args.model)

    # 正式分块前先量一次真实密度：量准了就不用为了保险把块切太小
    if args.probe and args.chunk == 0 and args.tpc <= 0 and len(text) > 2500:
        probed = probe_density(args.model, text, args.ctx, args.temperature)
        if probed:
            chunk_size, chunks = make_chunks()

    print_header()

    # ---- Map 阶段 ----
    print(f"Map 阶段：{len(chunks)} 块 → 逐块压缩")
    parts, tot_out, tot_sec = [], 0, 0.0
    for i, c in enumerate(chunks, 1):
        pieces = map_piece(args.model, c, args.ctx, args.temperature)
        for txt, st, src_len in pieces:
            tot_out += st["eval_count"]
            tot_sec += st["wall"]
            print(
                f"  [{i:>3}/{len(chunks)}] {src_len:>6}→{len(txt):>4} 字符  "
                f"{st['tok_s']:>5.1f} tok/s  {st['wall']:>5.1f}s"
            )
            if st["done_reason"] == "length":
                print("        ⚠ 输出被长度截断，考虑调大 --ctx")
        parts.extend(f"【第{i}块】\n{txt}" for txt, _, _ in pieces)

    stage1 = "\n\n".join(parts)

    # ---- Reduce 阶段 ----
    digest = stage1
    need_reduce = len(chunks) > 1 or est_tokens(REDUCE_PROMPT.format(joined=stage1)) > args.ctx - 256
    if need_reduce:
        print("Reduce 阶段：合并分段要点")
        try:
            digest, st = reduce_items(args.model, parts, args.ctx, args.temperature)
            print(f"  完成  {len(stage1)}→{len(digest)} 字符  {st['tok_s']:.1f} tok/s")
        except ContextOverflow as e:
            # 最后一道兜底：截断后硬合并，绝不整个任务退出
            cut = char_budget(args.ctx, 0.30, len(REDUCE_PROMPT))
            print(f"  ⚠ 二分仍失败（{str(e)[:80]}），改用前 {cut} 字符兜底合并")
            try:
                digest, st = chat(
                    args.model, REDUCE_PROMPT.format(joined=stage1[:cut]),
                    args.ctx, args.temperature,
                )
                print(f"  完成  {len(stage1)}→{len(digest)} 字符  {st['tok_s']:.1f} tok/s")
            except ContextOverflow:
                digest = stage1
                print("  ⚠ 兜底仍失败，直接输出分段要点（未合并，请调大 --ctx）")

    # ---- 结果 ----
    est_in_tokens = est_tokens(text)
    out_tokens = est_tokens(digest)
    ratio = out_tokens / est_in_tokens * 100 if est_in_tokens else 0

    print()
    print("=" * 62)
    print("压缩结果")
    print("=" * 62)
    print(f"  原始      {len(text):>10,} 字符   约 {est_in_tokens:>9,} token")
    print(f"  压缩后    {len(digest):>10,} 字符   约 {out_tokens:>9,} token")
    print(f"  压缩比    {ratio:>9.1f}%   省下约 {100 - ratio:.1f}%")
    print(f"  本地耗时  {tot_sec:>9.1f} 秒   (云端这部分按 0 元计)")
    print(f"  密度校准  {_DENSITY['samples']} 次采样，最近实测 {_DENSITY['last']:.2f} "
          f"token/字符（本次估算用 {_DENSITY['tpc']:.2f}）")
    print()

    if args.out:
        Path(args.out).write_text(digest, encoding="utf-8")
        print(f"已写入：{args.out}")
    else:
        print("-" * 62)
        print(digest)
        print("-" * 62)
        print()
        print("用法：把上面整段直接粘给 DeepSeek / Codex / DSH，代替原文。")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    main()
