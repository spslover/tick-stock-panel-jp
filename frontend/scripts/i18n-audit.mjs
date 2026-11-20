// i18n 硬编码中文审计器：扫描渲染位置里没有走 t()/tx() 的中文字面量。
// 既作为 CI 护栏的扫描内核（见 src/lib/i18n.hardcoded.test.ts），
// 也可单独运行查看/刷新待迁移清单：node scripts/i18n-audit.mjs [--write]
//
// 内核基于 TypeScript AST：字符串字面量 / 模板字面量 / JSX 文本各自精确识别；
// 正则字面量与注释天然不会被误判成文案（早期朴素引号扫描版本会）。
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const SRC = path.resolve(HERE, '..', 'src')
const CJK = /[\u4e00-\u9fff]/

export function maskComments(src) {
  let out = ''
  let i = 0
  const n = src.length
  while (i < n) {
    const c = src[i]
    const d = src[i + 1]
    if (c === '/' && d === '/') {
      while (i < n && src[i] !== '\n') { out += ' '; i++ }
      continue
    }
    if (c === '/' && d === '*') {
      out += '  '; i += 2
      while (i < n && !(src[i] === '*' && src[i + 1] === '/')) { out += src[i] === '\n' ? '\n' : ' '; i++ }
      out += '  '; i += 2
      continue
    }
    out += c; i++
  }
  return out
}

/** 去掉 tx()/t() 调用：模板里嵌了 tx('中文') 时，中文已被词库覆盖。 */
function stripTxCalls(v) {
  return v.replace(/(?<![A-Za-z0-9_$])(?:t|tx)\([^)]*\)/g, '')
}

/** 判断字面量是否处于 JSX 上下文(属性值 / JSX 表达式子节点)。 */
function inJsxContext(node) {
  let cur = node.parent
  while (cur) {
    if (ts.isJsxAttribute(cur) || ts.isJsxExpression(cur)) return true
    // 数组/对象字面量是「数据容器」: 里面的中文是数据键(渲染处再翻译), 允许按词库放行
    if (ts.isArrayLiteralExpression(cur) || ts.isObjectLiteralExpression(cur)
      || ts.isVariableDeclaration(cur)) return false
    if (ts.isFunctionDeclaration(cur) || ts.isFunctionExpression(cur) || ts.isArrowFunction(cur)
      || ts.isSourceFile(cur)) return false
    cur = cur.parent
  }
  return false
}

function isLocalizedCall(node, sf) {
  const p = node.parent
  return ts.isCallExpression(p) && /^(t|tx)$/.test(p.expression.getText(sf))
}

function loadCoveredStrings(root) {
  const covered = new Set()
  const phrases = fs.readFileSync(path.join(root, 'locales', 'phrases.ts'), 'utf8')
  const keyRe = /^\s*'((?:[^'\\]|\\.)*)'\s*:/gm
  let m
  while ((m = keyRe.exec(phrases))) covered.add(m[1].replace(/\\'/g, "'"))
  return covered
}

const LABEL_REGISTRY_FILES = new Set([
  'lib/watchlist-columns.ts',
  'lib/screener-columns.ts',
])

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

/** 返回 Map<相对路径, 违规条目[]>；相对路径以 src/ 为基准并用正斜杠。 */
export function scanUntranslatedCjk(root = SRC) {
  const covered = loadCoveredStrings(root)
  const result = new Map()
  for (const file of listFiles(root)) {
    const rel = path.relative(root, file).split(path.sep).join('/')
    if (LABEL_REGISTRY_FILES.has(rel)) continue
    const raw = fs.readFileSync(file, 'utf8')
    const sf = ts.createSourceFile(
      file, raw, ts.ScriptTarget.Latest, true,
      file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
    )
    const rawLines = raw.split(/\r?\n/)
    const hits = []
    // 豁免标记：单行字面量只看本行；跨多行的字面量（模板/代码样例）额外看上一行，
    // 这样 "// i18n-exempt" 注释可以放在语句前一行而不必塞进模板字符串内部。
    const exempt = (line, endLine = line) => {
      const from = endLine > line ? line - 1 : line
      for (let l = from; l <= endLine; l++) {
        if ((rawLines[l - 1] || '').includes('i18n-exempt')) return true
      }
      return false
    }
    const add = (line, kind, text, jsx = false, endLine = line) => {
      const t = String(text).replace(/\s+/g, ' ').trim()
      if (!t || !CJK.test(t)) return
      if (!jsx && covered.has(t)) return
      if (exempt(line, endLine)) return
      hits.push({ line, kind, text: t.slice(0, 120) })
    }
    const visit = node => {
      if (ts.isStringLiteral(node)) {
        if (!isLocalizedCall(node, sf)) {
          const start = sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1
          const end = sf.getLineAndCharacterOfPosition(node.getEnd()).line + 1
          add(start, 'literal', node.text, inJsxContext(node), end)
        }
      } else if (ts.isNoSubstitutionTemplateLiteral(node) || ts.isTemplateExpression(node)) {
        const text = node.getText(sf)
        const start = sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1
        const end = sf.getLineAndCharacterOfPosition(node.getEnd()).line + 1
        add(start, 'literal', stripTxCalls(text), false, end)
      } else if (ts.isJsxText(node)) {
        // JSX 文本永远原样渲染, 必须显式 tx() 化; 不能因词库已有该短语就放行
        const start = sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1
        const end = sf.getLineAndCharacterOfPosition(node.getEnd()).line + 1
        add(start, 'jsx', node.getText(sf), true, end)
      }
      ts.forEachChild(node, visit)
    }
    visit(sf)
    if (hits.length) result.set(rel, hits)
  }
  return result
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const scanned = scanUntranslatedCjk()
  const total = [...scanned.values()].reduce((n, arr) => n + arr.length, 0)
  const rows = [...scanned.entries()].sort((a, b) => b[1].length - a[1].length)
  console.log('未本地化中文字面量: ' + total + ' 处 / ' + rows.length + ' 个文件')
  for (const [file, hits] of rows) console.log('  ' + hits.length + '\t' + file)
  if (process.argv.includes('--write')) {
    const target = path.resolve(SRC, 'lib', 'i18n.pending.ts')
    const body = [
      '// i18n 迁移待办清单：这些文件仍含未走 t()/tx() 的中文字面量。',
      '// 由 scripts/i18n-audit.mjs 扫描生成 —— 迁移完成后请从本清单移除，',
      '// 护栏测试 i18n.hardcoded.test.ts 会阻止清单外的文件出现硬编码中文。',
      'export const PENDING_I18N_FILES: string[] = [',
      ...rows.map(([file]) => '  ' + JSON.stringify(file) + ','),
      ']',
      '',
    ].join(String.fromCharCode(10))
    fs.writeFileSync(target, body, 'utf8')
    console.log('已写入 ' + target)
  }
}
