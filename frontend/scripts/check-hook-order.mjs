#!/usr/bin/env node
// Guards against a render-time ReferenceError that the bundler cannot see:
// a hook dependency array is evaluated during render, so a dependency that is
// declared *later* in the same component throws
// "Cannot access 'x' before initialization" the moment the component mounts.
// Only component-scope declarations (two-space indent) are considered, which is
// what keeps this check free of cross-component false positives.

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

const ROOT = new URL('../src', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');
const DECL = /^ {2}(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)/;
const DEP_ARRAY = /^\s*\}\s*,\s*\[([^\]]*)\]\s*\),?\s*$/;

function walk(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (/\.(jsx?|mjs)$/.test(entry)) out.push(full);
  }
  return out;
}

function check(file) {
  const lines = readFileSync(file, 'utf8').split(/\r?\n/);
  const declaredAt = new Map();
  lines.forEach((line, index) => {
    const match = line.match(DECL);
    if (match && !declaredAt.has(match[1])) declaredAt.set(match[1], index + 1);
  });
  const problems = [];
  lines.forEach((line, index) => {
    const match = line.match(DEP_ARRAY);
    if (!match) return;
    for (const raw of match[1].split(',')) {
      const name = raw.trim().split('.')[0];
      if (!name) continue;
      const declared = declaredAt.get(name);
      if (declared && declared > index + 1) {
        problems.push(
          relative(process.cwd(), file) +
            ':' + (index + 1) + " depends on '" + name + "' declared later at line " + declared
        );
      }
    }
  });
  return problems;
}

const problems = walk(ROOT).flatMap(check);
if (problems.length > 0) {
  console.error('Hook dependency order problems (these throw "Cannot access before initialization"):');
  for (const problem of problems) console.error('  ' + problem);
  process.exit(1);
}
console.log('hook dependency order: ok');
