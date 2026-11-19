// 扫描「模块级中文常量 + 词库已覆盖」的位置。
//
// i18n-audit.mjs 对非 JSX 字面量采用「词库有对应条目就放行」策略(认为是数据键)。
// 但模块级常量如果其实是展示文案(例如 const SECTIONS = [{ label: '扩展页面' }]),
// 词库里一旦有该短语, 审计就会放行, 而渲染处若没走 tx() 切语言后仍是中文 ——
// 属于审计看不见的盲区。本脚本把这些位置列出来供人工确认。
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const SRC = path.resolve(HERE, '..', 'src')
const CJK = /[一-鿿]/

function loadCovered(root) {
  const covered = new Set()
  const phrases = fs.readFileSync(path.join(root, 'locales', 'phrases.ts'), 'utf8')
  const keyRe = /^\s*'((?:[^'\\]|\\.)*)'\s*:/gm
  let m
  while ((m = keyRe.exec(phrases))) covered.add(m[1].replace(/\\'/g, "'"))
  return covered
}

function isModuleScope(node) {
  let cur = node
  while (cur) {
    if (ts.isFunctionDeclaration(cur) || ts.isFunctionExpression(cur) || ts.isArrowFunction(cur)
      || ts.isMethodDeclaration(cur) || ts.isGetAccessor(cur) || ts.isSetAccessor(cur)
      || ts.isConstructorDeclaration(cur)) return false
    if (ts.isSourceFile(cur)) return true
    cur = cur.parent
  }
  return false
}

function listFiles(root) {
  const out = []
  const walk = dir => {
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, e.name)
      if (e.isDirectory()) {
        if (e.name === 'locales' || e.name === 'node_modules') continue
        walk(p)
      } else if (/\.(ts|tsx)$/.test(e.name) && !/\.test\.(ts|tsx)$/.test(e.name)) out.push(p)
    }
  }
  walk(root)
  return out
}

const covered = loadCovered(SRC)
let total = 0
const rows = []
for (const file of listFiles(SRC)) {
  const rel = path.relative(SRC, file).split(path.sep).join('/')
  const raw = fs.readFileSync(file, 'utf8')
  const sf = ts.createSourceFile(file, raw, ts.ScriptTarget.Latest, true, file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS)
  const hits = []
  const visit = node => {
    if (ts.isStringLiteral(node) && !ts.isJsxAttribute(node.parent)) {
      const p = node.parent
      const localized = ts.isCallExpression(p) && /^(t|tx)$/.test(p.expression.getText(sf))
      if (!localized && CJK.test(node.text) && isModuleScope(node)) {
        const key = node.text.trim()
        if (covered.has(key)) {
          hits.push({ line: sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1, text: key })
        }
      }
    }
    ts.forEachChild(node, visit)
  }
  visit(sf)
  if (hits.length) { rows.push([rel, hits]); total += hits.length }
}
rows.sort((a, b) => b[1].length - a[1].length)
console.log('模块级「词库已覆盖」中文常量: ' + total + ' 处 / ' + rows.length + ' 个文件')
for (const [rel, hits] of rows) {
  console.log('  ' + hits.length + '\t' + rel)
  for (const h of hits) console.log('      L' + h.line + ' ' + h.text.slice(0, 70))
}
