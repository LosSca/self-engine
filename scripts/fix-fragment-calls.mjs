// This sandbox build rewrites every `jsx(Fragment, null, <children>)` back into
// `jsxRuntimeExports.Fragment({ children: <children> })`. React Fragment is a
// symbol, not a function, so the page dies with
// "jsxRuntimeExports.Fragment is not a function" the moment a view renders an
// inline fragment. Neither esbuild options nor a noop esbuild loader stop it, so
// the bundle is corrected after the build instead: drop the bogus `children`
import { existsSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";

// Resolve the asset directory from this file's own location, never from a constant:
// the repo used to live at E:/shujia, and a hard-coded path there made every
// `npm run build` end in ENOENT even though vite itself had succeeded.
const here = fileURLToPath(new URL(".", import.meta.url));
const DEFAULT_ASSETS = resolve(here, "..", "frontend", "dist", "assets");

// `npm run build` passes "dist/assets" from the frontend directory; resolve it
// against the caller's cwd. A bare "dist/assets" that does not exist is never
// swapped for the default silently - that is how a hard-coded path here used to
// make the patch step look successful while touching nothing.
const requested = process.env.FRAGMENT_ASSETS || process.argv[2];
const ASSETS = requested ? resolve(process.cwd(), requested) : DEFAULT_ASSETS;

if (!existsSync(ASSETS)) {
  // Nothing to patch is not a build failure: this step post-processes `vite build`
  // output, so a build without an assets directory simply has no fragments.
  console.log("fragment-calls: no assets directory at " + ASSETS + " - nothing to do");
  process.exit(0);
}

const MARKER = "jsxRuntimeExports.Fragment(";

// Find the index of the bracket that closes the one at "open", skipping strings.
function matchBracket(code, open) {
  const pairs = { "(": ")", "[": "]", "{": "}" };
  const stack = [];
  let i = open;
  while (i < code.length) {
    const ch = code[i];
    if (ch === '"' || ch === "'" || ch === "`") {
      const quote = ch;
      i += 1;
      while (i < code.length) {
        if (code[i] === "\\") { i += 2; continue; }
        if (code[i] === quote) break;
        i += 1;
      }
    } else if (pairs[ch]) {
      stack.push(pairs[ch]);
    } else if (ch === ")" || ch === "]" || ch === "}") {
      if (stack.length === 0) return -1;
      if (stack.pop() !== ch) return -1;
      if (stack.length === 0) return i;
    }
    i += 1;
  }
  return -1;
}

// Locate the single property named "children" in an object-literal body.
function childValue(body) {
  const key = "children:";
  const at = body.indexOf(key);
  if (at < 0) return null;
  const bare = body[at - 1];
  if (bare !== undefined && !/[\s{,]/.test(bare)) return null;
  const start = at + key.length;
  let i = start;
  while (i < body.length && /\s/.test(body[i])) i += 1;
  if (i >= body.length) return null;
  const first = body[i];
  if (first === "(" || first === "[" || first === "{") {
    const close = matchBracket(body, i);
    if (close < 0) return null;
    let end = close + 1;
    if (first === "(") {
      // Drop the wrapping parens; they were only there to hold the expression.
      return body.slice(i + 1, close);
    }
    return body.slice(i, end);
  }
  // Anything else is a primary expression or a conditional; cut at the next
  // top-level comma so a following "key:" prop is not swallowed.
  let depth = 0;
  for (let j = i; j < body.length; j += 1) {
    const ch = body[j];
    if (ch === '"' || ch === "'" || ch === "`") {
      const quote = ch;
      j += 1;
      while (j < body.length) {
        if (body[j] === "\\") { j += 2; continue; }
        if (body[j] === quote) break;
        j += 1;
      }
      continue;
    }
    if (ch === "(" || ch === "[" || ch === "{") depth += 1;
    else if (ch === ")" || ch === "]" || ch === "}") depth -= 1;
    else if (ch === "," && depth === 0) return body.slice(i, j);
  }
  return body.slice(i);
}

function rewrite(code) {
  let out = code;
  let fixed = 0;
  let cursor = 0;
  for (;;) {
    const at = out.indexOf(MARKER, cursor);
    if (at < 0) break;
    const openParen = at + MARKER.length - 1;
    const closeParen = matchBracket(out, openParen);
    if (closeParen < 0) break;
    const argBody = out.slice(openParen + 1, closeParen - 1);
    const child = childValue(argBody);
    const replacement = child === null
      ? "jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, null)"
      : "jsxRuntimeExports.jsx(jsxRuntimeExports.Fragment, null, " + child.trim() + ")";
    // The bogus call often sat inside an expression that already closed with
    // its own ")", which the call swallowed; one extra one is usually left over.
    const after = closeParen + 1;
    out = out.slice(0, at) + replacement + out.slice(after);
    cursor = at + replacement.length;
    fixed += 1;
  }
  return { code: out, fixed };
}


function syntaxOk(path) {
  try {
    execFileSync(process.execPath, ["--check", path], { stdio: "ignore" });
    return true;
  } catch (error) {
    return false;
  }
}

let total = 0;
const broken = [];
for (const name of readdirSync(ASSETS)) {
  if (!name.endsWith(".js")) continue;
  const path = join(ASSETS, name);
  const before = readFileSync(path, "utf8");
  if (!before.includes(MARKER)) continue;
  const { code, fixed } = rewrite(before);
  if (fixed > 0) writeFileSync(path, code, "utf8");
  total += fixed;
  // Verify every asset that still contains the marker, patched or not: a bundle
  // left unparsable by the rewrite must fail here, not in the browser.
  if (!syntaxOk(path)) broken.push(name);
  console.log("fragment-calls: " + name + " -> " + fixed);
}
console.log("fragment-calls rewritten: " + total);
if (broken.length > 0) {
  console.error("fragment-calls: still unparsable: " + broken.join(", "));
  process.exitCode = 1;
}
