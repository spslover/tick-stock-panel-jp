// @vitest-environment node
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import { scanUntranslatedCjk } from '../../scripts/i18n-audit.mjs'
import { PENDING_I18N_FILES } from './i18n.pending'

/**
 * 硬编码中文护栏。
 *
 * i18n.coverage.test.ts 只能保证「已登记的键/短语都有英日译文」，
 * 但看不见绕过词库的硬编码中文 —— 那正是界面出现半中文的真正原因。
 * 本护栏直接扫描渲染位置：任何没有走 t()/tx() 的中文字面量都算违规。
 *
 * 规则：
 * - 待迁移清单（i18n.pending.ts）之外的文件出现硬编码中文 → 失败；
 * - 待迁移清单里的文件已被清零 → 失败（说明可以摘掉这条待办）。
 */
describe('i18n 硬编码中文护栏', () => {
  const scanned = scanUntranslatedCjk(path.resolve(process.cwd(), 'src'))
  const pending = new Set(PENDING_I18N_FILES)

  it('待迁移清单之外的文件不得残留未本地化中文', () => {
    const violations: string[] = []
    for (const [file, hits] of scanned) {
      if (pending.has(file)) continue
      for (const hit of hits.slice(0, 5)) {
        violations.push(`${file}:${hit.line} [${hit.kind}] ${hit.text}`)
      }
    }
    expect(violations).toEqual([])
  })

  it('待迁移清单不得保留已清零的文件', () => {
    const stale = PENDING_I18N_FILES.filter(file => !scanned.has(file))
    expect(stale).toEqual([])
  })

  it('待迁移清单覆盖全部仍有硬编码中文的文件', () => {
    const missing = [...scanned.keys()].filter(file => !pending.has(file))
    expect(missing).toEqual([])
  })
})
