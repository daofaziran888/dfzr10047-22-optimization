import { request } from './client'

// 回退运行回执的确认状态：待核对/已确认可采用，已采用是唯一有效产物
export type RunConfirmState = '待核对' | '已确认' | '已失败' | '已采用' | '已搁置'

export type FallbackRun = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  运行回执编号: string
  决策编号: string
  关联批次: string
  实际解析器: string
  回退序号: string
  输入凭据: string
  解析行数: string
  确认状态: RunConfirmState
  产出字段: string
  迟到标记: string
  尝试时间: string
  确认时间: string
  失败原因: string
}

export type ReconcileView = {
  决策编号: string
  关联批次: string
  推断格式: string
  配置解析器链: string[]
  决策状态: string
  已采用回执: FallbackRun | null
  已产出待核对: FallbackRun[]
  失败尝试: FallbackRun[]
  尚未执行备选: string[]
  全部运行回执: FallbackRun[]
  台账回退次数: number
  可继续尝试: boolean
}

type Envelope<T> = { ok: boolean; message: string; entry: T }

async function post<T>(path: string, body: Record<string, unknown>): Promise<Envelope<T>> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  return await response.json()
}

export async function listFallbackRuns(decisionNo?: string): Promise<{ items: FallbackRun[]; total: number }> {
  const query = decisionNo ? `?decision_no=${encodeURIComponent(decisionNo)}` : ''
  const response = await request(`/api/parser-route/runs${query}`)
  if (!response.ok) throw new Error('回退运行台账读取失败')
  return await response.json()
}

export async function reconcileDecision(decisionNo: string): Promise<Envelope<ReconcileView>> {
  const response = await request(`/api/parser-route/decisions/${encodeURIComponent(decisionNo)}/reconcile`)
  if (!response.ok) throw new Error('恢复核对失败')
  return await response.json()
}

export async function submitAttempt(
  decisionNo: string,
  payload: {
    parser: string
    input_credential: string
    success: boolean
    output_fields?: string
    row_count?: number
    attempt_time: string
    failure_reason?: string
  },
): Promise<Envelope<FallbackRun>> {
  return post(`/api/parser-route/decisions/${encodeURIComponent(decisionNo)}/attempts`, payload)
}

export async function confirmRun(
  receiptNo: string,
  confirmTime: string,
  late = false,
): Promise<Envelope<FallbackRun>> {
  return post(`/api/parser-route/runs/${encodeURIComponent(receiptNo)}/confirm`, {
    confirm_time: confirmTime,
    late,
  })
}

export async function adoptRun(
  decisionNo: string,
  receiptNo: string,
  confirmTime: string,
): Promise<Envelope<FallbackRun>> {
  return post(
    `/api/parser-route/decisions/${encodeURIComponent(decisionNo)}/adopt/${encodeURIComponent(receiptNo)}`,
    { confirm_time: confirmTime },
  )
}

export async function traceReading(
  readingNo: string,
): Promise<{ 指标读数编号: string; 运行回执: FallbackRun[]; total: number; 已采用数: number }> {
  const response = await request(`/api/parse-metric/readings/${encodeURIComponent(readingNo)}/trace`)
  if (!response.ok) throw new Error('指标追溯失败：读数不存在或该渠道暂无回退运行记录')
  return await response.json()
}
