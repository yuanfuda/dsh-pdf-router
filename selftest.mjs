// Offline end-to-end check of index.js - no harness needed.
// Registers against a fake context, then runs a real pdf_read through the
// definition's execute() and render(), so the tool contract is exercised the
// same way the agent would exercise it.
import { apply, inject, name } from "./index.js";

const captured = [];
const ctx = {
  tools: { register: (def) => captured.push(def) },
  logger: { debug: (m) => console.log("[debug]", m) },
};

apply(ctx, {});
console.log("plugin:", name, "| inject:", JSON.stringify(inject));
console.log("tools registered:", captured.map((d) => d.name).join(", "));

const def = captured[0];
console.log("has output.schema:", Boolean(def.output?.schema), "| execute:", typeof def.execute);
console.log("params required:", JSON.stringify(def.parameters.required));

const target = process.argv[2] || "D:\\dsh_configuration\\samples\\attention.pdf";
console.log(`\n--- execute({ path: ${target}, maxPages: 1 }) ---`);
const t0 = Date.now();
const value = await def.execute(
  { path: target, maxPages: 1, out: process.env.TEMP + "\\_selftest_out.md" },
  {},
);
console.log("returned keys:", Object.keys(value).join(", "));
console.log("routes:", JSON.stringify(value.routes), "| engine:", value.engine, "| ms:", Date.now() - t0);
console.log("warnings:", value.warnings.length ? value.warnings : "(none)");

// output.schema declares additionalProperties: false - make sure we match it.
const allowed = new Set(Object.keys(def.output.schema.properties));
const extra = Object.keys(value).filter((k) => !allowed.has(k));
console.log("keys outside output.schema:", extra.length ? extra : "(none)");

const blocks = def.output.render({ path: target }, value);
const text = blocks.map((b) => b.text).join("");
console.log("\n--- rendered (first 400 chars) ---");
console.log(text.slice(0, 400));
console.log("\ncontains body:", text.length > 200, "| inline limit respected:", text.length < 16000);
