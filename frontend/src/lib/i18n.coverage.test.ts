// @vitest-environment node
import fs from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import { zhCN } from '@/locales/zh-CN'
import { en } from '@/locales/en'
import { ja } from '@/locales/ja'
import { PHRASES_EN, PHRASES_JA } from '@/locales/phrases'

/**
 * 回归护栏（已登记文案）：**凡是已经登记进词库的**键/短语，都必须同时有英文与日文译文。
 *
 * 范围说明（不要夸大本文件的保证）：
 * - 本文件校验「源码里每个 t()/tx() 调用」都有英文与日文译文（同一行多处 tx() 会逐个检查）；
 *   缺词时 tx()/t() 会静默回退中文，界面出现“半中文”，这里会判定失败。
 * - 但**未登记的硬编码中文**（压根没走 t()/tx() 的字面量）对本文件完全不可见。
 *   那类问题由 i18n.hardcoded.test.ts 扫描渲染位置来拦，两条护栏缺一不可。
 */

function flattenKeys(obj: Record<string, unknown>, prefix = ''): string[] {
  const keys: string[] = []
  for (const [k, v] of Object.entries(obj)) {
    const full = prefix ? `${prefix}.${k}` : k
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      keys.push(...flattenKeys(v as Record<string, unknown>, full))
    } else {
      keys.push(full)
    }
  }
  return keys.sort()
}

function collectSourceFiles(root: string): string[] {
  const out: string[] = []
  const walk = (dir: string) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name)
      if (entry.isDirectory()) walk(full)
      else if (/\.(ts|tsx)$/.test(entry.name) && !/\.test\.(ts|tsx)$/.test(entry.name)) out.push(full)
    }
  }
  walk(root)
  return out
}

describe('i18n 翻译覆盖率', () => {
  const srcRoot = path.resolve(process.cwd(), 'src')
  const zhKeys = flattenKeys(zhCN as Record<string, unknown>)
  const enKeys = flattenKeys(en as Record<string, unknown>)
  const jaKeys = flattenKeys(ja as Record<string, unknown>)

  it('三份结构化词库的键集合一致', () => {
    expect(enKeys).toEqual(zhKeys)
    expect(jaKeys).toEqual(zhKeys)
  })

  it('所有 t() 结构化键在英文与日文词库中都有译文', () => {
    const missingEn: string[] = []
    const missingJa: string[] = []
    for (const file of collectSourceFiles(srcRoot)) {
      const rel = path.relative(srcRoot, file)
      fs.readFileSync(file, 'utf8')
        .split(/\r?\n/)
        .forEach((line, index) => {
          const match = line.match(/\bt\(\s*'([a-z][A-Za-z0-9]*(?:\.[A-Za-z0-9_]+)+)'/)
          if (!match) return
          const key = match[1]
          if (!enKeys.includes(key)) missingEn.push(`${rel}:${index + 1} ${key}`)
          if (!jaKeys.includes(key)) missingJa.push(`${rel}:${index + 1} ${key}`)
        })
    }
    expect(missingEn).toEqual([])
    expect(missingJa).toEqual([])
  })

  it('所有 tx() 中文短语都登记了英文与日文译文', () => {
    const missingEn: string[] = []
    const missingJa: string[] = []
    for (const file of collectSourceFiles(srcRoot)) {
      const rel = path.relative(srcRoot, file)
      fs.readFileSync(file, 'utf8')
        .split(/\r?\n/)
        .forEach((line, index) => {
          // 一行里可能有多处 tx(), 必须逐个检查 —— 只查第一处会漏掉未登记的短语
          const re = /\btx\(\s*'((?:[^'\\]|\\.)*)'/g
          let match: RegExpExecArray | null
          while ((match = re.exec(line)) !== null) {
            // 源码里的 \n / \r / \t 在运行时是真实字符, 需先还原再与词库键比较
            const phrase = match[1]
              .replace(/\\'/g, "'")
              .replace(/\\n/g, '\n')
              .replace(/\\r/g, '\r')
              .replace(/\\t/g, '\t')
            if (!Object.prototype.hasOwnProperty.call(PHRASES_EN, phrase)) {
              missingEn.push(`${rel}:${index + 1} ${phrase}`)
            }
            if (!Object.prototype.hasOwnProperty.call(PHRASES_JA, phrase)) {
              missingJa.push(`${rel}:${index + 1} ${phrase}`)
            }
          }
        })
    }
    expect(missingEn).toEqual([])
    expect(missingJa).toEqual([])
  })

  it('词库译文不含替换字符 (U+FFFD) —— 乱码会静默显示给用户', () => {
    const bad: string[] = []
    for (const [name, dict] of [['PHRASES_EN', PHRASES_EN], ['PHRASES_JA', PHRASES_JA]] as const) {
      for (const [key, value] of Object.entries(dict)) {
        if (/\uFFFD/.test(key) || /\uFFFD/.test(value)) bad.push(`${name} ${key}`)
      }
    }
    expect(bad).toEqual([])
  })

  it('文档不含替换字符 (U+FFFD) —— 手写多语言文档时可能写坏字符', () => {
    const bad: string[] = []
    const repo = path.resolve(process.cwd(), '..')
    const files: string[] = []
    const rootDocs = path.join(repo, 'docs')
    if (fs.existsSync(rootDocs)) {
      const walk = (dir: string) => {
        for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
          const full = path.join(dir, entry.name)
          if (entry.isDirectory()) walk(full)
          else if (/\.md$/.test(entry.name)) files.push(full)
        }
      }
      walk(rootDocs)
    }
    for (const entry of fs.readdirSync(repo)) {
      if (/\.md$/.test(entry)) files.push(path.join(repo, entry))
    }
    for (const file of files) {
      if (fs.readFileSync(file, 'utf8').includes('\uFFFD')) bad.push(path.relative(repo, file))
    }
    expect(bad).toEqual([])
  })

  it('日文译文不含中文专用词 —— 日语里不能直接留中文词', () => {
    // 这些词在日语里基本不使用(标准说法不同): 行情/板块/回测/数据/涨停…
    // 出现在日文译文中就说明翻译时把中文原样留在了日文里, 界面会出现半中半日
    const ZH_ONLY = [
      '行情', '板块', '回测', '数据', '涨幅', '跌幅', '盘中', '盘后', '涨停', '跌停',
      '炸板', '翘板', '连板', '五档', '档口', '挂单', '委托', '持仓', '仓位', '加仓',
      '减仓', '满仓', '空仓', '浮盈', '浮亏', '复权', '除权', '打板', '龙头', '题材',
      '个股', '大盘', '尾盘', '开盘', '收盘', '量价', '涨跌',
    ]
    const bad: string[] = []
    for (const [key, value] of Object.entries(PHRASES_JA)) {
      const hit = ZH_ONLY.filter(w => value.includes(w))
      if (hit.length) bad.push(`${key} -> ${hit.join(',')}`)
    }
    expect(bad).toEqual([])
  })

  it('英文词库与日文词库的键集合一致 —— 键被写坏会导致该短语在一种语言下取不到', () => {
    const enKeys = Object.keys(PHRASES_EN).sort()
    const jaKeys = Object.keys(PHRASES_JA).sort()
    const onlyEn = enKeys.filter(k => !jaKeys.includes(k))
    const onlyJa = jaKeys.filter(k => !enKeys.includes(k))
    expect({ onlyEn, onlyJa }).toEqual({ onlyEn: [], onlyJa: [] })
  })

  // 只查译文(value): 键本身就是中文原文, 含汉字是正常的
  it('英文译文不含汉字 —— 串语言/编码损坏会静默显示给用户', () => {
    const bad: string[] = []
    for (const [key, value] of Object.entries(PHRASES_EN)) {
      if (/[\u3400-\u9FFF\uF900-\uFAFF]/.test(value)) bad.push(`${key} -> ${value}`)
    }
    expect(bad).toEqual([])
  })

  it('日文译文不含夹带进来的英文碎片 —— 批量撰写时的串词会静默显示给用户', () => {
    const bad: string[] = []
    for (const [key, value] of Object.entries(PHRASES_JA)) {
      // 只抓「汉字/假名后紧跟 -英文单词」这种串词痕迹; User-Agent / YYYY-MM-DD 之类合法写法不误伤
      if (/[\u3000-\u9FFF]-[A-Za-z]/.test(value) || /expression|placeholder|undefined|NaN/.test(value)) bad.push(`${key} -> ${value}`)
    }
    expect(bad).toEqual([])
  })

  it('日文译文不含谚文 —— 串语言会静默显示给用户', () => {
    const bad: string[] = []
    for (const [key, value] of Object.entries(PHRASES_JA)) {
      if (/[\uAC00-\uD7AF\u1100-\u11FF]/.test(value)) bad.push(`${key} -> ${value}`)
    }
    expect(bad).toEqual([])
  })
})
