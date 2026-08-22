import { copyFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const out = join(root, "public", "fonts");

const fonts = [
  ["@fontsource-variable/roboto", "roboto-latin-wght-normal.woff2"],
  ["@fontsource-variable/roboto-mono", "roboto-mono-latin-wght-normal.woff2"],
];

mkdirSync(out, { recursive: true });
for (const [pkg, file] of fonts) {
  copyFileSync(join(root, "node_modules", pkg, "files", file), join(out, file));
  console.log(`fonts: ${file}`);
}
