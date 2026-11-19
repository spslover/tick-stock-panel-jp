import { tx } from './i18n'

/**
 * 后端错误文案本地化。
 *
 * 后端 HTTPException 的 detail 是中文句子（例如「分析菜单 'concept' 不存在」），
 * 前端直接渲染会在英文/日文模式下露出中文。这里按「固定写法 + 可变参数」匹配
 * 已知文案，命中就换成可本地化的模板；未命中的错误原样返回（不丢信息），
 * 等发现新的后端文案时再往 PATTERNS 里加一条即可。
 *
 * 后端文案保持中文不动（不引入 i18n 依赖，也不破坏既有 API 契约）。
 */
const PATTERNS: { re: RegExp; template: string }[] = [
  { re: /^分析菜单 '(.+)' 不存在或为默认菜单$/, template: "分析菜单 '{id}' 不存在或为默认菜单" },
  { re: /^分析菜单 '(.+)' 不存在$/, template: "分析菜单 '{id}' 不存在" },
]

/** 命中的后端错误 → 可本地化模板 + 参数；未命中返回 null。 */
export function matchBackendError(message: string): { template: string; params: Record<string, string> } | null {
  for (const { re, template } of PATTERNS) {
    const m = message.match(re)
    if (m) return { template, params: { id: m[1] } }
  }
  return null
}

/** 把后端错误文案转成当前语言的文案；未登记的文案原样返回。 */
export function localizeApiError(message: string): string {
  const hit = matchBackendError(message)
  if (!hit) return message
  return tx(hit.template, hit.params)
}
