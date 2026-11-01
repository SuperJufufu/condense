# condense — cut cloud LLM input tokens by pre-summarizing long documents with a local model

**A single-file Python pipeline (stdlib only, no `pip install`) that compresses long documents and huge logs into a small digest with a local model before you paste them into a cloud LLM** — typically down to **1–3% of the original size**, so you pay for a 2 KB digest instead of a 654 KB log.

Why bother: the digest gets **re-sent on every later turn**, so the saving compounds. Real case — 654 KB log → 2 KB digest → **98% less input tokens**, in 8–17 s on a 2B local model.

`llm` `ollama` `local-llm` `token-optimization` `prompt-compression` `context-window` `long-documents` `python`

---

## 中文说明

### 🔤 长文档本地预压缩管道

> 建于 2026-10-06 · 千夏（Hermes）
> **2026-10-07 更新**：修掉 Reduce 撞上下文的根因、加了一键入口、补上真机实测数据
> 目的：**把长文档 / 大日志先交给本机小模型压成摘要，再丢给云端大模型**，省云端 input token 的钱。

---

## 这是什么

一个纯 Python 单文件脚本（只用标准库，**不需要 pip install**）。

```
                     ┌─ 本地小模型 openbmb/minicpm5-2b (1.9 GB, 实测 ~245 tok/s)
长文档 60 KB  ──────►│  逐块压缩（Map 阶段）
10 块                │
                     └─ 合并去重（Reduce 阶段）
                              │
                              ▼
                     摘要 ~1.2 KB  ──►  粘给 DeepSeek / Codex / DSH
                     （云端只吃这一份，省掉 98% 的 input token）
```

**它不联网**，只连 `127.0.0.1:11434`（本机 Ollama）。

---

## ⚡ 一键用法（推荐）

| 怎么做 | 结果 |
|---|---|
| 把文件**拖到** `condense\condense-dragdrop.bat` 上 | 自动压缩 → 摘要写 `<原文件>.digest.md` → **复制到剪贴板** |
| 双击桌面 **`压缩长文档.lnk`**（拖文件到它上面也行）| 同上 |
| `pwsh -File oneclick-condense.ps1 -DryRun 大文件.log` | 只试跑看分块，不调模型 |

摘要进了剪贴板，直接 `Ctrl+V` 粘给云端就行 —— 这就是「本地预压缩」的完整闭环。

---

## 命令行用法

```bash
cd C:\Users\Administrator\Desktop\condense

python condense.py 大文件.log                      # 打印摘要
python condense.py 大文件.log --out digest.md      # 写到文件
python condense.py 大文件.log --auto               # 自动选 ctx + 输出 <输入>.digest.md
type 大文件.log | python condense.py -             # 从 stdin 读
python condense.py 大文件.log --dry-run            # 只看分块计划，不跑模型
python condense.py 大文件.log --limit 3            # 只跑前 3 块（试跑）
```

### 常用参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--ctx` | 8192 | 上下文窗口。**普通文档 8192 就够，不用再手动拉到 32768** |
| `--auto` | — | 一键模式：按体积自动选 ctx（≤150KB→8192 / ≤600KB→16384 / 更大→32768）|
| `--chunk` | 自动 | 每块字符数（默认按 ctx 和**实测密度**算）|
| `--probe` / `--no-probe` | 开 | 正式分块前先量一次真实 token 密度 |
| `--tpc` | 自动 | 手动指定 tokens/字符（调试用）|
| `--out` | 无 | 输出到文件 |
| `--dry-run` | — | 只显示分块，不调模型 |
| `--limit N` | 0 | 只处理前 N 块 |
| `--model` | minicpm5-2b | 换别的本地模型 |

---

## 📊 真机实测（2026-10-07，Bonsai 8010 同时常驻）

| 用例 | 语料 | 分块 | 耗时 | 压缩比 | 备注 |
|---|---|---|---|---|---|
| 中文日志 · ctx 8192 | 59,922 字符 | 10 块 | **7.7 s** | 2.0% | 密度探测 0.80（chat 模式，含模板开销）|
| 中文日志 · ctx 2048 | 59,922 字符 | 12 块 | 10.9 s | 1.3% | **真机上触发 400 → 自动二分合并成功** |
| 真实英文日志 · ctx 8192 | 79,908 字符 | 12 块 | 16.7 s | 0.7% | `%LOCALAPPDATA%\hermes\logs\agent.log` 前 80KB |

**token 密度取决于语料 —— 实测**（**raw 模式**绕开聊天模板，`tests\measure_density.py`，各 1200 字符）：

| 语料 | MiniCPM5-2B | Bonsai (Qwen) |
|---|---|---|
| 纯 ASCII 英文日志 | 0.35 | 0.47 |
| 规整日志（中文+时间戳+路径+数字）| 0.48 | 0.56 |
| condense 自己的摘要输出（Reduce 输入同类文本）| 0.55 | 0.61 |
| 常见字短语（重复句）| 0.59 | 0.65 |
| **中文散文（从真实报告正文里抽的）** | **0.67** | 0.66 |
| 繁体中文（常见字）| 0.94 | 0.67 |
| 全角符号 / emoji | 1.41 | 1.41 |
| 生僻字串（龘靐齉爩…）| 1.91 | 1.89 |
| **emoji 炸弹** | **2.53** | **2.50** |

> 📌 **结论**：密度**确实随语料大幅变化**（0.35 → 2.53，差 7 倍），
> 所以**固定值 0.75 本身就是错的做法** —— 这正是新版改成「实测前先探测 + 逐次校准 + 二分兜底」的原因。
>
> **「2.2」到底出现在哪**：常见中文散文/日志只有 **0.48–0.67**；生僻字 1.9、纯符号 1.4；
> **只有 emoji / 符号占比极高时才到 2.5**。所以旧记录里那个 2.2 不是「中文散文」的常态，
> 更像是符号/emoji 很重的语料（或按字节而非字符算的）。**但语料相关的差异是真实存在的。**
>
> ⚠️ 我在 10-07 早些时候写过一句「2.2 复现不出来」—— 那句话把话说满了：
> 2.2 复现不出来是**对常见中文散文**而言；换成 emoji / 生僻字语料就有 2.5。

生成速度 **~245 tok/s**（Map/Reduce 实测 238–264），与文档里记的 276 tok/s 相符。

---

## 🔧 「Reduce 撞上下文」——已修复（2026-10-07）

**症状**（旧版）：合并分段要点时 `HTTP 400: request (N tokens) exceeds the available context size`，
然后脚本直接 `die()` 退出，**整趟活白跑**。

**根因**：整个流程依赖一个**猜出来的** `TOKENS_PER_CHAR = 0.75` 常量 —— 猜低了就撞上下文，
猜高了就把块切得稀碎、多跑好几倍调用。猜是不必要的，Ollama 每次回话都带 `prompt_eval_count`。

**修法（三重保险，密度估错也不崩）**：

1. **先验**：按字符类别（ASCII / CJK / 其他）给保守值，不再写死一个数；
2. **实测校准**：正式分块前先探测一次（`--probe`），之后每次调用都用 `prompt_eval_count` 反推真实密度；
3. **兜底**：真被 Ollama 拒绝时捕获 `ContextOverflow`，把内容**二分重跑**，而不是退出；
   连二分都切不动时按字符折半，最后一步才截断，并且**明说**截了多少。

**证据**（`tests\test_reduce_overflow.py`，离线假 Ollama，不需要显卡）：

```
对照组（旧副本）: ❌ 失败 11 项   exit 1   —— 撞 400 后直接退出
修复版          : ✅ 全部通过     exit 0   —— 触发 8 次真 400，全部自救成功
```

真机上（ctx 故意压到 2048）也确实走到了这条自救路径：

```
Reduce 阶段：合并分段要点
  ↻ 合并撞上下文，二分 12 → 6 + 6 条
  完成  3763→1038 字符
```

### 怎么跑测试

```powershell
cd C:\Users\Administrator\Desktop\condense
python tests\test_reduce_overflow.py     # 离线回归（不需要显卡/Ollama）
python tests\measure_density.py          # 密度实测：按语料类型出表（需要 Ollama + 8010）
# 想做「旧版必失败」对照：
$env:CONDENSE_PY='C:\Users\Administrator\AppData\Local\Hermes\condense\condense.py'
python tests\test_reduce_overflow.py
```

---

## 🤖 给 AI 代理的约定（= 手动调度版）

**没有自动调度器** —— 云端模型不会自己想到去调用 condense。所以约定一条规则：

> 当需要读的文件 **> 50 KB**（或估算 > 20k token）、且只需要「要点/脉络/找问题」而不是逐字比对时：
> **先跑 condense，再读 `<文件>.digest.md`**，不要直接整份塞进上下文。

工具侧（Hermes / DSH / Codex）照这条执行，就等于有了调度器；真正全自动的 hook 还没做（见报告第七节）。

---

## 其它注意点

1. **minicpm5 的 thinking 必须关** —— 脚本已显式传 `"think": false`，这是 245+ tok/s 的原因。别改回去。
2. **走 localhost 必须绕代理** —— 脚本用 `ProxyHandler({})` 空 opener，等价于 `curl --noproxy '*'`。系统常开 Clash Verge，不绕会拿到假错误。
3. **Ollama 默认 num_ctx 只有 4096** —— 长文会被静默截断，模型流畅地胡说。脚本显式传 num_ctx，并按 num_ctx + 实测密度反推块大小。
4. **按行边界切块** —— 不切碎日志行；超长单行（压缩 JSON、base64）硬切。
5. **和 Bonsai 同时跑没问题** —— 实测 Bonsai 11.3 GB + mini 2.0 GB 同驻，Bonsai 延迟 0.66–0.82s 无变化，mini 264 tok/s。但**必须先起 Bonsai 再挂 mini**（反过来的话 Bonsai 的 12 GB 守卫会拒绝启动）。

---

## 🔁 两份副本怎么保持同步

脚本有两份，内容必须一致（以前 README 说「Hermes 那份是主副本」，容易搞混，现在两份等价，**改完就同步**）：

```powershell
# 改完桌面这份后，同步到 Hermes 工作副本
Copy-Item 'C:\Users\Administrator\Desktop\condense\condense.py' `
          "$env:LOCALAPPDATA\Hermes\condense\condense.py" -Force
```

---

## 相关

- 模型：`Ollama.lnk` 启动，端口 11434；`ollama pull openbmb/minicpm5-2b`（1.6 GB）
- 一键入口：`condense-dragdrop.bat` / `oneclick-condense.ps1` / 桌面 `压缩长文档.lnk`
- 测试：`tests\test_reduce_overflow.py`（离线）· 语料 `tests\_corpus\`
- 报告：`04-文档报告\condense-Reduce修复与双模型共存复核-20261007.md`

---

*千夏 ฅ^•ﻌ•^ฅ · 2026-10-06 建，2026-10-07 修*
