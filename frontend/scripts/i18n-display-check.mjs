import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'
const HERE = path.dirname(fileURLToPath(import.meta.url))
const SRC = path.resolve(HERE, '..', 'src')
const CJK = /[\u4e00-\u9fff]/
function listFiles(root) { const out = []; const walk = d => { for (const e of fs.readdirSync(d, { withFileTypes: true })) { const p = path.join(d, e.name); if (e.isDirectory()) { if (e.name === 'locales' || e.name === 'node_modules') continue; walk(p) } else if (/\.(ts|tsx)$/.test(e.name) && !/\.test\.(ts|tsx)$/.test(e.name)) out.push(p) } }; walk(root); return out }
function insideTranslate(node) { let cur = node; while (cur) { const p = cur.parent; if (!p) return false; if (ts.isCallExpression(p) && ts.isIdentifier(p.expression) && /^(t|tx)$/.test(p.expression.text)) return true; cur = p } return false }
const out = []
for (const file of listFiles(SRC)) {
  const raw = fs.readFileSync(file, 'utf8')
  const sf = ts.createSourceFile(file, raw, ts.ScriptTarget.Latest, true, file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS)
  const maps = new Map()
  for (const st of sf.statements) { if (!ts.isVariableStatement(st)) continue; for (const d of st.declarationList.declarations) { if (!ts.isIdentifier(d.name) || !d.initializer) continue; let h = false; const pr = n => { if (ts.isStringLiteral(n) && CJK.test(n.text)) h = true; ts.forEachChild(n, pr) }; pr(d.initializer); if (h) maps.set(d.name.text, d.initializer) } }
  if (!maps.size) continue
  const rel = path.relative(SRC, file).split(path.sep).join('/')
  const visit = n => {
    if (ts.isIdentifier(n) && maps.has(n.text) && n !== maps.get(n.text) && !insideTranslate(n)) {
      const isJsxName = ts.isJsxOpeningElement(n.parent) || ts.isJsxSelfClosingElement(n.parent) || ts.isJsxClosingElement(n.parent) || (ts.isJsxAttribute(n.parent) && n.parent.name === n)
      if (!isJsxName) {
        let expr = n
        while (expr.parent && (ts.isPropertyAccessExpression(expr.parent) || ts.isElementAccessExpression(expr.parent)) && expr.parent.expression === expr) expr = expr.parent
        // 找到包含该表达式的回调/JSX 块, 检查块内是否已有 tx()
        let block = expr
        while (block.parent && !ts.isBlock(block.parent) && !ts.isJsxExpression(block.parent) && !ts.isJsxAttribute(block.parent) && !ts.isArrowFunction(block.parent) && !ts.isFunctionExpression(block.parent)) block = block.parent
        const bodyText = block.getText(sf)
        const hasTx = /\btx\(|\bt\(/.test(bodyText)
        const pos = sf.getLineAndCharacterOfPosition(expr.getStart(sf))
        if (!hasTx) out.push(rel + ':' + (pos.line + 1) + '  ' + expr.getText(sf).replace(/\s+/g, ' ').slice(0, 50))
      }
    }
    ts.forEachChild(n, visit)
  }
  visit(sf)
}
console.log('NEEDS_TX', out.length)
for (const o of out) console.log('  ' + o)