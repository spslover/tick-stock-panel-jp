// 诊断脚本: 找出「模块级中文常量在渲染处(JSX 子节点/属性)没有走 tx()/t()」的写法。
// 审计器(i18n-audit.mjs)对非 JSX 字面量采取「词库已覆盖就放行」策略, 模块常量若在渲染处
// 忘记 tx(), 审计器看不见 —— 这正是本脚本要补的盲区。仅作诊断: 命中项含误报(局部变量、
// 已在回调内部 tx() 的标签、板块标识等), 需人工逐条确认。
// 用法: node scripts/i18n-display-usage.mjs
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const SRC = path.resolve(HERE, '..', 'src')
const CJK = /[\u4e00-\u9fff]/
const FIX = process.argv.includes('--fix')

function listFiles(root) {
  const out = []
  const walk = d => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name)
      if (e.isDirectory()) { if (e.name === 'locales' || e.name === 'node_modules') continue; walk(p) }
      else if (/\.(ts|tsx)$/.test(e.name) && !/\.test\.(ts|tsx)$/.test(e.name)) out.push(p)
    }
  }
  walk(root)
  return out
}

function insideTranslate(node) {
  let cur = node
  while (cur) {
    const p = cur.parent
    if (!p) return false
    if (ts.isCallExpression(p) && ts.isIdentifier(p.expression) && /^(t|tx)$/.test(p.expression.text)) return true
    cur = p
  }
  return false
}

function inJsxScope(node, wantAttr) {
  let c = node.parent
  while (c) {
    if (wantAttr) { if (ts.isJsxAttribute(c)) return true }
    else if (ts.isJsxExpression(c)) return true
    if (ts.isJsxAttribute(c) || ts.isJsxElement(c)) return false
    c = c.parent
  }
  return false
}

const allHits = []
for (const file of listFiles(SRC)) {
  const raw = fs.readFileSync(file, 'utf8')
  const sf = ts.createSourceFile(file, raw, ts.ScriptTarget.Latest, true, file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS)
  const maps = new Map()
  for (const st of sf.statements) {
    if (!ts.isVariableStatement(st)) continue
    for (const d of st.declarationList.declarations) {
      if (!ts.isIdentifier(d.name) || !d.initializer) continue
      let hasCjk = false
      const probe = n => { if (ts.isStringLiteral(n) && CJK.test(n.text)) hasCjk = true; ts.forEachChild(n, probe) }
      probe(d.initializer)
      if (hasCjk) maps.set(d.name.text, d.initializer)
    }
  }
  if (!maps.size) continue
  const rel = path.relative(SRC, file).split(path.sep).join('/')
  const hits = []
  const visit = n => {
    if (ts.isIdentifier(n) && maps.has(n.text)) {
      const decl = maps.get(n.text)
      const isJsxName = ts.isJsxOpeningElement(n.parent) || ts.isJsxSelfClosingElement(n.parent) || ts.isJsxClosingElement(n.parent)
        || (ts.isJsxAttribute(n.parent) && n.parent.name === n)
      if (n !== decl && !isJsxName && !insideTranslate(n)) {
        const attr = inJsxScope(n, true)
        const child = !attr && inJsxScope(n, false)
        if (attr || child) {
          let expr = n
          while (expr.parent && (ts.isPropertyAccessExpression(expr.parent) || ts.isElementAccessExpression(expr.parent)) && expr.parent.expression === expr) expr = expr.parent
          const isCall = ts.isCallExpression(expr.parent) && expr.parent.expression === expr
          const pos = sf.getLineAndCharacterOfPosition(expr.getStart(sf))
          hits.push({ file: rel, path: file, line: pos.line + 1, kind: attr ? 'attr' : 'child', name: n.text, isCall, expr: expr.getText(sf), start: expr.getStart(sf), end: expr.getEnd() })
        }
      }
    }
    ts.forEachChild(n, visit)
  }
  visit(sf)
  if (hits.length) allHits.push({ file, rel, hits })
}

const total = allHits.reduce((n, f) => n + f.hits.length, 0)
if (FIX) {
  for (const { path: file, hits } of allHits) {
    let src = fs.readFileSync(file, 'utf8')
    const sorted = [...hits].sort((a, b) => b.start - a.start)
    for (const h of sorted) {
      if (h.isCall) continue
      src = src.slice(0, h.start) + 'tx(' + h.expr + ')' + src.slice(h.end)
    }
    fs.writeFileSync(file, src, 'utf8')
  }
}

console.log('模块级中文常量在渲染处未走 tx(): ' + total + ' 处')
for (const { rel, hits } of allHits) for (const h of hits) {
  console.log('  ' + rel + ':' + h.line + ' [' + h.name + ' ' + h.kind + (h.isCall ? ' CALL' : '') + '] ' + h.expr.replace(/\s+/g, ' ').slice(0, 80))
}