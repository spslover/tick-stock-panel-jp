import { tx } from '@/lib/i18n'

/** 按当前运行平台给出 Tesseract 安装说明（桌面端未捆绑二进制时使用）。 */
export function getOcrInstallHint(): string {
  const platform =
    (navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData
      ?.platform ||
    navigator.platform ||
    ''
  const p = platform.toLowerCase()

  if (p.includes('win')) {
    return [
      tx('OCR 引擎不可用：请安装 Tesseract 后重试。'),
      '',
      tx('Windows：'),
      tx('• 下载 UB Mannheim 发行版并安装（勾选将 tesseract 加入 PATH）'),
      tx('• 或执行：choco install tesseract'),
      '',
      tx('安装后重启应用。'),
    ].join('\n')
  }

  if (p.includes('mac')) {
    return [
      tx('OCR 引擎不可用：请安装 Tesseract 后重试。'),
      '',
      tx('macOS：'),
      tx('• 执行：brew install tesseract'),
      '',
      tx('安装后重启应用。'),
    ].join('\n')
  }

  return [
    tx('OCR 引擎不可用：请安装 Tesseract 后重试。'),
    '',
    tx('Linux：安装 tesseract-ocr（及语言包）'),
    tx('Docker：官方镜像已内置，请确认使用对应镜像。'),
    '',
    tx('安装后重启应用。'),
  ].join('\n')
}
