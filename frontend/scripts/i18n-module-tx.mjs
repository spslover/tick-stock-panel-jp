// 扫描「模块级 tx()/t() 调用」—— 模块初始化时求值会把语言冻结在导入那一刻，
// 切换语言后这些文案不会更新。用法: node scripts/i18n-module-tx.mjs [--write]
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const SRC = path.resolve(HERE, '..', 'src')

function listFiles(root) {
  const out = []
  const walk = dir => {
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, e.name)
      if (e.isDirectory()) {
        if (e.name === 'locales' || e.name === 'node_modules') continue
        walk(p)
      } else if (/\.(ts|tsx)$/.test(e.name) && !/\.test\.(ts|tsx)$/.test(e.name)) {
        out.push(p)
      }
    }
  }
  walk(root)
  return out
}

export function scanModuleLevelTx(root = SRC) {
  const result = new Map()
  for (const file of listFiles(root)) {
    const rel = path.relative(root, file).split(path.sep).join('/')
    const raw = fs.readFileSync(file, 'utf8')
    const sf = ts.createSourceFile(file, raw, ts.ScriptTarget.Latest, true, file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS)
    const hits = []
    const visit = node => {
      if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && /^(t|tx)$/.test(node.expression.text)) {
        let p = node.parent
        let inFn = false
        while (p && !ts.isSourceFile(p)) {
          if (ts.isFunctionLike(p)) { inFn = true; break }
          p = p.parent
        }
        if (!inFn) {
          hits.push({
            line: sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1,
            text: node.getText(sf).slice(0, 100),
          })
        }
      }
      ts.forEachChild(node, visit)
    }
    visit(sf)
    if (hits.length) result.set(rel, hits)
  }
  return result
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const scanned = scanModuleLevelTx()
  const total = [...scanned.values()].reduce((n, a) => n + a.length, 0)
  console.log('模块级 tx()/t() 调用: ' + total + ' 处 / ' + scanned.size + ' 个文件')
  for (const [f, hits] of scanned) {
    console.log('  ' + hits.length + '\t' + f)
    for (const h of hits) console.log('      L' + h.line + ' ' + h.text)
  }
}
