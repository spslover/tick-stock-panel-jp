// 给使用了 tx() 的顶层组件补上 useI18n() 响应式取词，
// 保证切换语言时组件重新渲染、界面文案随之更新。
// 用法: node scripts/i18n-reactive.mjs <file>
import fs from 'node:fs'
import path from 'node:path'
import ts from 'typescript'

const file = path.resolve(process.cwd(), process.argv[2])
let src = fs.readFileSync(file, 'utf8')
// 保持源文件行尾风格（CRLF / LF），不要把混合行尾写回磁盘
const EOL = src.includes('\r\n') ? '\r\n' : '\n'

const sf = ts.createSourceFile(file, src, ts.ScriptTarget.Latest, true, file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS)

/** React 组件判定：大写开头的函数名 / memo()、forwardRef() 包裹的函数。 */
function isComponentLike(node) {
  let fn = null
  if (ts.isFunctionDeclaration(node) || ts.isFunctionExpression(node) || ts.isArrowFunction(node)) fn = node
  else if (ts.isCallExpression(node) && /^(memo|forwardRef)$/.test(node.expression.getText(sf))) {
    const a = node.arguments[0]
    if (a && (ts.isFunctionExpression(a) || ts.isArrowFunction(a))) fn = a
  }
  if (!fn) return false
  // 只有「有名字的函数」或「memo/forwardRef 包裹」才当作组件;
  // const XXX = () => {} 这种大写命名的辅助函数不是组件, 不能插 Hook
  if (fn.name && ts.isIdentifier(fn.name) && /^[A-Z]/.test(fn.name.text)) return true
  const p = fn.parent
  if (p && ts.isCallExpression(p) && /^(memo|forwardRef)$/.test(p.expression.getText(sf))) {
    const gp = p.parent
    if (gp && ts.isVariableDeclaration(gp) && ts.isIdentifier(gp.name) && /^[A-Z]/.test(gp.name.text)) return true
  }
  return false
}

/** 该函数体内是否已经声明了 tx（const { tx } = useI18n() / const tx = ...）。 */
function declaresTx(fn) {
  if (!fn.body || !ts.isBlock(fn.body)) return false
  let found = false
  for (const st of fn.body.statements) {
    if (!ts.isVariableStatement(st)) continue
    for (const d of st.declarationList.declarations) {
      if (ts.isObjectBindingPattern(d.name)) {
        for (const el of d.name.elements) {
          const nm = el.propertyName ?? el.name
          if (ts.isIdentifier(nm) && nm.text === 'tx') found = true
        }
      } else if (ts.isIdentifier(d.name) && d.name.text === 'tx') {
        found = true
      }
    }
  }
  return found
}

// 最外层函数（模块级函数 / 组件函数 / 组件内嵌的普通函数都归到各自最外层）
const candidates = []
const visitTop = node => {
  if (ts.isFunctionLike(node) && node.body) {
    candidates.push(node)
    return
  }
  ts.forEachChild(node, visitTop)
}
visitTop(sf)

/** 函数子树里是否有 tx() 调用（含嵌套函数与箭头回调）。 */
function subtreeUsesTx(fn) {
  let used = false
  const probe = n => {
    if (ts.isCallExpression(n) && ts.isIdentifier(n.expression) && n.expression.text === 'tx') used = true
    ts.forEachChild(n, probe)
  }
  probe(fn.body)
  // 默认参数在 body 之前求值, 里的 tx() 也算「用到了 tx」
  for (const p of fn.parameters ?? []) probe(p)
  return used
}

/** 最近的祖先函数（没有则返回 null，表示模块级）。 */
function nearestFn(node) {
  let p = node.parent
  while (p && !ts.isSourceFile(p)) {
    if (ts.isFunctionLike(p)) return p
    p = p.parent
  }
  return null
}

const withTx = new Set()
for (const fn of candidates) {
  if (subtreeUsesTx(fn) && !declaresTx(fn) && isComponentLike(fn)) withTx.add(fn)
}

// 已经有 tx 声明的组件也计入「自给自足」的集合
const selfSufficient = new Set()
for (const fn of candidates) if (declaresTx(fn)) selfSufficient.add(fn)

// 模块级 tx 需求：tx() 调用的所有祖先函数都没有自己的 tx 声明,
// 且该调用不在任何「会补 Hook 的组件」内部 —— 这时必须依赖模块级 tx 导入。
let needModuleTx = false
{
  const covered = n => {
    let p = n.parent
    while (p && !ts.isSourceFile(p)) {
      if (ts.isFunctionLike(p)) {
        if (declaresTx(p) || selfSufficient.has(p) || withTx.has(p)) return true
      }
      p = p.parent
    }
    return false
  }
  const probe = n => {
    if (ts.isCallExpression(n) && ts.isIdentifier(n.expression) && n.expression.text === 'tx') {
      if (!covered(n)) needModuleTx = true
    }
    ts.forEachChild(n, probe)
  }
  probe(sf)
}

const inserts = [...withTx].map(n => ({ pos: n.body.getStart(sf) + 1, text: EOL + '  const { tx } = useI18n()' }))
inserts.sort((a, b) => b.pos - a.pos)
for (const i of inserts) src = src.slice(0, i.pos) + i.text + src.slice(i.pos)

// 修正 import：模块级确实用到 tx 才保留 tx 导入
const importRe = /import \{([^}]*)\} from '@\/lib\/i18n'/
const im = src.match(importRe)
if (im) {
  const hasUseI18n = /\buseI18n\b/.test(im[1])
  // 保留原有导入项(getLocale / t / 类型等), 只由脚本管理 tx / useI18n 两个
  // —— 直接重写成 ['tx','useI18n'] 会把 getLocale 之类的导入悄悄丢掉;
  //    而无条件保留 tx 又会在「组件用 Hook 的 tx、模块级没有 tx 调用」时留下未使用导入(TS6133)
  const parts = im[1].split(',').map(s => s.trim()).filter(Boolean)
    .filter(s => s !== 'tx' && s !== 'useI18n')
  if (needModuleTx) parts.push('tx')
  if (hasUseI18n || withTx.size > 0) parts.push('useI18n')
  if (parts.length) {
    src = src.replace(importRe, "import { " + parts.join(', ') + " } from '@/lib/i18n'")
  } else {
    src = src.replace(new RegExp(importRe.source + '\\r?\\n'), '')
  }
} else if (withTx.size > 0) {
  const lines = src.split(/\r?\n/)
  let idx = lines.findIndex(l => /^import .*from '@\/lib\//.test(l))
  if (idx < 0) idx = 0
  else idx += 1
  lines.splice(idx, 0, "import { useI18n } from '@/lib/i18n'")
  src = lines.join(EOL)
}
fs.writeFileSync(file, src, 'utf8')
console.log('reactive components=' + withTx.size + ' moduleTx=' + needModuleTx)
