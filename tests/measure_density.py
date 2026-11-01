#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
measure_density.py —— 按语料类型实测 token 密度（token / 字符）

方法：**raw 模式**，绕开聊天模板，prompt_eval_count 就是纯文本的 token 数。
  * Ollama      : /api/generate  {raw: true}
  * llama.cpp   : /completion    （8010 Bonsai）
这样短样本也不会被模板开销污染。

用法：
    python tests\measure_density.py
"""

import json
import os
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
BONSAI = "http://127.0.0.1:8010"
MINI = "openbmb/minicpm5-2b"

LEN = 1200  # 每个语料取 1200 字符


def _post(url: str, payload: dict, timeout: int = 300) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with _opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def ollama_tokens(text: str) -> int:
    r = _post(f"{OLLAMA}/api/generate", {
        "model": MINI, "prompt": text, "raw": True, "stream": False,
        "options": {"num_predict": 1, "num_ctx": 16384},
    })
    return int(r.get("prompt_eval_count") or 0)


def bonsai_tokens(text: str) -> int:
    r = _post(f"{BONSAI}/completion", {
        "prompt": text, "n_predict": 1, "temperature": 0.0,
    })
    return int(r.get("tokens_evaluated") or 0)


def fit(text: str, n: int = LEN) -> str:
    """把语料截到 n 字符（按行边界）。"""
    if len(text) <= n:
        return text
    cut = text[:n]
    nl = cut.rfind("\n")
    return cut[: nl + 1] if nl > n * 0.5 else cut


def prose_from_reports() -> str:
    """从 04-文档报告 里的中文报告抽“真散文”行（滤掉表格/代码/英文行）。"""
    base = Path(r"C:\Users\Administrator\Desktop\04-文档报告")
    out = []
    for f in sorted(base.glob("*.md")):
        for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
            s = ln.strip().lstrip(">#-* ").strip()
            if len(s) < 12 or s.startswith(("```", "|", "    ")):
                continue
            cjk = sum(1 for ch in s if 0x4E00 <= ord(ch) <= 0x9FFF)
            ascii_n = sum(1 for ch in s if ord(ch) < 128)
            if cjk >= len(s) * 0.6 and ascii_n < len(s) * 0.25:
                out.append(s)
        if sum(len(x) for x in out) > LEN * 3:
            break
    return "\n".join(out)


RARE = "龘靐齉爩龗灪龖厵纞虋韊鱻麤矗鑫淼焱垚犇羴猋贔舙譶孨尛掱嫑嘦勥恏兲氼砳嘂圐圙玊孖屾烎玍冇畾"
PHRASE = "这是密集中文散文的密度测试文本，用来看清楚分词器在常见汉字上的表现。"
TRAD = ("這是一段繁體中文的密度測試，用來看看分詞器在繁體字上的表現。"
        "說明、報告、驗證、問題、這個、時間、顯存、模型、上下文、設定、檔案、"
        "執行、錯誤、資訊、網路、開啟、關閉、檢查、測試、結果、總計、類別、應該。")

CORPORA = {
    "① 规整日志（中文+时间戳+路径+数字）": None,   # 运行时填
    "② 中文散文（从你的报告里抽的正文）": None,
    "③ 常见字短语（同一句重复）": None,
    "④ 生僻字串（龘靐齉爩…）": None,
    "⑤ 纯 ASCII 英文日志": None,
    "⑥ 全角符号 / emoji": None,
    "⑦ 繁体中文（常見字）": None,
    "⑧ emoji 炸弹": None,
    "⑨ 极端混合（繁体+生僻+emoji+符号）": None,
    "⑩ condense 自己的摘要输出（Reduce 输入同类文本）": None,
}


def build() -> dict:
    synth = (HERE / "_corpus" / "synth_zh.log")
    log_text = synth.read_text(encoding="utf-8", errors="replace") if synth.exists() else ""
    en = (HERE / "_corpus" / "real_agent_en.log")
    en_text = en.read_text(encoding="utf-8", errors="replace") if en.exists() else ""
    prose = prose_from_reports()
    digests = []
    for p in sorted((HERE / "_corpus").glob("*.digest.md")):
        digests.append(p.read_text(encoding="utf-8", errors="replace"))
    for p in sorted((HERE / "_corpus").glob("out_*.md")):
        digests.append(p.read_text(encoding="utf-8", errors="replace"))
    emoji = "💡✅❌⚠️🔴🎮📁🧹🐱🌈🔥⭐🎉🚀🛠️📊🔤🤖💬🌙🍺🎨🖥️⛔📋🔁🧪📌"
    return {
        "① 规整日志（中文+时间戳+路径+数字）": fit(log_text),
        "② 中文散文（从你的报告里抽的正文）": fit(prose),
        "③ 常见字短语（同一句重复）": fit(PHRASE * 60),
        "④ 生僻字串（龘靐齉爩…）": fit((RARE + "，") * 30),
        "⑤ 纯 ASCII 英文日志": fit(en_text),
        "⑥ 全角符号 / emoji": fit("、。，「」《》；：？！……——💡✅❌⚠️🔴🎮📁🧹喵ฅ^•ﻌ•^ฅ" * 20),
        "⑦ 繁体中文（常見字）": fit(TRAD * 20),
        "⑧ emoji 炸弹": fit((emoji + " ") * 60),
        "⑨ 极端混合（繁体+生僻+emoji+符号）": fit((TRAD + RARE + emoji + "、。「」《》；：？！……——") * 12),
        "⑩ condense 自己的摘要输出（Reduce 输入同类文本）": fit("\n\n".join(digests)),
    }


def main() -> None:
    corpora = build()
    print(f"每个语料约 {LEN} 字符 · raw 模式（无聊天模板）\n")
    print(f"{'语料':<36}{'字符':>6}{'MiniCPM5':>12}{'Bonsai':>12}")
    print("-" * 68)
    for name, text in corpora.items():
        if not text.strip():
            print(f"{name:<36}{'（缺语料）':>30}")
            continue
        t = fit(text)
        try:
            tm = ollama_tokens(t)
            dm = tm / len(t)
            sm = f"{tm:>5} ({dm:.2f})"
        except Exception as e:  # noqa: BLE001
            sm = f"失败 {str(e)[:14]}"
        try:
            tb = bonsai_tokens(t)
            db = tb / len(t)
            sb = f"{tb:>5} ({db:.2f})"
        except Exception as e:  # noqa: BLE001
            sb = f"失败 {str(e)[:14]}"
        print(f"{name:<36}{len(t):>6}{sm:>12}{sb:>12}")
    print("\n注：格式 = token 数（token/字符）")


if __name__ == "__main__":
    main()
