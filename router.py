#!/usr/bin/env python
"""Routing PDF reader used by the dsh-pdf-router plugin.

Per page it picks the cheapest engine that can actually do the job:

  text layer exists        -> PyMuPDF extraction (milliseconds, no recognition error)
  no text layer            -> an OpenAI-compatible vision endpoint (default: local MiniCPM-OCR)
  structure requested      -> MiniCPM-OCR layout pipeline (tables as HTML, formulas as LaTeX)

The OCR side is deliberately provider-agnostic: any server that speaks
`POST {base}/chat/completions` with `image_url` content parts works, so the
plugin is not tied to one deployment. MiniCPM-OCR is the recommended backend
because it emits tables and formulas, not just plain text.

Emits one JSON object on stdout so the plugin never has to parse prose.

Usage:
  python router.py --input doc.pdf [--out doc.md] [--want auto|text|structure]
      [--ocr-base-url http://127.0.0.1:18499/v1] [--ocr-model minicpm-ocr]
      [--ocr-api-key KEY] [--ocr-profile auto|minicpm|generic] [--ocr-prompt TEXT]
      [--sdk-exe PATH] [--layout-model-dir PATH]
      [--pages 1-5] [--max-pages N] [--timeout 900] [--no-ocr] [--force-ocr]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz  # PyMuPDF < 1.24
    except ImportError:
        # A missing dependency must be actionable, not a traceback the plugin
        # cannot parse: users run this against whatever python is on PATH.
        print(json.dumps({
            "ok": False,
            "error": "PyMuPDF is missing. Install it with `pip install pymupdf`, "
                     "or point the plugin's pythonPath config at an environment that has it.",
        }))
        raise SystemExit(2)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
# A page with fewer printable characters than this, but at least one image, is a scan.
TEXT_PAGE_MIN_CHARS = 120
RENDER_ZOOM = 2.0

DEFAULT_BASE = "http://127.0.0.1:18499/v1"
PROFILE_PROMPTS = {
    # MiniCPM-OCR is trained on these exact instruction prefixes.
    "minicpm": "Text Recognition:",
    # Any general vision model: ask for structure explicitly, since nothing else will.
    "generic": (
        "Transcribe this document page into Markdown. Keep the reading order of "
        "columns. Render every table as an HTML <table> element and every formula "
        "as LaTeX. Do not summarise or add commentary."
    ),
}

# Windows consoles default to a legacy code page (GBK on a Chinese system) that
# cannot encode characters such as the "fi" ligature present in real papers, so
# force UTF-8 on the pipe the plugin reads.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------- text

def _lines(page) -> list[tuple[float, float, float, float, str]]:
    out = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line.get("spans", [])).strip()
            if text:
                x0, y0, x1, y1 = line["bbox"]
                out.append((x0, y0, x1, y1, text))
    return out


def _two_column(lines, width: float, height: float) -> bool:
    if len(lines) < 6:
        return False
    mid, tol = width / 2, width * 0.03
    left = [ln for ln in lines if ln[2] <= mid + tol]
    right = [ln for ln in lines if ln[0] >= mid - tol]
    span = len(lines) - len(left) - len(right)

    def vspan(items):
        return (max(i[3] for i in items) - min(i[1] for i in items)) / height if items else 0.0

    return (len(left) >= 3 and len(right) >= 3 and span <= len(lines) * 0.6
            and vspan(left) > 0.12 and vspan(right) > 0.12)


def reading_order(page) -> str:
    """Page text in reading order, with a simple two-column reflow."""
    lines = _lines(page)
    if not lines:
        return ""
    w, h = page.rect.width, page.rect.height
    if not _two_column(lines, w, h):
        lines.sort(key=lambda ln: (round(ln[1], 1), ln[0]))
        return "\n".join(ln[4] for ln in lines)

    mid, tol = w / 2, w * 0.03
    spanning, left, right = [], [], []
    for ln in lines:
        if ln[0] < mid - tol and ln[2] > mid + tol:
            spanning.append(ln)
        elif ln[2] <= mid + tol:
            left.append(ln)
        elif ln[0] >= mid - tol:
            right.append(ln)
        else:
            spanning.append(ln)
    spanning.sort(key=lambda ln: ln[1])

    parts: list[str] = []
    band_top = -1e9
    for sep in spanning + [None]:
        band_bottom = sep[1] - 0.5 if sep else 1e9
        parts.extend(ln[4] for ln in sorted([x for x in left if band_top <= x[1] < band_bottom], key=lambda x: x[1]))
        parts.extend(ln[4] for ln in sorted([x for x in right if band_top <= x[1] < band_bottom], key=lambda x: x[1]))
        if sep is not None:
            parts.append(sep[4])
            band_top = sep[3]
    return "\n".join(parts)


# ----------------------------------------------------------------- OCR backend

def _headers(cfg: dict) -> dict:
    headers = {"Content-Type": "application/json"}
    key = (cfg.get("ocr_api_key") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _base(cfg: dict) -> str:
    return (cfg.get("ocr_base_url") or DEFAULT_BASE).rstrip("/")


def _prompt(cfg: dict) -> str:
    explicit = (cfg.get("ocr_prompt") or "").strip()
    if explicit:
        return explicit
    profile = cfg.get("ocr_profile") or "auto"
    if profile == "auto":
        # MiniCPM deployments answer the trained prefix; anything else gets instructions.
        base = _base(cfg).lower()
        profile = "minicpm" if ("18499" in base or "minicpm" in base) else "generic"
    return PROFILE_PROMPTS.get(profile, PROFILE_PROMPTS["generic"])


def probe(cfg: dict) -> tuple[bool, str]:
    """Is the endpoint usable? An auth/404 answer still proves a server is there."""
    base = _base(cfg)
    try:
        req = urllib.request.Request(f"{base}/models", headers=_headers(cfg))
        with urllib.request.urlopen(req, timeout=6):
            return True, ""
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404, 405):
            return True, ""
        return False, f"HTTP {exc.code} from {base}/models"
    except urllib.error.URLError as exc:
        return False, f"{base} unreachable ({exc.reason})"
    except Exception as exc:  # noqa: BLE001
        return False, f"{base} unusable ({type(exc).__name__}: {exc})"


def _chat(cfg: dict, image_bytes: bytes, mime: str = "png") -> str:
    """One vision call against any OpenAI-compatible endpoint."""
    b64 = base64.b64encode(image_bytes).decode()
    body = {
        "model": cfg.get("ocr_model") or "minicpm-ocr",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _prompt(cfg)},
                {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{b64}"}},
            ],
        }],
        "max_tokens": 8192,
        "temperature": 0.01,
        "top_p": 0.001,
        "stream": False,
    }
    req = urllib.request.Request(
        f"{_base(cfg)}/chat/completions",
        data=json.dumps(body).encode(),
        headers=_headers(cfg),
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.get("timeout", 900)) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise RuntimeError(f"OCR endpoint answered HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OCR endpoint unreachable at {_base(cfg)}: {exc.reason}") from exc

    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError(f"OCR endpoint returned no choices: {str(payload)[:300]}")
    content = choices[0].get("message", {}).get("content")
    if isinstance(content, list):  # some servers return content parts
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(content or "")


def ocr_page(page, cfg: dict) -> str:
    pix = page.get_pixmap(matrix=fitz.Matrix(RENDER_ZOOM, RENDER_ZOOM), colorspace=fitz.csRGB, alpha=False)
    return _chat(cfg, pix.tobytes("png"))


def ocr_image(path: Path, cfg: dict) -> str:
    suffix = path.suffix.lower().lstrip(".") or "png"
    mime = "jpeg" if suffix in {"jpg", "jpeg"} else suffix
    return _chat(cfg, path.read_bytes(), mime=mime)


# -------------------------------------------------------------------- structure

def structure_parse(pdf: Path, out: Path, cfg: dict) -> str:
    """Layout-aware pass: PP-DocLayoutV3 blocks, per-block prompts, HTML tables."""
    exe = cfg.get("sdk_exe") or ""
    if not exe or not Path(exe).exists():
        raise RuntimeError(
            "structure mode needs the MiniCPM-OCR SDK (minicpmvocr). "
            "Install it, or set the sdkExe config, or use want: auto for plain text."
        )
    env = dict(os.environ)
    if cfg.get("layout_model_dir"):
        env["OCR_LAYOUT_MODEL_DIR"] = cfg["layout_model_dir"]
    env.setdefault("OCR_LAYOUT_DEVICE", "cpu")
    env.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    env.setdefault("OMP_NUM_THREADS", "8")
    base = _base(cfg)
    server = base[:-3] if base.endswith("/v1") else base
    cmd = [exe, "parse", str(pdf), "-o", str(out),
           "--device", "cpu",
           "--server-url", server, "--model", cfg.get("ocr_model") or "minicpm-ocr",
           "--backend", "llamacpp",
           "--set", "pipeline.queue_mode=serial", "--set", "pipeline.max_workers=1"]
    if cfg.get("layout_model_dir"):
        cmd += ["--layout-model", cfg["layout_model_dir"]]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=cfg.get("timeout", 1800))
    if proc.returncode != 0 and not out.exists():
        tail = (proc.stderr or proc.stdout or "")[-800:]
        raise RuntimeError(f"SDK parse failed (exit {proc.returncode}): {tail}")
    return out.read_text(encoding="utf-8")


def structure_run(src: Path, cfg: dict) -> dict:
    """Structure mode, honouring --pages / --max-pages by slicing a temporary PDF."""
    out = Path(cfg["out"]) if cfg.get("out") else src.with_suffix(".sdk.md")
    selected: list[int] = []
    if src.suffix.lower() == ".pdf" and (cfg.get("pages") or cfg.get("max_pages")):
        doc = fitz.open(src)
        selected = parse_pages(cfg["pages"]) if cfg.get("pages") else list(range(doc.page_count))
        if cfg.get("max_pages"):
            selected = selected[: cfg["max_pages"]]
        tmp = fitz.open()
        for idx in selected:
            if 0 <= idx < doc.page_count:
                tmp.insert_pdf(doc, from_page=idx, to_page=idx)
        doc.close()
        if tmp.page_count == 0:
            tmp.close()
            raise RuntimeError("no pages selected")
        slice_path = Path(tempfile.mkdtemp(prefix="pdf-router-")) / "slice.pdf"
        tmp.save(slice_path)
        tmp.close()
        target = slice_path
    else:
        target = src

    markdown = structure_parse(target, out, cfg)
    return {
        "ok": True, "engine": "minicpm-ocr-sdk", "out": str(out), "markdown": markdown,
        "pages": [{"n": n + 1, "mode": "structure", "chars": 0, "images": 0, "seconds": 0}
                  for n in (selected or [])],
        "warnings": [], "seconds": 0,
        "routes": {"text": 0, "ocr": 0, "structure": len(selected) or 1},
    }


# ------------------------------------------------------------------------- main

def parse_pages(spec: str) -> list[int]:
    m = re.match(r"^(\d+)(?:-(\d+))?$", spec.strip())
    if not m:
        raise SystemExit(f"bad --pages value: {spec}")
    a = int(m.group(1))
    b = int(m.group(2) or m.group(1))
    return list(range(a - 1, b))


def run(cfg: dict) -> dict:
    src = Path(cfg["input"])
    if not src.exists():
        raise SystemExit(f"input not found: {src}")
    out = Path(cfg["out"]) if cfg.get("out") else None
    want = cfg.get("want", "auto")
    started = time.perf_counter()
    warnings: list[str] = []

    if want == "structure":
        available, reason = probe(cfg)
        if not available:
            raise SystemExit(f"structure mode requires the OCR endpoint: {reason}")
        result = structure_run(src, cfg)
        result["seconds"] = round(time.perf_counter() - started, 1)
        return result

    if src.suffix.lower() in IMAGE_SUFFIXES:
        available, reason = probe(cfg)
        if not available:
            raise SystemExit(f"image input needs an OCR endpoint: {reason}")
        text = ocr_image(src, cfg)
        if out:
            out.write_text(text, encoding="utf-8")
        return {
            "ok": True, "engine": "ocr-endpoint", "out": str(out) if out else None,
            "markdown": text, "pages": [{"n": 1, "mode": "ocr", "chars": 0, "seconds": 0}],
            "warnings": warnings, "seconds": round(time.perf_counter() - started, 1),
            "routes": {"text": 0, "ocr": 1, "structure": 0},
        }

    doc = fitz.open(src)
    targets = parse_pages(cfg["pages"]) if cfg.get("pages") else list(range(doc.page_count))
    if cfg.get("max_pages"):
        targets = targets[: cfg["max_pages"]]

    # Probe lazily: a born-digital PDF must not pay for an OCR round trip.
    ocr_available, ocr_reason = (False, "OCR disabled by request") if cfg.get("no_ocr") else probe(cfg)

    pages_meta: list[dict] = []
    chunks: list[str] = []
    n_text = n_ocr = 0

    for idx in targets:
        page = doc[idx]
        raw = page.get_text("text") or ""
        chars = len(re.sub(r"\s+", "", raw))
        images = len(page.get_images(full=True))
        t0 = time.perf_counter()
        mode = "text"

        scanned = cfg.get("force_ocr") or (chars < TEXT_PAGE_MIN_CHARS and images > 0)
        if scanned and ocr_available:
            try:
                body = ocr_page(page, cfg)
                mode = "ocr"
            except Exception as exc:  # keep going page by page
                warnings.append(f"page {idx + 1}: OCR failed ({exc})")
                body = reading_order(page)
        else:
            body = reading_order(page)
            if scanned and not ocr_available:
                warnings.append(f"page {idx + 1}: looks scanned but OCR is unavailable ({ocr_reason})")

        if mode == "ocr":
            n_ocr += 1
        else:
            n_text += 1
        dt = round(time.perf_counter() - t0, 2)
        pages_meta.append({"n": idx + 1, "mode": mode, "chars": chars, "images": images, "seconds": dt})
        chunks.append(f"<!-- page {idx + 1} [{mode}] -->\n\n{body.strip()}\n")

    doc.close()
    markdown = f"# {src.name}\n\n" + "\n---\n\n".join(chunks)
    if out:
        out.write_text(markdown, encoding="utf-8")

    return {
        "ok": True,
        "engine": "ocr-endpoint" if n_ocr else "pymupdf",
        "out": str(out) if out else None,
        "markdown": markdown,
        "pages": pages_meta,
        "warnings": warnings,
        "seconds": round(time.perf_counter() - started, 1),
        "routes": {"text": n_text, "ocr": n_ocr, "structure": 0},
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="routing PDF reader")
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--want", default="auto", choices=["auto", "text", "structure"])
    ap.add_argument("--pages", default="")
    ap.add_argument("--max-pages", type=int, default=0)
    ap.add_argument("--ocr-base-url", default="", help=f"OpenAI-compatible base URL (default {DEFAULT_BASE})")
    # Legacy flag kept so an already-loaded plugin build keeps working across a
    # router.py update: `--server http://host:port` means `--ocr-base-url .../v1`.
    ap.add_argument("--server", default="", help=argparse.SUPPRESS)
    ap.add_argument("--ocr-model", default="", help="model name sent to the endpoint")
    ap.add_argument("--ocr-api-key", default="", help="bearer token, if the endpoint needs one")
    ap.add_argument("--ocr-profile", default="auto", choices=["auto", "minicpm", "generic"])
    ap.add_argument("--ocr-prompt", default="", help="override the instruction text")
    ap.add_argument("--sdk-exe", default="", help="minicpmvocr executable, structure mode only")
    ap.add_argument("--layout-model-dir", default="", help="PP-DocLayoutV3 dir, structure mode only")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--force-ocr", action="store_true")
    ap.add_argument("--no-ocr", action="store_true")
    args = ap.parse_args(argv)

    base = args.ocr_base_url
    if not base and args.server:
        base = args.server.rstrip("/") + "/v1"
    cfg = {
        "input": args.input,
        "out": args.out or None,
        "want": "auto" if args.want == "text" else args.want,
        "pages": args.pages or None,
        "max_pages": args.max_pages,
        "ocr_base_url": base or DEFAULT_BASE,
        "ocr_model": args.ocr_model,
        "ocr_api_key": args.ocr_api_key,
        "ocr_profile": args.ocr_profile,
        "ocr_prompt": args.ocr_prompt,
        "sdk_exe": args.sdk_exe,
        "layout_model_dir": args.layout_model_dir,
        "timeout": args.timeout,
        "force_ocr": args.force_ocr,
        "no_ocr": args.no_ocr,
    }
    try:
        result = run(cfg)
    except SystemExit as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    except Exception as exc:  # noqa: BLE001 - the plugin turns this into a tool error
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
