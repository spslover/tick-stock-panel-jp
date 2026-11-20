import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// 扫描日文译文里残留的中文词。
// 现有护栏只能拦 U+FFFD / 谚文 / 夹带英文碎片, 拦不住「日语译文里直接写中文词」
// (如把「行情」「数据」原样留在日文里)。这里用一份中文专用词表把这类残留列出来。
// 用法: node scripts/i18n-ja-scan.mjs
const HERE = path.dirname(fileURLToPath(import.meta.url))
const PHRASES = path.resolve(HERE, '..', 'src', 'locales', 'phrases.ts')

// 这些词在日语里基本不使用(标准日语说法不同), 出现在日文译文中即为残留
const ZH_ONLY = [
  '行情', '板块', '回测', '数据', '涨幅', '跌幅', '盘中', '盘后', '涨停', '跌停',
  '炸板', '翘板', '连板', '五档', '档口', '挂单', '委托', '持仓', '仓位', '加仓',
  '减仓', '满仓', '空仓', '浮盈', '浮亏', '复权', '除权', '打板', '龙头', '题材',
  '涨停板', '跌停板', '个股', '大盘', '尾盘', '开盘', '收盘', '量价', '涨跌',
]

const lines = fs.readFileSync(PHRASES, 'utf8').split('\n')
let block = ''
const hits = []
for (let i = 0; i < lines.length; i++) {
  if (lines[i].includes('export const PHRASES_JA')) block = 'JA'
  if (block !== 'JA') continue
  const m = lines[i].match(/^\s*'((?:[^'\\]|\\.)*)'\s*:\s*'((?:[^'\\]|\\.)*)'/)
  if (!m) continue
  const found = ZH_ONLY.filter(w => m[2].includes(w))
  if (found.length) hits.push({ line: i + 1, key: m[1], words: found })
}
console.log('日文译文残留中文词: ' + hits.length + ' 条')
for (const h of hits) console.log('  L' + h.line + ' [' + h.words.join(',') + '] ' + h.key.slice(0, 48))