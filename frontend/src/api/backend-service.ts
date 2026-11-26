import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

/** 报告登记回执：已确认 / 待受理 / 生成失败 / 真实无检查 四种结果。 */
export type IntakeReceipt = {
  ok: boolean
  state: 'confirmed' | 'pending' | 'failed' | 'none'
  report_id?: number
  报告批次?: string
  来源?: string
  status?: string
  回执状态?: string
  检查总数?: string
  命中数?: string
  message?: string
}

/** 清洗页与规则页共用的报告回执回查：按来源与执行轮查询登记状态。 */
export async function fetchIntakeReceipt(
  source: string,
  roundKey: string,
): Promise<IntakeReceipt> {
  const response = await request(`/api/quality_result/intake/${source}/${roundKey}`)
  if (!response.ok) throw new Error('报告回执回查失败')
  return await response.json()
}
