/**
 * dsh-pdf-router - a routing PDF reader for DeepSeek Harness.
 *
 * One `pdf_read` tool, three engines chosen per page:
 *   - text layer present  -> PyMuPDF extraction (milliseconds, no recognition error)
 *   - no text layer       -> an OpenAI-compatible vision endpoint (MiniCPM-OCR by default)
 *   - structure requested -> MiniCPM-OCR layout pipeline: HTML tables, LaTeX formulas
 *
 * The OCR backend is pluggable on purpose: any server that speaks
 * `POST {base}/chat/completions` with `image_url` parts works, so users are not
 * forced into one deployment. MiniCPM-OCR is recommended because it returns
 * tables and formulas, not just plain text.
 *
 * The plugin imports only Node builtins: it activates in any profile without
 * pulling @deepseek-ai packages into the profile's node_modules.
 */
import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const name = "pdf-router";
export const inject = ["tools"];

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROUTER = path.join(HERE, "router.py");

/** Well-known locations from the reference deployment; all overridable by config. */
const KNOWN = {
  python: [
    "D:\\dsh_configuration\\ocr\\venv\\Scripts\\python.exe",
    "C:\\dsh_configuration\\ocr\\venv\\Scripts\\python.exe",
  ],
  sdk: [
    "D:\\dsh_configuration\\ocr\\venv\\Scripts\\minicpmvocr.exe",
    "C:\\dsh_configuration\\ocr\\venv\\Scripts\\minicpmvocr.exe",
  ],
  layout: [
    "D:\\dsh_configuration\\ocr\\models\\layout",
    "C:\\dsh_configuration\\ocr\\models\\layout",
  ],
};

function firstExisting(candidates, fallback) {
  for (const candidate of candidates) {
    if (candidate && existsSync(candidate)) return candidate;
  }
  return fallback;
}

function resolveBackend(config) {
  return {
    python: firstExisting(
      [config.pythonPath, process.env.DSH_PDF_ROUTER_PYTHON, ...KNOWN.python],
      "python",
    ),
    sdk: firstExisting([config.sdkExe, ...KNOWN.sdk], ""),
    layout: firstExisting([config.layoutModelDir, ...KNOWN.layout], ""),
    ocrEndpoint:
      config.ocrEndpoint || process.env.DSH_PDF_ROUTER_OCR || "http://127.0.0.1:18499/v1",
    ocrModel: config.ocrModel || "minicpm-ocr",
    ocrApiKey: config.ocrApiKey || process.env.DSH_PDF_ROUTER_OCR_KEY || "",
    ocrProfile: config.ocrProfile || "auto",
    ocrPrompt: config.ocrPrompt || "",
  };
}

const ARGS_SCHEMA = {
  type: "object",
  additionalProperties: false,
  properties: {
    path: {
      type: "string",
      description: "Absolute path to a PDF, or to a single image (png/jpg/webp/tif).",
    },
    out: {
      type: "string",
      description: "Optional Markdown output path. Defaults to <input>.md next to the input.",
    },
    want: {
      type: "string",
      enum: ["auto", "text", "structure"],
      description:
        "auto (default): per-page routing - text layer where it exists, OCR for scans, plain text only. " +
        "text: never call OCR. " +
        "structure: layout-aware pass that also returns tables as HTML and formulas as LaTeX; " +
        "use it whenever the page content must stay machine-usable rather than merely readable.",
    },
    pages: {
      type: "string",
      description: "Optional page range such as '1-5' or '3' for PDFs.",
    },
    maxPages: {
      type: "integer",
      description: "Optional cap on how many pages to process (for a quick sample).",
    },
    allowOcr: {
      type: "boolean",
      description: "Set false to forbid OCR entirely: scans then return their (empty) text layer.",
    },
  },
  required: ["path"],
};

const VALUE_SCHEMA = {
  type: "object",
  additionalProperties: false,
  properties: {
    ok: { type: "boolean" },
    engine: { type: "string" },
    out: { type: "string" },
    routes: {
      type: "object",
      additionalProperties: false,
      properties: {
        text: { type: "integer" },
        ocr: { type: "integer" },
        structure: { type: "integer" },
      },
      required: ["text", "ocr", "structure"],
    },
    seconds: { type: "number" },
    pagesText: { type: "string" },
    warnings: { type: "array", items: { type: "string" } },
    markdown: { type: "string" },
  },
  required: ["ok", "engine", "routes", "seconds", "markdown"],
};

function buildArgs(args, backend, config) {
  const argv = [
    ROUTER,
    "--input", args.path,
    "--ocr-base-url", backend.ocrEndpoint,
    "--ocr-model", backend.ocrModel,
    "--ocr-profile", backend.ocrProfile,
    "--want", args.want || config.defaultWant || "auto",
  ];
  if (backend.ocrApiKey) argv.push("--ocr-api-key", backend.ocrApiKey);
  if (backend.ocrPrompt) argv.push("--ocr-prompt", backend.ocrPrompt);
  if (args.out) argv.push("--out", args.out);
  if (args.pages) argv.push("--pages", args.pages);
  if (args.maxPages) argv.push("--max-pages", String(args.maxPages));
  if (backend.sdk) argv.push("--sdk-exe", backend.sdk);
  if (backend.layout) argv.push("--layout-model-dir", backend.layout);
  if (args.allowOcr === false) argv.push("--no-ocr");
  return argv;
}

function runRouter(python, argv, timeoutMs, signal) {
  return new Promise((resolve, reject) => {
    const child = execFile(
      python,
      argv,
      {
        windowsHide: true,
        maxBuffer: 64 * 1024 * 1024,
        timeout: timeoutMs,
        signal,
        encoding: "utf8",
        env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" },
      },
      (error, stdout, stderr) => {
        if (error && !stdout) {
          const tail = (stderr || error.message || "").trim().slice(-1500);
          reject(new Error(`router.py failed: ${tail}`));
          return;
        }
        let parsed;
        try {
          parsed = JSON.parse(String(stdout).trim());
        } catch {
          const tail = (stderr || String(stdout)).trim().slice(-1500);
          reject(new Error(`router.py returned unparsable output: ${tail}`));
          return;
        }
        if (!parsed.ok) {
          reject(new Error(parsed.error || "router.py reported failure"));
          return;
        }
        resolve(parsed);
      },
    );
    child.on("error", reject);
  });
}

function summarize(value) {
  const { routes, pagesText, seconds, engine, warnings, out } = value;
  const parts = [
    `${engine}: ${routes.text} page(s) from the text layer, ${routes.ocr} via OCR`,
    `${routes.structure} via the structure pipeline`,
    `${seconds}s`,
  ];
  const head = parts.join(", ") + (out ? ` -> ${out}` : "");
  const warn = warnings && warnings.length ? `\n${warnings.map((w) => `- ${w}`).join("\n")}` : "";
  return `${head}.${warn}${pagesText ? `\n${pagesText}` : ""}`;
}

/** Large documents are written to `out`; keep the inline copy bounded. */
const INLINE_LIMIT = 14000;

function renderResult(value) {
  const markdown = value.markdown || "";
  const body =
    markdown.length <= INLINE_LIMIT
      ? markdown
      : `${markdown.slice(0, INLINE_LIMIT)}\n\n... truncated (${markdown.length} chars total). Read ${value.out} for the rest.`;
  return [{ type: "text", text: `${summarize(value)}\n\n${body}` }];
}

export function apply(ctx, config = {}) {
  const backend = resolveBackend(config);
  const timeoutMs = config.timeoutMs || 1_800_000;

  ctx.tools.register({
    name: "pdf_read",
    description:
      "Read a PDF or image into Markdown through a per-page router. " +
      "Pages that carry a text layer are extracted directly (fast and exact); pages without one go to a " +
      "vision OCR endpoint (any OpenAI-compatible server; MiniCPM-OCR is the recommended backend). " +
      "'structure' mode runs the layout-aware pipeline instead, so tables come back as HTML <table> " +
      "elements and formulas as LaTeX - use it when the content has to stay machine-usable, " +
      "not just readable. Prefer auto for prose-only documents.",
    parameters: ARGS_SCHEMA,
    output: {
      schema: VALUE_SCHEMA,
      render: (_args, value) => renderResult(value),
    },
    timeoutMs,
    execute: async (args, exec) => {
      if (!args || typeof args.path !== "string" || !args.path.trim()) {
        throw new Error("pdf_read requires a `path`");
      }
      const out = args.out || args.path.replace(/\.[^.\\/]+$/, "") + ".md";
      const argv = buildArgs({ ...args, out }, backend, config);
      const result = await runRouter(backend.python, argv, timeoutMs, exec?.signal);
      return {
        ok: true,
        engine: result.engine,
        out: result.out || "",
        routes: result.routes,
        seconds: result.seconds,
        pagesText: result.pages.length
          ? result.pages.slice(0, 12).map((p) => `p${p.n} ${p.mode} ${p.seconds}s`).join(", ") +
            (result.pages.length > 12 ? ` ... (${result.pages.length} pages)` : "")
          : "",
        warnings: result.warnings || [],
        markdown: result.markdown,
      };
    },
    presentCall: (args) => ({
      card: "generic",
      title: `Read ${path.basename(args.path || "document")}`,
      kind: "other",
      rawInput: { path: args.path, want: args.want || "auto" },
    }),
  });

  // Touch the resolver once so a misconfigured python or endpoint surfaces in the
  // log instead of on the first call.
  try {
    ctx.logger?.debug?.(
      `[pdf-router] python=${backend.python} ocr=${backend.ocrEndpoint} model=${backend.ocrModel}`,
    );
  } catch {
    /* logging is best-effort */
  }
}
