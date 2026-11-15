/** 转换器/解析模板前端共用的领域常量与展示辅助。 */

export type Row = Record<string, string | number | null | boolean | string[] | Record<string, unknown>>

export const FORMATTER_STATUSES = ['草稿', '转换测试中', '已启用', '已退役'] as const
export const PARSER_STATUSES = ['草稿', '模板验证中', '已生效', '待复核', '已废弃'] as const

/** 每个状态只暴露唯一合法的下推动作，按钮按状态渲染，从根上杜绝“点了没反应”。 */
export const FORMATTER_NEXT_ACTION: Record<string, string> = {
  草稿: '提交转换测试',
  转换测试中: '启用转换器',
  已启用: '退役转换器',
}

export const PARSER_NEXT_ACTION: Record<string, string> = {
  草稿: '提交模板验证',
  模板验证中: '生效模板',
  已生效: '废弃模板',
  待复核: '迁移转换器引用',
}

export const STATUS_BADGE_CLASS: Record<string, string> = {
  已退役: 'badge badge-retired',
  已废弃: 'badge badge-retired',
  待复核: 'badge badge-review',
  已启用: 'badge badge-active',
  已生效: 'badge badge-active',
  转换测试中: 'badge badge-testing',
  模板验证中: 'badge badge-testing',
  草稿: 'badge badge-draft',
}

export function statusBadgeClass(status: string): string {
  return STATUS_BADGE_CLASS[status] ?? 'badge badge-draft'
}

export function formatCell(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

/** 待复核模板在迁移表单里需要的默认值，保证两个模板看到同一份退役快照结论。 */
export type EnabledVersion = {
  versionCode: string
  转换器编号: string
  版本号: string
  status: string
  outputFormat: string
  outputFields: string[]
  inputFormat: string
}

export type MappingRow = { source: string; target: string }

export function parseMappingsText(text: string): MappingRow[] {
  /** 每行一条 source->target；空行忽略，格式错误抛出由调用方提示。 */
  const rows: MappingRow[] = []
  const seen = new Set<string>()
  text.split('\n').map((line) => line.trim()).filter(Boolean).forEach((line, index) => {
    const [rawSource, rawTarget] = line.split('->').map((part) => part?.trim() ?? '')
    if (!rawSource || !rawTarget) {
      throw new Error(`第 ${index + 1} 行「${line}」不是 source->target 形式`)
    }
    if (seen.has(rawTarget)) {
      throw new Error(`目标字段「${rawTarget}」重复，重复映射会互相覆盖`)
    }
    seen.add(rawTarget)
    rows.push({ source: rawSource, target: rawTarget })
  })
  if (rows.length === 0) throw new Error('字段映射不能为空')
  return rows
}

export function mappingsToText(mappings: unknown): string {
  if (!Array.isArray(mappings)) return ''
  return mappings.map((row) => {
    const item = row as MappingRow
    return `${item.source}->${item.target}`
  }).join('\n')
}
