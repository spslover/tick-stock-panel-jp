import { describe, expect, it } from 'vitest'
import { zhCN } from '@/locales/zh-CN'
import { en } from '@/locales/en'
import { ja } from '@/locales/ja'
import { BUILTIN_COLUMNS } from '@/lib/watchlist-columns'
import { SCREENER_BUILTIN_COLUMNS } from '@/lib/screener-columns'
import { columnGroupLabel, columnLabel, COLUMN_GROUP_KEYS, COLUMN_LABEL_KEYS } from '@/lib/column-i18n'

/**
 * 列注册表（watchlist/screener）里的中文 label 是「中文原文 → i18n 键」的来源，
 * 展示层由 columnLabel() 按 key 覆盖。这里断言每个内置列/分组在三语词库里都有条目，
 * 避免注册表新增列后漏译。
 */
function keysOf(dict: Record<string, unknown>, prefix = ''): string[] {
  const out: string[] = []
  for (const [k, v] of Object.entries(dict)) {
    const full = prefix ? `${prefix}.${k}` : k
    if (v && typeof v === 'object' && !Array.isArray(v)) out.push(...keysOf(v as Record<string, unknown>, full))
    else out.push(full)
  }
  return out
}

describe('列表列标签本地化', () => {
  const zhKeys = new Set(keysOf(zhCN as Record<string, unknown>))
  const enKeys = new Set(keysOf(en as Record<string, unknown>))
  const jaKeys = new Set(keysOf(ja as Record<string, unknown>))

  const builtinColumns = [...BUILTIN_COLUMNS, ...SCREENER_BUILTIN_COLUMNS].filter(
    c => c.source.type === 'builtin',
  )

  it('中文表头的内置列都有本地化映射', () => {
    // 纯 ASCII 表头(MA5/RSI6/EPS…)本身就是语言无关的，不需要映射
    const missing = builtinColumns
      .filter(c => /[\u4e00-\u9fff]/.test(c.label))
      .map(c => (c.source as { type: 'builtin'; key: string }).key)
      .filter(key => !COLUMN_LABEL_KEYS[key])
    expect([...new Set(missing)]).toEqual([])
  })

  it('每个列本地化键在三语词库中都有条目', () => {
    const missing: string[] = []
    for (const key of Object.values(COLUMN_LABEL_KEYS)) {
      if (!zhKeys.has(key)) missing.push('zh:' + key)
      if (!enKeys.has(key)) missing.push('en:' + key)
      if (!jaKeys.has(key)) missing.push('ja:' + key)
    }
    expect(missing).toEqual([])
  })

  it('每个列分组都有本地化映射', () => {
    const missing = Object.values(COLUMN_GROUP_KEYS).filter(
      key => !zhKeys.has(key) || !enKeys.has(key) || !jaKeys.has(key),
    )
    expect(missing).toEqual([])
  })

  // 渲染层回归：自选/策略两张核心列表的表头在英文/日文下不得残留中文原文。
  // 只断言「键存在」不够 —— 词库里若某条译文仍是中文原文，界面照样是半中文。
  it('核心列表表头在英文下不残留汉字, 日文下不得与中文原文完全相同', () => {
    const cjk = /[\u4e00-\u9fff]/
    const badEn: string[] = []
    const badJa: string[] = []
    for (const col of [...BUILTIN_COLUMNS, ...SCREENER_BUILTIN_COLUMNS]) {
      const enText = columnLabel(col, 'en')
      if (cjk.test(enText)) badEn.push(`${col.id} -> ${enText}`)
      // 日文允许使用汉字, 但整条与中文原文一致 = 漏译。
      // 例外: 中日同形的金融术语(振幅/急落 等)本就一致, 单独列出不算漏译。
      const SAME_IN_ZH_JA = new Set(['振幅'])
      if (cjk.test(col.label) && !SAME_IN_ZH_JA.has(col.label) && columnLabel(col, 'ja') === col.label) {
        badJa.push(`${col.id} -> ${col.label}`)
      }
    }
    // 列分组标题同样检查
    for (const [groupId, key] of Object.entries(COLUMN_GROUP_KEYS)) {
      const zhText = (zhCN as Record<string, unknown>)[key] as string | undefined
      const enText = columnGroupLabel({ id: groupId } as never, 'en')
      const jaText = columnGroupLabel({ id: groupId } as never, 'ja')
      if (enText && cjk.test(enText)) badEn.push(`${groupId} -> ${enText}`)
      if (zhText && cjk.test(zhText) && jaText === zhText) badJa.push(`${groupId} -> ${zhText}`)
    }
    if (badEn.length || badJa.length) console.log('BAD_EN=' + JSON.stringify(badEn) + ' BAD_JA=' + JSON.stringify(badJa))
    expect(badEn).toEqual([])
    expect(badJa).toEqual([])
  })
})
