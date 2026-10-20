// Runs the prototype's file classifier (prototype/app.js, from HEAD_BYTES
// to handleFiles) on each path given and prints "path<TAB>label" per file,
// with "-" when the classifier rejects it. tests/test_prototype.py drives it.
import { readFileSync } from "node:fs";

const [app, ...paths] = process.argv.slice(2);
const src = readFileSync(app, "utf8");
const code = src.slice(src.indexOf("const HEAD_BYTES"), src.indexOf("async function handleFiles")) + "\nexport { classify };";
const { classify } = await import("data:text/javascript," + encodeURIComponent(code));
for (const p of paths) {
  const found = await classify(new Blob([readFileSync(p)]));
  console.log(`${p}\t${found ? found.label : "-"}`);
}
