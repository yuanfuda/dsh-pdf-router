# dsh-pdf-router

A routing PDF reader for DeepSeek Harness. One tool, `pdf_read`, that picks the
cheapest engine per page instead of blindly OCRing everything:

| Page situation | Engine | Cost |
|---|---|---|
| Has a text layer | PyMuPDF direct extraction | ~0.01 s/page, zero recognition error |
| No text layer (scan/photo) | vision OCR endpoint (MiniCPM-OCR recommended) | ~20-130 s/page |
| Tables or formulas matter | MiniCPM-OCR layout pipeline | ~130-200 s/page |

The point is not "PDF to text" - several plugins do that. The point is that a
page with tables and formulas comes back **machine-usable**, not merely readable.

## Install

```powershell
dsh plugin --profile web add github:yuanfuda/dsh-pdf-router
```

Restart the harness afterwards: the loader imports plugin modules once, so a
freshly installed bundle is picked up on the next start. The tool `pdf_read`
then appears for every session in that profile.

## The recommended backend: MiniCPM-OCR

[**MiniCPM-OCR**](https://github.com/OpenBMB/MiniCPM-OCR) is a 1.3B
document-parsing vision model from OpenBMB, released under **Apache-2.0**
(code and weights). It is what this plugin is built and measured against.

**Why it is the recommendation**

- **It returns structure, not just characters.** Tables come back as HTML
  `<table>` elements (OTSL internally, converted by the SDK), display formulas
  as LaTeX, and the model keeps the reading order of multi-column pages. A plain
  OCR pass gives you neither - it flattens a table into a column of numbers.
- **It handles real-world capture.** Scans, phone photos, curved pages, skewed
  pages, recaptured screens, seals and stamps are in scope, not just clean PDFs.
- **It is small enough to run locally.** Q8_0 is 775 MB plus an 859 MB vision
  projector, ~16 GB system RAM is the documented floor, and it runs on CPU,
  Vulkan or CUDA through llama.cpp, plus vLLM/SGLang/Ollama. No cloud call, so
  documents never leave the machine.
- **Published quality numbers**: 93.68 on Real5-OmniDocBench, 54.7 on
  OCRBench v2, 1.2 pages/s on an RTX 5090 - reported at roughly 1.49x the
  throughput of PaddleOCR-VL-1.6 and 3.62x that of MinerU2.5 Pro.
- **There is a companion quality model.** [MiniCPM-OCR
  Jev](https://huggingface.co/openbmb/MiniCPM-OCR-1.0-Jev) (2B) scores an OCR
  result 0-1 per region, which is how you find the cells that need a human look
  instead of trusting the whole page.

**Weights and sizes** (GGUF, from the official repo)

| File | Size | Note |
|---|---|---|
| `MiniCPM-OCR-1_0-Q4_K_M.gguf` | 505 MB | smallest usable; slight loss on dense code |
| `MiniCPM-OCR-1_0-Q8_0.gguf` | 775 MB | recommended balance |
| `MiniCPM-OCR-1_0-F16.gguf` | 1.4 GB | highest fidelity |
| `mmproj-model-f16-4x.gguf` | 859 MB | vision projector, required for images |

**Measured on this project's reference machine** (Ultra 7 155H, Intel Arc iGPU,
22 threads, no NVIDIA GPU):

| Path | Time | Result |
|---|---|---|
| Text layer, 30-page arXiv paper | 1.9 s total | 0 OCR pages needed |
| MiniCPM-OCR, one scanned page | 21-43 s | two-column reading order preserved |
| MiniCPM-OCR SDK, one table page | 199 s | 5 HTML `<table>` blocks |
| MiniCPM-OCR SDK, one formula page | 65 s | compilable LaTeX |

Quality check on a real paper page, scored against the PDF's own text layer:
sequence CER **0.3%** for MiniCPM-OCR versus **53%** for a plain OCR pass - the
difference is reading order, not characters.

### Deploying it locally (Windows, no NVIDIA GPU)

```powershell
# 1) weights - ModelScope mirrors the official repo (huggingface.co is often blocked in CN)
curl.exe -L -o MiniCPM-OCR-1_0-Q8_0.gguf `
  https://modelscope.cn/models/OpenBMB/MiniCPM-OCR-1.0-gguf/resolve/master/MiniCPM-OCR-1_0-Q8_0.gguf
curl.exe -L -o mmproj-model-f16-4x.gguf `
  https://modelscope.cn/models/OpenBMB/MiniCPM-OCR-1.0-gguf/resolve/master/mmproj-model-f16-4x.gguf

# 2) runtime: llama.cpp prebuilt with the Vulkan backend drives an Intel/AMD iGPU
#    https://github.com/ggml-org/llama.cpp/releases -> llama-b11962-bin-win-vulkan-x64.zip

# 3) serve an OpenAI-compatible endpoint on :18499
llama-server.exe --model MiniCPM-OCR-1_0-Q8_0.gguf --mmproj mmproj-model-f16-4x.gguf `
  --host 127.0.0.1 --port 18499 --ctx-size 16384 --n-gpu-layers 999
```

Layout-aware (`want: structure`) additionally needs the `minicpm-ocr` SDK and
PP-DocLayoutV3:

```bash
pip install "minicpm-ocr[all]==1.0.2" "paddlepaddle==3.2.1"   # CPU is enough
# PP-DocLayoutV3_infer.tar (126 MB) from paddle-model-ecology.bj.bcebos.com
```

Sources: [model card](https://huggingface.co/openbmb/MiniCPM-OCR-1.0) .
[llama.cpp guide](https://github.com/OpenBMB/MiniCPM-OCR/blob/main/docs/llamacpp_deploy.md) .
[PyPI SDK](https://pypi.org/project/minicpm-ocr/)

## Using a different backend

The OCR half is provider-agnostic: anything that speaks OpenAI
`POST {base}/chat/completions` with `image_url` content parts works. Point
`ocrEndpoint` at Ollama, vLLM, LM Studio or a hosted API:

```jsonc
{ "ocrEndpoint": "http://127.0.0.1:11434/v1", "ocrModel": "qwen2.5vl:7b", "ocrProfile": "generic" }
```

`ocrProfile: generic` asks the model for Markdown with HTML tables and LaTeX
formulas in words. Expect that only models with real document training - which
is exactly what MiniCPM-OCR is for - will honour it. `structure` mode is
MiniCPM-OCR specific: it depends on the model's own table and formula prompts.

## Requirements

- **PyMuPDF** in the python that runs `router.py`: `pip install pymupdf`.
  Without it the tool returns an actionable error rather than a traceback.
- **Optional** the OCR endpoint above. Without it, digital PDFs still work and
  scanned pages return a warning explaining what is missing.

## Configuration

Set on the plugin row (`config`) in the bundle patch or your profile patch:

| Key | Default | Meaning |
|---|---|---|
| `pythonPath` | auto-detect, then `python` | python that can `import pymupdf` |
| `ocrEndpoint` | `http://127.0.0.1:18499/v1` | OpenAI-compatible base URL |
| `ocrModel` | `minicpm-ocr` | model name sent to the endpoint |
| `ocrApiKey` | empty | bearer token for hosted endpoints |
| `ocrProfile` | `auto` | `auto` / `minicpm` / `generic` prompt family |
| `ocrPrompt` | empty | overrides the instruction text |
| `sdkExe` | auto-detect | `minicpmvocr` executable, `structure` only |
| `layoutModelDir` | auto-detect | PP-DocLayoutV3 inference dir, `structure` only |
| `defaultWant` | `auto` | `auto` / `text` / `structure` |
| `timeoutMs` | `1800000` | whole-document budget |

## Tool usage

```jsonc
{ "path": "C:\\docs\\paper.pdf" }                                    // per-page routing
{ "path": "C:\\docs\\paper.pdf", "pages": "7", "want": "structure" } // HTML tables + LaTeX
{ "path": "C:\\docs\\scan.pdf", "want": "text" }                     // never call OCR
{ "path": "C:\\docs\\photo.jpg" }                                    // images are OCRed directly
```

Returns route counts, per-page timing, warnings, the Markdown, and the `out`
path. Documents above 14000 characters are written to `out` and truncated inline.

## Limitations

1. **Slow on CPU-only hardware.** A 30-page two-column paper through the SDK is
   roughly an hour; `auto` on born-digital PDFs is seconds. Batch overnight.
2. **Tables are structured, not perfect.** Small tables with multi-level headers
   can still misalign columns.
3. **Scans need the endpoint deployed.** The plugin does not install 1.6 GB of
   weights for you.
4. **Code edits need a harness restart.** The loader imports plugin modules with
   a plain `import()`, so Node caches them by URL; HMR does not cover a workspace
   directory linked into a profile.

## Development

`node selftest.mjs` checks registration and the render path offline, without
booting a harness.

## Publishing checklist (not a market listing yet)

1. Remove `private: true`; fill `repository`, `homepage`, `bugs`, `author`.
2. `LICENSE` (MIT) and a raster `icon-180.png` are already included - market
   tiles reject SVG, which is why `icon.svg` is only the in-app manifest icon.
3. Push to a public GitHub repo and add the `dsh-plugin` topic.
4. Optional: `npm publish --access public` (the name `dsh-pdf-router` is free).
5. PR one file `data/plugins/<owner>__dsh-pdf-router.yml` to
   [awesome-dsh-plugin](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin).
   The repo must be at least one day old at submission time.
