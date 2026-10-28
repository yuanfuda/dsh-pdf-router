// Strip a UTF-8 BOM from JSON/YAML files that Windows PowerShell wrote.
// PS 5.1 `Set-Content -Encoding utf8` prepends EF BB BF, which the DSH loader
// rejects in package.json and which some YAML parsers choke on.
import fs from "node:fs";

for (const file of process.argv.slice(2)) {
  if (!fs.existsSync(file)) {
    console.log(`skip (missing): ${file}`);
    continue;
  }
  const buf = fs.readFileSync(file);
  const hadBom = buf.length >= 3 && buf[0] === 0xef && buf[1] === 0xbb && buf[2] === 0xbf;
  if (!hadBom) {
    console.log(`ok (no BOM): ${file}`);
    continue;
  }
  fs.writeFileSync(file, buf.subarray(3));
  console.log(`stripped BOM: ${file}`);
}
