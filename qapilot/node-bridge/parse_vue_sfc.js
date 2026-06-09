#!/usr/bin/env node
/**
 * Vue SFC parser bridge (PoC 2).
 *
 * Usage:
 *   node parse_vue_sfc.js <vue_file_path>
 *
 * Output (stdout, JSON):
 *   {
 *     "ok": true,
 *     "file": "...",
 *     "template": {
 *       "ast": { ... }            // raw @vue/compiler-sfc template AST (NodeTypes 트리)
 *     },
 *     "script": { "lang": "ts"|"js", "content": "..." },   // optional
 *     "errors": []
 *   }
 *
 * 에러:
 *   { "ok": false, "error": "<msg>", "stack": "<optional>" }
 *
 * 본 script 는 본인 영역 PoC 2. 추출 정책 (어떤 element 를 catalog 에 넣을지) 은
 * Python wrapper 가 결정 — 본 script 는 AST 만 그대로 반환.
 */

'use strict';

const fs = require('fs');
const path = require('path');

function main() {
  const args = process.argv.slice(2);
  if (args.length < 1) {
    process.stdout.write(JSON.stringify({
      ok: false,
      error: 'Usage: node parse_vue_sfc.js <vue_file_path>',
    }), () => process.exit(2));
  }

  const filePath = args[0];
  if (!fs.existsSync(filePath)) {
    process.stdout.write(JSON.stringify({
      ok: false,
      error: `File not found: ${filePath}`,
    }), () => process.exit(2));
  }

  let parseFn;
  try {
    parseFn = require('@vue/compiler-sfc').parse;
  } catch (e) {
    process.stdout.write(JSON.stringify({
      ok: false,
      error: `Cannot load @vue/compiler-sfc — run 'npm install' inside node-bridge/. ${e.message}`,
    }), () => process.exit(3));
  }

  let content;
  try {
    content = fs.readFileSync(filePath, 'utf-8');
  } catch (e) {
    process.stdout.write(JSON.stringify({
      ok: false,
      error: `Cannot read file: ${e.message}`,
    }), () => process.exit(2));
  }

  let result;
  try {
    result = parseFn(content, { filename: path.basename(filePath) });
  } catch (e) {
    process.stdout.write(JSON.stringify({
      ok: false,
      error: `Vue SFC parse failed: ${e.message}`,
      stack: e.stack,
    }), () => process.exit(4));
  }

  const desc = result.descriptor;
  const out = {
    ok: true,
    file: filePath,
    errors: (result.errors || []).map((err) => ({
      message: err.message,
      loc: err.loc ? {
        start: { line: err.loc.start.line, column: err.loc.start.column },
        end: { line: err.loc.end.line, column: err.loc.end.column },
      } : null,
    })),
  };

  if (desc.template) {
    out.template = {
      lang: desc.template.lang || 'html',
      ast: desc.template.ast || null,
    };
  } else {
    out.template = null;
  }

  if (desc.script) {
    out.script = {
      lang: desc.script.lang || 'js',
      content: desc.script.content,
      loc_start_line: desc.script.loc?.start?.line || null,
    };
  }
  if (desc.scriptSetup) {
    out.script_setup = {
      lang: desc.scriptSetup.lang || 'js',
      content: desc.scriptSetup.content,
      loc_start_line: desc.scriptSetup.loc?.start?.line || null,
    };
  }

  // stdout flush 보장 — 큰 출력 (수십 KB+) 시 Node 가 write callback 전에
  // exit 하면 데이터 잘림. callback 안에서 명시적 exit.
  process.stdout.write(JSON.stringify(out), () => process.exit(0));
}

main();
