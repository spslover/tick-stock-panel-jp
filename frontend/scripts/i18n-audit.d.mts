export interface I18nViolation {
  line: number
  kind: 'literal' | 'jsx'
  text: string
}

/** 扫描 src 下未走 t()/tx() 的中文字面量，返回 相对路径 -> 违规条目 */
export function scanUntranslatedCjk(root?: string): Map<string, I18nViolation[]>
