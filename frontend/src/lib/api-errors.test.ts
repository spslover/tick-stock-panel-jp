// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { localizeApiError, matchBackendError } from './api-errors'

describe('后端错误文案本地化', () => {
  it('识别分析菜单不存在的两种后端写法', () => {
    expect(matchBackendError("分析菜单 'concept' 不存在")).toEqual({
      template: "分析菜单 '{id}' 不存在",
      params: { id: 'concept' },
    })
    expect(matchBackendError("分析菜单 'concept' 不存在或为默认菜单")).toEqual({
      template: "分析菜单 '{id}' 不存在或为默认菜单",
      params: { id: 'concept' },
    })
  })

  it('未登记的后端文案原样返回，不丢信息', () => {
    expect(matchBackendError('boom')).toBeNull()
    expect(localizeApiError('boom')).toBe('boom')
  })

  it('英文模式下把后端中文错误转成英文', () => {
    localStorage.setItem('tf-locale', 'en')
    expect(localizeApiError("分析菜单 'concept' 不存在")).toBe("Analysis menu 'concept' not found")
    localStorage.setItem('tf-locale', 'zh-CN')
    expect(localizeApiError("分析菜单 'concept' 不存在")).toBe("分析菜单 'concept' 不存在")
  })
})
