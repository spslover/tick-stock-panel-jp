// i18n 迁移待办清单：这些文件仍含未走 t()/tx() 的中文字面量。
// 由 scripts/i18n-audit.mjs 扫描生成 —— 迁移完成后请从本清单移除，
// 护栏测试 i18n.hardcoded.test.ts 会阻止清单外的文件出现硬编码中文。
export const PENDING_I18N_FILES: string[] = [
]
