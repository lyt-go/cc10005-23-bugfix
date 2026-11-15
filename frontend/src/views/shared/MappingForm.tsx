import { useMemo, useState } from 'react'

import { mappingsToText, parseMappingsText, type EnabledVersion, type MappingRow } from './domain'

type MappingFormProps = {
  mode: 'activate' | 'migrate'
  versions: EnabledVersion[]
  defaultFormatterCode?: string
  defaultVersionNo?: string
  defaultTargetOutput?: string
  defaultMappings?: MappingRow[]
  submitting: boolean
  onSubmit: (payload: {
    action: string
    formatterCode: string
    versionNo: string
    targetOutputFormat: string
    mappings: MappingRow[]
  }) => void
  onCancel: () => void
}

/**
 * 生效/迁移共用表单：只能选择「已启用」版本，提交前本地先做映射解析，
 * 真正的兼容性裁决仍以后端 409 为准并原样展示原因。
 */
export default function MappingForm({
  mode,
  versions,
  defaultFormatterCode,
  defaultVersionNo,
  defaultTargetOutput,
  defaultMappings,
  submitting,
  onSubmit,
  onCancel,
}: MappingFormProps) {
  const action = mode === 'activate' ? '生效模板' : '迁移转换器引用'
  const enabled = useMemo(() => versions.filter((item) => item.status === '已启用'), [versions])
  const [formatterCode, setFormatterCode] = useState(defaultFormatterCode ?? enabled[0]?.转换器编号 ?? '')
  const [versionNo, setVersionNo] = useState(defaultVersionNo ?? enabled[0]?.版本号 ?? 'v1')
  const [targetOutput, setTargetOutput] = useState(defaultTargetOutput ?? enabled[0]?.outputFormat ?? '')
  const [mappingText, setMappingText] = useState(mappingsToText(defaultMappings))
  const [localError, setLocalError] = useState('')

  function pickVersion(code: string, version: string) {
    const matched = enabled.find((item) => item.转换器编号 === code && item.版本号 === version)
    setFormatterCode(code)
    setVersionNo(version)
    if (matched && !targetOutput) setTargetOutput(matched.outputFormat)
  }

  function handleSubmit() {
    setLocalError('')
    try {
      const mappings = parseMappingsText(mappingText)
      if (!formatterCode) {
        setLocalError('请选择要引用的已启用转换器版本')
        return
      }
      if (!targetOutput.trim()) {
        setLocalError('请填写模板目标输出格式')
        return
      }
      onSubmit({ action, formatterCode, versionNo, targetOutputFormat: targetOutput.trim(), mappings })
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : '映射解析失败')
    }
  }

  return (
    <section className="mapping-form">
      <h4>{mode === 'activate' ? '生效模板：绑定已启用转换器版本' : '迁移引用：改绑新的已启用版本'}</h4>
      {enabled.length === 0 && (
        <p className="error-text">当前没有「已启用」的转换器版本，请先在格式转换页启用一个兼容版本。</p>
      )}
      <label className="form-item">
        <span>引用转换器版本（仅已启用可选）</span>
        <select
          value={`${formatterCode}@${versionNo}`}
          onChange={(event) => {
            const [code, version] = event.target.value.split('@')
            pickVersion(code, version)
          }}
        >
          {enabled.map((item) => (
            <option key={item.versionCode} value={item.versionCode}>
              {item.versionCode}（输出 {item.outputFormat}）
            </option>
          ))}
        </select>
      </label>
      <label className="form-item">
        <span>模板目标输出格式</span>
        <input value={targetOutput} onChange={(event) => setTargetOutput(event.target.value)} placeholder="parquet://article?fields=a,b" />
      </label>
      <label className="form-item">
        <span>字段映射（每行一条 source{'-'}&gt;target）</span>
        <textarea rows={5} value={mappingText} onChange={(event) => setMappingText(event.target.value)} placeholder="title->title" />
      </label>
      {localError && <p className="error-text">{localError}</p>}
      <div className="form-actions">
        <button className="btn primary" type="button" disabled={submitting || enabled.length === 0} onClick={handleSubmit}>
          {submitting ? '提交中…' : action}
        </button>
        <button className="btn ghost" type="button" disabled={submitting} onClick={onCancel}>取消</button>
      </div>
    </section>
  )
}
