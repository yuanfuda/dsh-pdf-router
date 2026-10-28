# dsh-pdf-router · PDF 分流阅读器

给 DeepSeek Harness 用的**分流式** PDF 读取插件。一个 `pdf_read` 工具，逐页挑最划算的引擎：

| 页面情况 | 用的引擎 | 代价 |
|---|---|---|
| 有文字层 | PyMuPDF 直抽 | 约 0.01 秒/页，零识别误差 |
| 没有文字层（扫描件/照片） | 视觉 OCR 端点（推荐 MiniCPM-OCR） | 约 20–130 秒/页 |
| 表格、公式要能用 | MiniCPM-OCR 版面流水线 | 约 130–200 秒/页 |

卖点不是“PDF 转文字”（已经有好几个插件做了），而是**带表格和公式的页面读出来仍然是机器可用的**，而不只是“人眼能看懂”。

## 安装

```powershell
dsh plugin --profile web add github:yuanfuda/dsh-pdf-router
```

装完要重启 harness：loader 只导入一次插件模块，新装的 bundle 在下次启动时生效。之后该 profile 下所有会话都能看到 `pdf_read` 工具。

## 推荐后端：MiniCPM-OCR

[**MiniCPM-OCR**](https://github.com/OpenBMB/MiniCPM-OCR) 是 OpenBMB 的 1.3B 文档解析视觉模型，**Apache-2.0** 授权（代码与权重都算商用友好）。本插件就是围着它开发和实测的。

**为什么推荐它**

- **它给的是结构，不只是字符。** 表格输出 HTML `<table>`（内部 OTSL，SDK 自动转），独立公式输出 LaTeX，并且**保持多栏页面的阅读顺序**。普通 OCR 两样都给不了——它会把一张表压平成一列数字。
- **它对“脏”文档是认真的。** 扫描件、手机拍照、弯曲页、歪斜页、屏幕重拍、印章，都在它的处理范围内。
- **它小到能在本地跑。** Q8_0 是 775 MB，加 859 MB 视觉投影；官方系统要求约 16 GB 内存起步；通过 llama.cpp 可以走 CPU / Vulkan / CUDA，也支持 vLLM、SGLang、Ollama。**全程不联网**，文档不出本机。
- **公开指标**：Real5-OmniDocBench 93.68、OCRBench v2 54.7、RTX 5090 上 1.2 pages/s；官方称吞吐约为 PaddleOCR-VL-1.6 的 1.49 倍、MinerU2.5 Pro 的 3.62 倍。
- **还有配套的质量判别模型。** [MiniCPM-OCR Jev](https://huggingface.co/openbmb/MiniCPM-OCR-1.0-Jev)（2B）能给每块 OCR 结果打 0–1 分，用来找出“这块要人看一眼”，而不是整页盲信。

**权重大小（GGUF，官方仓库）**

| 文件 | 大小 | 说明 |
|---|---|---|
| `MiniCPM-OCR-1_0-Q4_K_M.gguf` | 505 MB | 最省；密集代码略有损失 |
| `MiniCPM-OCR-1_0-Q8_0.gguf` | 775 MB | **推荐平衡档** |
| `MiniCPM-OCR-1_0-F16.gguf` | 1.4 GB | 保真最高 |
| `mmproj-model-f16-4x.gguf` | 859 MB | 视觉投影，处理图片必需 |

**本项目参考机实测**（Ultra 7 155H + Intel Arc 核显，22 线程，无 NVIDIA 显卡）：

| 路径 | 耗时 | 结果 |
|---|---|---|
| 文字层，30 页 arXiv 论文 | 共 1.9 秒 | 0 页需要 OCR |
| MiniCPM-OCR，单页扫描件 | 21–43 秒 | 双栏阅读顺序正确 |
| MiniCPM-OCR SDK，单页表格 | 199 秒 | 5 个 HTML `<table>` |
| MiniCPM-OCR SDK，单页公式 | 65 秒 | 可直接编译的 LaTeX |

用论文自带的文字层做真值打分：**序列 CER 0.3%**（普通 OCR 是 53%）——差距全在阅读顺序，不在认字。

### 本地部署（Windows，无 NVIDIA 显卡）

```powershell
# 1) 权重：ModelScope 有官方镜像（huggingface.co 在国内常被墙）
curl.exe -L -o MiniCPM-OCR-1_0-Q8_0.gguf `
  https://modelscope.cn/models/OpenBMB/MiniCPM-OCR-1.0-gguf/resolve/master/MiniCPM-OCR-1_0-Q8_0.gguf
curl.exe -L -o mmproj-model-f16-4x.gguf `
  https://modelscope.cn/models/OpenBMB/MiniCPM-OCR-1.0-gguf/resolve/master/mmproj-model-f16-4x.gguf

# 2) 运行时：用带 Vulkan 后端的 llama.cpp 预编译版，吃 Intel/AMD 核显
#    https://github.com/ggml-org/llama.cpp/releases -> llama-b11962-bin-win-vulkan-x64.zip

# 3) 在 :18499 起一个 OpenAI 兼容端点
llama-server.exe --model MiniCPM-OCR-1_0-Q8_0.gguf --mmproj mmproj-model-f16-4x.gguf `
  --host 127.0.0.1 --port 18499 --ctx-size 16384 --n-gpu-layers 999
```

要用 `want: structure`（版面感知，出表格/公式），额外需要 `minicpm-ocr` SDK 和 PP-DocLayoutV3：

```bash
pip install "minicpm-ocr[all]==1.0.2" "paddlepaddle==3.2.1"   # CPU 版足够
# PP-DocLayoutV3_infer.tar（126 MB）来自 paddle-model-ecology.bj.bcebos.com
```

## 换成别的后端

OCR 这半边是**不绑定厂商**的：任何支持 OpenAI `POST {base}/chat/completions` 且接受 `image_url` 内容块的服务都能用。把它指向 Ollama / vLLM / LM Studio / 云端：

```jsonc
{ "ocrEndpoint": "http://127.0.0.1:11434/v1", "ocrModel": "qwen2.5vl:7b", "ocrProfile": "generic" }
```

`ocrProfile: generic` 会用自然语言要求“输出 Markdown、表格用 HTML、公式用 LaTeX”。但只有真正做过文档训练的模型才照做——这正是 MiniCPM-OCR 存在的意义。`structure` 模式是 MiniCPM-OCR 专属：它依赖该模型自己的表格/公式提示词。

## 依赖

- **PyMuPDF**：装在使用 `router.py` 的那个 python 里（`pip install pymupdf`）。缺了会返回可操作的报错，不会抛栈。
- **可选**：上面的 OCR 端点。没有它，数字 PDF 照常工作，扫描页会返回一条说明缺什么的警告。

## 配置

写在插件行的 `config` 里（bundle patch 或你自己的 profile patch）：

| 键 | 默认 | 含义 |
|---|---|---|
| `pythonPath` | 自动探测，兜底 `python` | 能 `import pymupdf` 的 python |
| `ocrEndpoint` | `http://127.0.0.1:18499/v1` | OpenAI 兼容 base URL |
| `ocrModel` | `minicpm-ocr` | 发给端点的模型名 |
| `ocrApiKey` | 空 | 云端端点用的 Bearer token |
| `ocrProfile` | `auto` | `auto` / `minicpm` / `generic` 提示词族 |
| `ocrPrompt` | 空 | 直接覆盖提示词文本 |
| `sdkExe` | 自动探测 | `minicpmvocr` 可执行文件，仅 structure 用 |
| `layoutModelDir` | 自动探测 | PP-DocLayoutV3 目录，仅 structure 用 |
| `defaultWant` | `auto` | `auto` / `text` / `structure` |
| `timeoutMs` | `1800000` | 整份文档的时间预算 |

## 用法

```jsonc
{ "path": "C:\\docs\\论文.pdf" }                                    // 逐页分流
{ "path": "C:\\docs\\论文.pdf", "pages": "7", "want": "structure" }  // 出 HTML 表格 + LaTeX
{ "path": "C:\\docs\\扫描件.pdf", "want": "text" }                   // 完全禁止 OCR
{ "path": "C:\\docs\\照片.jpg" }                                    // 图片直接 OCR
```

返回：各路页数、逐页耗时、警告、Markdown 正文、输出文件路径。超过 14000 字符的文档会写到 `out`，正文内联截断。

## 已知限制

1. **纯 CPU 机器上慢**。30 页双栏论文走 SDK 大约 1 小时；`auto` 读数字 PDF 是秒级。批量请过夜。
2. **表格是“有结构”，不是“完美”**。小表、多级表头仍可能列错位。
3. **扫描件需要自己部署端点**。插件不会替你下载 1.6 GB 权重。
4. **改代码要重启 harness**。loader 用原生 `import()`，Node 按 URL 缓存；HMR 覆盖不到 profile 里 link 的 workspace 目录。

## 开发

`node selftest.mjs` 可离线检查注册与渲染路径，不用启 harness。

## 发布清单（目前尚未上架）

1. 去掉 `private: true`，补 `repository` / `homepage` / `bugs` / `author`。
2. `LICENSE`（MIT）与位图 `icon-180.png` 已就位——市场卡片拒收 SVG，所以 `icon.svg` 只用于应用内 manifest 图标。
3. 推到公开 GitHub 仓库，加 `dsh-plugin` topic。
4. 可选：`npm publish --access public`（`dsh-pdf-router` 这个名字空着）。
5. 往 [awesome-dsh-plugin](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin) 提 **一个文件**
   `data/plugins/<owner>__dsh-pdf-router.yml`。仓库创建需满 1 天。
