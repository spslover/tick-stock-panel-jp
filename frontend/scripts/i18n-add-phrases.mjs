// 把 { "中文": { "en": "...", "ja": "..." } } 形式的短语补进 locales/phrases.ts。
// 用于 AST 迁移器无法自动包裹的位置（数据常量、toast 文案等）的人工处理。
// 用法: node scripts/i18n-add-phrases.mjs <phrases.json>
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const PHRASES = path.resolve(HERE, '..', 'src', 'locales', 'phrases.ts')
const NL = String.fromCharCode(10)
const phrases = JSON.parse(fs.readFileSync(path.resolve(process.cwd(), process.argv[2]), 'utf8'))
const q = s => "'" + String(s).replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, "\\n").replace(/\r/g, "\\r").replace(/\t/g, "\\t") + "'"

let dict = fs.readFileSync(PHRASES, 'utf8')
for (const [name, field] of [['PHRASES_EN', 'en'], ['PHRASES_JA', 'ja']]) {
  const marker = 'export const ' + name + ': Record<string, string> = {'
  const start = dict.indexOf(marker)
  const end = dict.indexOf(NL + '}', start)
  const body = dict.slice(start, end)
  const add = []
  for (const [key, v] of Object.entries(phrases)) {
    if (body.includes(q(key) + ':')) continue
    const val = v?.[field]
    if (val === undefined || val === null) continue
    add.push('  ' + q(key) + ': ' + q(val) + ',')
  }
  if (add.length) dict = dict.slice(0, end) + NL + add.join(NL) + dict.slice(end)
  console.log(name + ': +' + add.length)
}
fs.writeFileSync(PHRASES, dict, 'utf8')
