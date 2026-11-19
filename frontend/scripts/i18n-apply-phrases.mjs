// 基于 TypeScript AST 的 i18n applier：精确判断每个中文字面量/JSX 文本所处的位置，
// 只对「展示型」位置自动包裹 tx()，其余（数据常量、比较值、API 入参）一律报告。
// 用法: node scripts/i18n-apply-phrases.mjs <file> <phrases.json>
import fs from 'node:fs'
import path from 'node:path'
import ts from 'typescript'

const NL = String.fromCharCode(10)
const CJK = /[\u4e00-\u9fff]/
const HERE = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'))
const SRC = path.resolve(HERE, '..', 'src')
const file = path.resolve(process.cwd(), process.argv[2])
const phrases = JSON.parse(fs.readFileSync(path.resolve(process.cwd(), process.argv[3]), 'utf8'))
const raw = fs.readFileSync(file, 'utf8')

const sf = ts.createSourceFile(file, raw, ts.ScriptTarget.Latest, true, file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS)

const DISPLAY_PROPS = new Set([
  'label', 'title', 'text', 'desc', 'description', 'hint', 'placeholder',
  'ariaLabel', 'subtitle', 'note', 'tip', 'tooltip', 'emptyText', 'unit',
])
const DATA_CALLS = /^(api\.|set[A-Z]|console\.|toast$|reportError$)/

// 统一的单引号字符串转义: 换行/制表符/反斜杠都要转义, 否则多行字面量写回源码会语法错误
const q = s => "'" + String(s).replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n').replace(/\r/g, '\\r').replace(/\t/g, '\\t') + "'"

const edits = []
const report = []
const used = new Set()


// JSX 文本节点的首尾空格是有语义的（` 个`、` 显示 ` 之类）。
// Babel 会去掉「跨行」的首尾空白，但保留同一行内的空格，
// 包裹成 {tx(...)} 时必须把这些空格原样还回去，否则渲染会粘连。
function jsxEdges(raw) {
  let lead = ''
  let trail = ''
  const firstNl = raw.indexOf('\n')
  if (firstNl === -1) {
    lead = (raw.match(/^[ \t]*/) || [''])[0]
    trail = (raw.match(/[ \t]*$/) || [''])[0]
  } else {
    const firstLine = raw.slice(0, firstNl)
    if (firstLine.trim() !== '') lead = (firstLine.match(/^[ \t]*/) || [''])[0]
    const lastNl = raw.lastIndexOf('\n')
    const lastLine = raw.slice(lastNl + 1)
    if (lastLine.trim() !== '') trail = (lastLine.match(/[ \t]*$/) || [''])[0]
  }
  return { lead, trail }
}


// 模块级常量在 import 时求值：tx() 会被冻结在当时的语言，
// 切语言后不刷新。这类位置不能自动包 tx()，一律报告交人工处理。
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

// 字符串字面量里的首尾空格是有语义的(如 '本机 ' + v),
// 包裹成 tx() 时必须把空格还原到调用两侧, 否则渲染会粘连。
function txText(node, key) {
  const raw = node.text
  const lead = (raw.match(/^\s*/) || [''])[0]
  const trail = (raw.match(/\s*$/) || [''])[0]
  const q = s => "'" + s.replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n').replace(/\r/g, '\\r').replace(/\t/g, '\\t') + "'"
  const call = 'tx(' + q(key) + ')'
  return (lead ? q(lead) + ' + ' : '') + call + (trail ? ' + ' + q(trail) : '')
}

function classify(node) {
  if (isModuleScope(node)) return 'module'
  const parent = node.parent
  if (ts.isCallExpression(parent) || ts.isNewExpression(parent)) {
    const callee = parent.expression.getText(sf)
    if (/^(t|tx)$/.test(callee)) return 'skip'
    if (DATA_CALLS.test(callee)) return 'data'
    return 'call-arg'
  }
  if (ts.isJsxAttribute(parent)) return 'jsx-attr'
  if (ts.isJsxExpression(parent)) return 'jsx-expr'
  if (ts.isPropertyAssignment(parent)) {
    const name = parent.name.getText(sf).replace(/^['"]|['"]$/g, '')
    if (DISPLAY_PROPS.has(name)) return 'prop'
    // 标签映射表: const XXX_LABEL: Record<..> = { key: '中文' }
    let holder = parent.parent
    while (holder && (ts.isObjectLiteralExpression(holder) || ts.isPropertyAssignment(holder))) holder = holder.parent
    const varName = holder && ts.isVariableDeclaration(holder) ? holder.name.getText(sf) : ''
    if (/LABEL|TEXT|TITLE|OPTION|KIND|STATUS|NAME/i.test(varName)) return 'prop'
    return 'data'
  }
  if (ts.isArrayLiteralExpression(parent)) return 'data'
  if (ts.isVariableDeclaration(parent)) return 'data'
  if (ts.isConditionalExpression(parent)) return 'cond'
  if (ts.isBinaryExpression(parent) || ts.isTemplateSpan(parent)) return 'cond'
  if (ts.isReturnStatement(parent)) return 'cond'
  if (ts.isJsxExpression(parent) || ts.isJsxText(parent)) return 'jsx'
  return 'other'
}

function walk(node) {
  if (ts.isJsxText(node)) {
    const text = node.getText(sf)
    if (CJK.test(text)) {
      const key = text.replace(/\s+/g, ' ').trim()
      if (phrases[key]) {
        const edge = jsxEdges(text)
        edits.push({ start: node.getStart(sf), end: node.getEnd(), text: edge.lead + '{tx(' + "'" + key.replace(/'/g, "\\'") + "'" + ')}' + edge.trail })
        used.add(key)
      } else {
        report.push([sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1, 'jsx-text', key])
      }
    }
  } else if (ts.isStringLiteral(node) && !ts.isJsxAttribute(node.parent)) {
    if (CJK.test(node.text)) {
      const kind = classify(node)
      const key = node.text.trim()
      if (kind === 'skip') {
        // 已包裹
      } else if (kind === 'jsx-expr') {
        // JSX 表达式内部的字符串：看外层用途，默认包裹
        if (phrases[key]) {
          edits.push({ start: node.getStart(sf), end: node.getEnd(), text: txText(node, key) })
          used.add(key)
        } else report.push([sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1, 'jsx-expr', key])
      } else if (kind === 'module') {
        report.push([sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1, 'module', key])
      } else if (kind === 'data') {
        report.push([sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1, 'data', key])
      } else if (kind === 'cond') {
        // 比较值 / 模板片段 / return 值: 是数据不是文案, 包 tx() 会破坏逻辑 (如 boardFilter === '主板')
        report.push([sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1, 'cond', key])
      } else if (kind === 'call-arg') {
        report.push([sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1, 'call-arg', key])
      } else if (phrases[key]) {
        edits.push({ start: node.getStart(sf), end: node.getEnd(), text: txText(node, key) })
        used.add(key)
      } else {
        report.push([sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1, 'no-translation', key])
      }
    }
  }
  ts.forEachChild(node, walk)
}
walk(sf)

// JSX 属性值是 StringLiteral 但被上面跳过, 这里单独处理
function walkAttrs(node) {
  if (ts.isJsxAttribute(node) && node.initializer && ts.isStringLiteral(node.initializer)) {
    const lit = node.initializer
    if (CJK.test(lit.text)) {
      const key = lit.text.trim()
      if (phrases[key]) {
        edits.push({ start: lit.getStart(sf), end: lit.getEnd(), text: '{tx(' + q(key) + ')}' })
        used.add(key)
      } else {
        report.push([sf.getLineAndCharacterOfPosition(lit.getStart(sf)).line + 1, 'jsx-attr', key])
      }
    }
  }
  ts.forEachChild(node, walkAttrs)
}
walkAttrs(sf)

edits.sort((a, b) => b.start - a.start)
let out = raw
for (const e of edits) {
  if (e.start < 0 || e.end > out.length) continue
  out = out.slice(0, e.start) + e.text + out.slice(e.end)
}
const importRe = /import \{([^}]*)\} from '@\/lib\/i18n'/
const im = out.match(importRe)
if (im) {
  if (!/\btx\b/.test(im[1])) out = out.replace(importRe, 'import { tx, ' + im[1].trim() + " } from '@/lib/i18n'")
} else {
  const ls = out.split(/\r?\n/)
  let idx = ls.findIndex(l => /^import .*from '@\/lib\//.test(l))
  if (idx < 0) idx = 0; else idx += 1
  ls.splice(idx, 0, "import { tx } from '@/lib/i18n'")
  out = ls.join('\r\n')
}
fs.writeFileSync(file, out, 'utf8')

const p = path.resolve(SRC, 'locales', 'phrases.ts')
let dict = fs.readFileSync(p, 'utf8')
for (const [name, field] of [['PHRASES_EN', 'en'], ['PHRASES_JA', 'ja']]) {
  const marker = 'export const ' + name + ': Record<string, string> = {'
  const start = dict.indexOf(marker)
  const end = dict.indexOf(NL + '}', start)
  const body = dict.slice(start, end)
  const add = []
  for (const key of used) {
    if (body.includes(q(key) + ':')) continue
    const v = phrases[key]?.[field]
    if (v === undefined || v === null) continue
    add.push('  ' + q(key) + ': ' + q(v) + ',')
  }
  if (add.length) dict = dict.slice(0, end) + NL + add.join(NL) + dict.slice(end)
  console.log(name + ': +' + add.length)
}
fs.writeFileSync(p, dict, 'utf8')
console.log('FILE=' + path.basename(file) + ' WRAPPED=' + edits.length + ' PHRASES=' + used.size + ' REPORT=' + report.length)
for (const r of report) console.log('  L' + r[0] + ' [' + r[1] + '] ' + r[2].slice(0, 60))
