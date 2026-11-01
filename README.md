# condense

**Compress a long document with a local small model before sending it to a cloud LLM — same answers, a fraction of the input tokens.**

A single-file Python script (standard library only, **no `pip install`**) that reads a 60 KB log or
document, splits it into chunks, summarizes each chunk on a local Ollama model, merges the summaries,
and hands you ~1 KB of digest to paste into Claude / GPT / DeepSeek instead of the full text.

```
                        ┌─ local small model (e.g. MiniCPM5-2B, ~245 tok/s)
60 KB document ────────►│  chunk-by-chunk compress (Map)
10 chunks               │
                        └─ merge + deduplicate (Reduce)
                                 │
                                 ▼
                        ~1.2 KB digest ──►  paste into your cloud model
                        (up to ~98 % fewer input tokens)
```

It does not touch the network. The only endpoint it talks to is `127.0.0.1:11434` (local Ollama).

---

## Why this exists

Feeding a 200 KB log to a cloud model to answer "what went wrong here?" is mostly waste — the model
reads every line, and you pay for every line. A 2B model running on your own GPU can do the
"read everything, keep the important parts" pass locally in seconds, for free.

This is **not** a replacement for the cloud model. It is a filter in front of it. Use it when you need
the gist, the shape of the problem, or a specific answer — not when you need verbatim comparison.

---

## Install

```bash
# 1. A local model behind Ollama
ollama pull openbmb/minicpm5-2b     # 1.6 GB, runs on CPU or GPU

# 2. The script — no dependencies
git clone https://github.com/SuperJufufu/condense
cd condense
```

`condense.py` uses only the Python standard library (3.9+). No pip, no venv, no requirements.txt.

> Any Ollama chat model works — pass `--model <name>`. MiniCPM5-2B is the default because it is fast,
> small, and good enough at "extract the important parts".

---

## Usage

```bash
python condense.py bigfile.log                     # print the digest
python condense.py bigfile.log --out digest.md     # write it to a file
python condense.py bigfile.log --auto              # auto-pick context size, write <input>.digest.md
python condense.py bigfile.log --dry-run           # show the chunking plan, do not call the model
python condense.py bigfile.log --limit 3           # process only the first 3 chunks (trial run)
cat bigfile.log | python condense.py -             # read from stdin
```

| Flag | Default | Meaning |
|---|---|---|
| `--ctx` | 8192 | Context window. **8192 is enough for normal documents** — no need to push it to 32768 |
| `--auto` | — | Pick `ctx` from file size (≤150 KB→8192, ≤600 KB→16384, larger→32768) and write `<input>.digest.md` |
| `--chunk` | auto | Characters per chunk (derived from `ctx` and the measured token density) |
| `--probe` / `--no-probe` | on | Measure real token density once before chunking |
| `--tpc` | auto | Manually set tokens/char (debugging) |
| `--out` | none | Output file |
| `--dry-run` | — | Show the chunk plan without calling the model |
| `--limit N` | 0 | Only process the first N chunks |
| `--model` | minicpm5-2b | Use a different local model |

### Windows drag-and-drop

`condense-dragdrop.bat` (drop a file on it) and `oneclick-condense.ps1` wrap the common case:
compress → write `<input>.digest.md` → copy the digest to the clipboard. Replace the hardcoded paths at
the top of both files with your own checkout location first.

---

## Benchmarks (real machine, 2026-10-07)

| Case | Corpus | Chunks | Time | Compression |
|---|---|---|---|---|
| Chinese log, ctx 8192 | 59,922 chars | 10 | **7.7 s** | 2.0 % |
| Chinese log, ctx 2048 | 59,922 chars | 12 | 10.9 s | 1.3 % (hit a real 400 → auto bisect recovery) |
| English log, ctx 8192 | 79,908 chars | 12 | 16.7 s | 0.7 % |

Throughput **~245 tok/s** (Map/Reduce measured 238–264).

### Token density depends on the corpus — measured

Fixed tokens-per-character constants are wrong. Measured with `tests/measure_density.py`, 1200 chars
per sample, raw mode (bypassing the chat template):

| Corpus | MiniCPM5-2B | Bonsai (Qwen) |
|---|---|---|
| Pure ASCII English log | 0.35 | 0.47 |
| Structured log (Chinese + timestamps + paths + numbers) | 0.48 | 0.56 |
| condense's own digest output (Reduce input) | 0.55 | 0.61 |
| Common-character phrases (repeated sentences) | 0.59 | 0.65 |
| **Chinese prose (from real reports)** | **0.67** | 0.66 |
| Traditional Chinese (common characters) | 0.94 | 0.67 |
| Full-width symbols / emoji | 1.41 | 1.41 |
| Rare CJK characters | 1.91 | 1.89 |
| **Emoji-heavy text** | **2.53** | **2.50** |

That is a **7× spread**. No fixed constant can be correct for all of them, which is why the script
probes real density, calibrates after every call, and falls back to bisecting on overflow.

---

## The "Reduce hit the context limit" bug — fixed

**Symptom (old version):** while merging chunk summaries, `HTTP 400: request (N tokens) exceeds the
available context size`, then the script `die()`d and **the entire run was lost**.

**Root cause:** the whole pipeline depended on a *guessed* `TOKENS_PER_CHAR = 0.75`. Guess low and it
overflows; guess high and it shreds the document into tiny chunks and runs several times more calls.
The guess was unnecessary: Ollama returns `prompt_eval_count` on every response.

**Fix — three independent layers:**

1. **Prior**: conservative per-character-class estimate (ASCII / CJK / other) instead of one number.
2. **Measurement**: probe once before chunking (`--probe`), then back-solve real density from
   `prompt_eval_count` after every call.
3. **Fallback**: catch `ContextOverflow` from Ollama and **bisect and retry** instead of exiting.
   If bisecting cannot split any further, halve by characters, and only truncate as a last resort —
   and say how much was truncated.

**Evidence** (`tests/test_reduce_overflow.py`, offline fake Ollama, no GPU needed):

```
old copy:    FAILED 11 checks   exit 1   -- exits immediately on the 400
fixed:       ALL PASSED         exit 0   -- triggered 8 real overflows, recovered from all
```

On real hardware (ctx deliberately clamped to 2048) it does take the recovery path:

```
Reduce stage: merging chunk summaries
  ↻ merge overflowed, bisecting 12 -> 6 + 6
  done  3763 -> 1038 chars
```

Run the tests with:

```bash
python tests/test_reduce_overflow.py      # offline regression, no GPU or Ollama needed
python tests/measure_density.py           # density table (needs Ollama)
```

---

## Using it with an AI agent

There is no automatic scheduler — a cloud model will not spontaneously decide to call condense.
So the convention is a rule in the agent's instructions:

> When a file is **> 50 KB** (or roughly > 20k tokens) and you only need the gist / the shape of the
> problem / an error to look for — **run condense first, then read `<file>.digest.md`.** Do not paste
> the whole file into context.

Any agent that follows that rule effectively gets a scheduler. The pre-compression step costs local
GPU time (which is free) instead of cloud input tokens (which are not).

---

## Gotchas that cost real time

1. **MiniCPM5's thinking mode must be off.** The script passes `"think": false` explicitly — that is
   where the 245+ tok/s comes from. Changing it back will make everything far slower.
2. **Localhost must bypass the proxy.** The script uses `ProxyHandler({})` (equivalent to
   `curl --noproxy '*'`). With Clash / system proxies running, requests to `127.0.0.1` otherwise fail
   with confusing errors.
3. **Ollama's default `num_ctx` is only 4096.** Long input gets silently truncated and the model
   hallucinates fluently. The script always passes `num_ctx` explicitly and derives the chunk size from
   it plus measured density.
4. **Chunks are split on line boundaries** — log lines are never cut in half. Extremely long single
   lines (minified JSON, base64) are hard-split.
5. **Running alongside another local model is fine.** Measured: a 11.3 GB model and the 2 GB one
   resident simultaneously, latency of the bigger one unchanged (0.66–0.82 s). But start the large
   model first — a VRAM guard may refuse to load it once the small one is already resident.

---

## License

MIT — see [LICENSE](LICENSE).
