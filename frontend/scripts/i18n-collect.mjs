// 收集单个文件里「唯一」的中文短语及其 AST 位置分类，供人工撰写译文。
// 只读，不修改任何文件。用法: node scripts/i18n-collect.mjs <file>
import fs from 'node:fs'
import path from 'node:path'
import ts from 'typescript'

const CJK = /[\u4e00-\u9fff]/
const file = path.resolve(process.cwd(), process.argv[2])
const raw = fs.readFileSync(file, 'utf8')
const sf = ts.createSourceFile(file, raw, ts.ScriptTarget.Latest, true, file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS)

const DISPLAY_PROPS = new Set([
  'label', 'title', 'text', 'desc', 'description', 'hint', 'placeholder',
  'ariaLabel', 'subtitle', 'note', 'tip', 'tooltip', 'emptyText', 'unit',
])
const DATA_CALLS = /^(api\.|set[A-Z]|console\.|toast$|reportError$)/
const out = new Map()

function add(line, kind, key) {
  if (!key) return
  if (!out.has(key)) out.set(key, { kind, line, n: 0 })
  out.get(key).n++
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
  return 'other'
}

function walk(node) {
  if (ts.isJsxText(node)) {
    const text = node.getText(sf)
    if (CJK.test(text)) {
      const key = text.replace(/\s+/g, ' ').trim()
      add(sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1, 'jsx-text', key)
    }
  } else if (ts.isStringLiteral(node) && !ts.isJsxAttribute(node.parent)) {
    if (CJK.test(node.text)) {
      const kind = classify(node)
      if (kind !== 'skip') add(sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1, kind, node.text.trim())
    }
  }
  ts.forEachChild(node, walk)
}
walk(sf)

function walkAttrs(node) {
  if (ts.isJsxAttribute(node) && node.initializer && ts.isStringLiteral(node.initializer)) {
    const lit = node.initializer
    if (CJK.test(lit.text)) add(sf.getLineAndCharacterOfPosition(lit.getStart(sf)).line + 1, 'jsx-attr', lit.text.trim())
  }
  ts.forEachChild(node, walkAttrs)
}
walkAttrs(sf)

const arr = [...out.entries()]
console.log('UNIQUE=' + arr.length)
for (const [k, v] of arr) console.log(JSON.stringify({ line: v.line, kind: v.kind, n: v.n, text: k }))
