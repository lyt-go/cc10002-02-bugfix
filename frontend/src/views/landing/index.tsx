import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/landing'
const columns = [
  "批次编号",
  "来源系统",
  "落地路径",
  "文件格式",
  "接收时间",
  "原始行数",
  "摘要算法",
  "摘要值",
  "文件大小",
  "批次状态",
  "可用性判断"
]
const statuses = [
  "草稿",
  "落地中",
  "已落地",
  "已归档"
]

type Row = Record<string, string | number | null> & {
  id: number
  status: string
  snapshot_id?: string | null
  可用性判断?: { available: boolean; snapshot_id: string | null; reason: string }
  abnormal_reason?: string[]
}

type FormState = Record<string, string>

const registrationFields: Array<{ key: string; label: string; required?: boolean; placeholder?: string }> = [
  { key: '批次编号', label: '批次编号', required: true, placeholder: '如 LAND-20261113-001' },
  { key: '来源系统', label: '来源系统', required: true, placeholder: '如 订单中心' },
  { key: '落地路径', label: '落地路径', required: true, placeholder: 's3://lake/raw/...' },
  { key: '文件格式', label: '文件格式', placeholder: 'parquet' },
  { key: '接收时间', label: '接收时刻', placeholder: '2026-11-13T08:00:00+00:00' },
  { key: '原始行数', label: '原始行数', placeholder: '300000' },
  { key: '摘要算法', label: '摘要算法', placeholder: 'sha256' },
  { key: '摘要值', label: '摘要值（登记时刻可空，确认时补全）' },
  { key: '文件大小', label: '文件大小（字节）' },
]

const confirmationFields: Array<{ key: string; label: string; placeholder?: string }> = [
  { key: 'digest_type', label: '摘要算法', placeholder: 'sha256' },
  { key: 'digest_value', label: '摘要值' },
  { key: 'byte_size', label: '文件大小（字节，0 即空文件）' },
  { key: 'row_count', label: '实测行数' },
  { key: 'file_format', label: '文件格式', placeholder: 'parquet' },
  { key: 'received_at', label: '接收时刻' },
  { key: 'snapshot_id', label: '恢复用 snapshot_id（失败续传时填写，可空）' },
  { key: 'source_manifest', label: 'source_manifest（JSON，多文件写 files 数组）', placeholder: '{"files":[{"name":"a.parquet"}],"row_count":300000}' },
]

function toSubmitValues(form: FormState): Record<string, unknown> {
  const values: Record<string, unknown> = {}
  for (const [key, raw] of Object.entries(form)) {
    if (raw === '') continue
    if (key === '原始行数' || key === '文件大小' || key === 'byte_size' || key === 'row_count') {
      values[key] = Number(raw)
    } else if (key === 'source_manifest') {
      try {
        values[key] = JSON.parse(raw)
      } catch {
        values[key] = raw
      }
    } else {
      values[key] = raw
    }
  }
  return values
}

function renderCell(row: Row, column: string): string {
  if (column === '可用性判断') {
    const judge = row['可用性判断']
    if (!judge) return '—'
    return judge.available ? `可用（${judge.snapshot_id ?? ''}）` : `不可用：${judge.reason}`
  }
  const value = row[column]
  return value === null || value === undefined || value === '' ? '—' : String(value)
}

export default function LandingView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [registerOpen, setRegisterOpen] = useState(false)
  const [registerForm, setRegisterForm] = useState<FormState>({})
  const [confirmTarget, setConfirmTarget] = useState<Row | null>(null)
  const [confirmForm, setConfirmForm] = useState<FormState>({})
  const filterFields = useMemo(() => ['批次编号', '来源系统', '落地路径'], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('落地批次列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '原始落地列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row, extra: Record<string, unknown> = {}) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action, ...extra } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || '原始落地动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '原始落地操作失败')
    }
  }

  async function submitRegistration() {
    setErrorMessage('')
    try {
      const response = await request(ENDPOINT, {
        method: 'POST',
        body: JSON.stringify({ values: toSubmitValues(registerForm) }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        throw new Error(payload.message || '登记失败')
      }
      setRegisterOpen(false)
      setRegisterForm({})
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '落地批次登记失败')
    }
  }

  async function submitConfirmation() {
    if (!confirmTarget) return
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${confirmTarget.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({
          values: { action: '确认已落地', ...toSubmitValues(confirmForm) },
        }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        const codes = (payload.codes ?? []).join('、')
        throw new Error(codes ? `${payload.message}（${codes}）` : payload.message)
      }
      setConfirmTarget(null)
      setConfirmForm({})
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '校验确认失败')
    }
  }

  function openConfirmation(row: Row) {
    setConfirmTarget(row)
    setConfirmForm({
      digest_type: String(row['摘要算法'] ?? 'sha256'),
      row_count: String(row['原始行数'] ?? ''),
      file_format: String(row['文件格式'] ?? ''),
      received_at: String(row['接收时间'] ?? ''),
      snapshot_id: row.snapshot_id ?? '',
    })
  }

  return (
    <section className="page" data-module="landing">
      <header className="page-head">
        <div>
          <h2>原始落地管理</h2>
          <p className="page-desc">登记完整业务字段，校验快照通过后才能确认落地与归档；贴源只读取校验通过批次。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setRegisterOpen(true)}>
            登记落地批次
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出原始落地清单
          </button>
        </div>
      </header>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{renderCell(row, column)}</td>)}
              <td className="row-actions">
                <button
                  className="link" type="button"
                  disabled={row.status !== '草稿'}
                  onClick={() => void runAction('发起落地', row)}
                >
                  发起落地
                </button>
                <button
                  className="link" type="button"
                  disabled={row.status !== '落地中'}
                  onClick={() => openConfirmation(row)}
                >
                  确认已落地（提交校验快照）
                </button>
                <button
                  className="link" type="button"
                  disabled={row.status !== '已落地'}
                  onClick={() => void runAction('归档批次', row)}
                >
                  归档批次
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无原始落地数据，可先登记落地批次</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条原始落地记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>

      {registerOpen && (
        <div className="modal-mask" role="dialog" aria-modal="true">
          <div className="modal">
            <h3>登记落地批次（原始登记会完整持久化）</h3>
            <div className="modal-form">
              {registrationFields.map((field) => (
                <label key={field.key} className="modal-field">
                  <span>{field.label}{field.required ? ' *' : ''}</span>
                  <input
                    value={registerForm[field.key] ?? ''}
                    placeholder={field.placeholder}
                    onChange={(event) => setRegisterForm((prev) => ({ ...prev, [field.key]: event.target.value }))}
                  />
                </label>
              ))}
            </div>
            <div className="modal-actions">
              <button className="btn ghost" type="button" onClick={() => setRegisterOpen(false)}>取消</button>
              <button className="btn primary" type="button" onClick={() => void submitRegistration()}>保存登记</button>
            </div>
          </div>
        </div>
      )}

      {confirmTarget && (
        <div className="modal-mask" role="dialog" aria-modal="true">
          <div className="modal">
            <h3>确认已落地：提交完整校验快照（{confirmTarget['批次编号']}）</h3>
            <p className="page-desc">
              空文件、压缩包多文件、缺摘要、行数不符、同摘要不同大小（分片冲突）都会被拒绝；
              失败后可凭 snapshot_id 在此恢复续传。
            </p>
            <div className="modal-form">
              {confirmationFields.map((field) => (
                <label key={field.key} className="modal-field">
                  <span>{field.label}</span>
                  <input
                    value={confirmForm[field.key] ?? ''}
                    placeholder={field.placeholder}
                    onChange={(event) => setConfirmForm((prev) => ({ ...prev, [field.key]: event.target.value }))}
                  />
                </label>
              ))}
            </div>
            <div className="modal-actions">
              <button className="btn ghost" type="button" onClick={() => setConfirmTarget(null)}>取消</button>
              <button className="btn primary" type="button" onClick={() => void submitConfirmation()}>提交校验</button>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
