<template>
  <section class="page fallback-page">
    <header class="page-head">
      <div>
        <h2>解析路由 · 回退运行恢复台账</h2>
        <p class="page-desc">
          恢复时先核对已执行解析器与迟到结果，再凭回退运行回执继续合法尝试；
          已产出待核对与尚未执行的备选分开操作，已采用尝试只计一次。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="reload-ledger" type="button" @click="reloadAll">刷新台账</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">运行回执总数</span>
        <strong class="stat-value">{{ runs.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已产出待核对</span>
        <strong class="stat-value">{{ countByState(['待核对', '已确认']) }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已采用（唯一有效产物）</span>
        <strong class="stat-value">{{ countByState(['已采用']) }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">失败/已搁置</span>
        <strong class="stat-value">{{ countByState(['已失败', '已搁置']) }}</strong>
      </article>
    </div>

    <p v-if="message" class="notice" :class="noticeKind" data-testid="notice">{{ message }}</p>

    <!-- 1. 路由决策列表：选择一个决策进入恢复核对 -->
    <h3 class="section-title">路由决策</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>决策编号</th>
          <th>关联批次</th>
          <th>推断格式</th>
          <th>命中解析器</th>
          <th>回退次数</th>
          <th>当前状态</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in decisions" :key="String(row.id)" :data-status="String(row.status)">
          <td>{{ row['决策编号'] }}</td>
          <td>{{ row['关联批次'] }}</td>
          <td>{{ row['推断格式'] }}</td>
          <td>{{ row['命中解析器'] || '—' }}</td>
          <td>{{ row['回退次数'] }}</td>
          <td>{{ row.status }}</td>
          <td>
            <button class="link" type="button" :data-testid="`reconcile-${row['决策编号']}`" @click="openReconcile(String(row['决策编号']))">
              恢复核对
            </button>
          </td>
        </tr>
      </tbody>
    </table>

    <!-- 2. 恢复核对面板：待核对 / 尚未执行 分开 -->
    <template v-if="view">
      <h3 class="section-title">
        恢复核对 · {{ view.决策编号 }}
        <span class="title-extra">台账回退次数 {{ view.台账回退次数 }} · {{ view.决策状态 }}</span>
      </h3>

      <div class="reconcile-grid">
        <article class="panel panel-pending">
          <h4>已产出但待核对（解析器已回传结果）</h4>
          <p v-if="!view.已产出待核对.length" class="empty-inline">暂无待核对回执</p>
          <div v-for="run in view.已产出待核对" :key="run.运行回执编号" class="run-card" :data-testid="`pending-${run.运行回执编号}`">
            <div class="run-head">
              <strong>{{ run.实际解析器 }}</strong>
              <span class="tag">{{ run.确认状态 }}</span>
              <span v-if="run.迟到标记 === '是'" class="tag tag-late">迟到</span>
            </div>
            <p class="run-line">回执 {{ run.运行回执编号 }} · {{ run.解析行数 }} 行 · 字段：{{ run.产出字段 || '—' }}</p>
            <p class="run-line muted">输入凭据：{{ run.输入凭据 }}</p>
            <div class="run-actions">
              <button class="btn small" type="button" @click="onConfirm(run.运行回执编号)">核对迟到回执</button>
              <button class="btn small primary" type="button" :data-testid="`adopt-${run.运行回执编号}`" @click="onAdopt(run.运行回执编号)">
                采用此回执
              </button>
            </div>
          </div>
        </article>

        <article class="panel panel-idle">
          <h4>尚未执行的备选（可从原决策继续）</h4>
          <p v-if="!view.尚未执行备选.length" class="empty-inline">备选均已执行或决策已采用</p>
          <div v-for="parser in view.尚未执行备选" :key="parser" class="run-card" :data-testid="`idle-${parser}`">
            <div class="run-head">
              <strong>{{ parser }}</strong>
              <span class="tag tag-idle">未执行</span>
            </div>
            <form class="attempt-form" @submit.prevent="onSubmitAttempt(parser)">
              <label>
                <span>输入凭据</span>
                <input v-model="attemptForm[parser].credential" :data-testid="`cred-${parser}`" required placeholder="该次尝试各自的输入摘要" />
              </label>
              <label>
                <span>解析行数</span>
                <input v-model.number="attemptForm[parser].rowCount" type="number" min="0" required />
              </label>
              <label>
                <span>产出字段（逗号分隔）</span>
                <input v-model="attemptForm[parser].fields" placeholder="order_id,amount" />
              </label>
              <div class="run-actions">
                <button class="btn small" type="button" @click="attemptForm[parser].success = false">标记失败尝试</button>
                <button class="btn small primary" type="button" @click="attemptForm[parser].success = true">提交成功产出</button>
              </div>
            </form>
          </div>
        </article>
      </div>

      <article v-if="view.已采用回执" class="panel panel-adopted" data-testid="adopted-panel">
        <h4>唯一有效产物（已采用）</h4>
        <p class="run-line">
          回执 <strong>{{ view.已采用回执.运行回执编号 }}</strong> · 解析器
          <strong>{{ view.已采用回执.实际解析器 }}</strong> · {{ view.已采用回执.解析行数 }} 行
        </p>
        <p class="run-line muted">采用字段：{{ view.已采用回执.产出字段 || '—' }} · 输入凭据：{{ view.已采用回执.输入凭据 }}</p>
      </article>

      <article class="panel panel-failed">
        <h4>失败尝试（保留可展开的原输入）</h4>
        <p v-if="!view.失败尝试.length" class="empty-inline">暂无失败尝试</p>
        <details v-for="run in view.失败尝试" :key="run.运行回执编号" class="fail-detail" :data-testid="`fail-${run.运行回执编号}`">
          <summary>{{ run.实际解析器 }} · 回执 {{ run.运行回执编号 }} · {{ run.尝试时间 }}</summary>
          <p class="run-line muted">输入凭据：{{ run.输入凭据 }}</p>
          <p class="run-line">失败原因：{{ run.失败原因 || '—' }}</p>
        </details>
      </article>
    </template>

    <!-- 3. 持久化回退运行台账 -->
    <h3 class="section-title">回退运行台账（持久化，指标结算唯一依据）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>运行回执</th>
          <th>决策编号</th>
          <th>实际解析器</th>
          <th>回退序号</th>
          <th>输入凭据</th>
          <th>解析行数</th>
          <th>产出字段</th>
          <th>确认状态</th>
          <th>迟到</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="run in runs" :key="String(run.id)" :data-testid="`run-${run.运行回执编号}`" :data-state="run.确认状态">
          <td>{{ run.运行回执编号 }}</td>
          <td>{{ run.决策编号 }}</td>
          <td>{{ run.实际解析器 }}</td>
          <td>{{ run.回退序号 }}</td>
          <td class="cred-cell">{{ run.输入凭据 }}</td>
          <td>{{ run.解析行数 }}</td>
          <td>{{ run.产出字段 || '—' }}</td>
          <td><span class="tag" :class="stateTagClass(run.确认状态)">{{ run.确认状态 }}</span></td>
          <td>{{ run.迟到标记 }}</td>
        </tr>
      </tbody>
    </table>

    <!-- 4. 指标复核：从采用结果回到实际执行记录 -->
    <h3 class="section-title">解析指标复核追溯</h3>
    <form class="filter-bar" @submit.prevent="onTrace">
      <label class="filter-item">
        <span>指标读数编号</span>
        <input v-model="readingNo" data-testid="trace-reading-input" placeholder="如 PM-001" required />
      </label>
      <button class="btn primary" data-testid="trace-reading" type="submit">追溯回退运行记录</button>
    </form>
    <table v-if="tracedRuns.length" class="data-table" data-testid="trace-table">
      <thead>
        <tr>
          <th>运行回执</th>
          <th>决策编号</th>
          <th>实际解析器</th>
          <th>输入凭据</th>
          <th>解析行数</th>
          <th>确认状态</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="run in tracedRuns" :key="String(run.id)">
          <td>{{ run.运行回执编号 }}</td>
          <td>{{ run.决策编号 }}</td>
          <td>{{ run.实际解析器 }}</td>
          <td class="cred-cell">{{ run.输入凭据 }}</td>
          <td>{{ run.解析行数 }}</td>
          <td><span class="tag" :class="stateTagClass(run.确认状态)">{{ run.确认状态 }}</span></td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import {
  adoptRun,
  confirmRun,
  listFallbackRuns,
  reconcileDecision,
  submitAttempt,
  traceReading,
} from '@/api/fallback-run'
import { listEntries } from '@/api/backend-service'
import type { FallbackRun, ReconcileView } from '@/api/fallback-run'
import type { EntryRow } from '@/data/types'

const decisions = ref<EntryRow[]>([])
const runs = ref<FallbackRun[]>([])
const view = ref<ReconcileView | null>(null)
const readingNo = ref('PM-001')
const tracedRuns = ref<FallbackRun[]>([])
const message = ref('')
const noticeKind = ref('info')
const nowTick = ref(0)

interface AttemptFormState {
  credential: string
  rowCount: number
  fields: string
  success: boolean
}

const attemptForm = reactive<Record<string, AttemptFormState>>({})

function ensureFormState(parser: string): AttemptFormState {
  if (!attemptForm[parser]) {
    attemptForm[parser] = { credential: '', rowCount: 0, fields: '', success: true }
  }
  return attemptForm[parser]
}

function countByState(states: string[]): number {
  return runs.value.filter((run) => states.includes(run.确认状态)).length
}

function stateTagClass(state: string): string {
  if (state === '已采用') return 'tag-adopted'
  if (state === '已失败') return 'tag-failed'
  if (state === '已搁置') return 'tag-superseded'
  return 'tag-pending'
}

function flash(text: string, kind: 'info' | 'error' = 'info') {
  message.value = text
  noticeKind.value = kind
}

async function reloadAll() {
  try {
    const [decisionPage, runPage] = await Promise.all([
      listEntries('parser_route'),
      listFallbackRuns(),
    ])
    decisions.value = decisionPage.items
    runs.value = runPage.items
    if (view.value) {
      const refreshed = await reconcileDecision(view.value.决策编号)
      view.value = refreshed.entry
      seedFormState(view.value)
    }
  } catch (error) {
    flash(error instanceof Error ? error.message : '台账刷新失败', 'error')
  }
}

async function openReconcile(decisionNo: string) {
  try {
    const result = await reconcileDecision(decisionNo)
    view.value = result.entry
    seedFormState(result.entry)
    flash(result.message)
  } catch (error) {
    flash(error instanceof Error ? error.message : '恢复核对失败', 'error')
  }
}

function seedFormState(target: ReconcileView) {
  for (const parser of target.尚未执行备选) {
    const state = ensureFormState(parser)
    if (!state.credential) {
      state.credential = `digest:${target.关联批次}:${parser}#manual-${nowTick.value}`
    }
  }
}

async function onConfirm(receiptNo: string) {
  if (!view.value) return
  try {
    const result = await confirmRun(receiptNo, new Date().toISOString(), true)
    flash(result.message)
    await reloadAll()
  } catch (error) {
    flash(error instanceof Error ? error.message : '迟到确认失败', 'error')
  }
}

async function onAdopt(receiptNo: string) {
  if (!view.value) return
  try {
    const result = await adoptRun(view.value.决策编号, receiptNo, new Date().toISOString())
    if (!result.ok) {
      flash(result.message, 'error')
      return
    }
    flash(result.message)
    await reloadAll()
  } catch (error) {
    flash(error instanceof Error ? error.message : '采用失败', 'error')
  }
}

async function onSubmitAttempt(parser: string) {
  if (!view.value) return
  const state = ensureFormState(parser)
  nowTick.value += 1
  try {
    const result = await submitAttempt(view.value.决策编号, {
      parser,
      input_credential: state.credential,
      success: state.success,
      output_fields: state.fields,
      row_count: state.rowCount,
      attempt_time: new Date().toISOString(),
      failure_reason: state.success ? '' : `${parser} 人工登记失败`,
    })
    if (!result.ok) {
      flash(result.message, 'error')
      return
    }
    flash(result.message)
    await reloadAll()
  } catch (error) {
    flash(error instanceof Error ? error.message : '尝试提交失败', 'error')
  }
}

async function onTrace() {
  try {
    const result = await traceReading(readingNo.value)
    tracedRuns.value = result.运行回执
    flash(`读数 ${readingNo.value} 追溯到 ${result.total} 条运行回执，其中已采用 ${result.已采用数} 条`)
  } catch (error) {
    tracedRuns.value = []
    flash(error instanceof Error ? error.message : '指标追溯失败', 'error')
  }
}

onMounted(reloadAll)
</script>

<style scoped>
.fallback-page .section-title {
  margin: 22px 0 10px;
  font-size: 15px;
}
.title-extra {
  margin-left: 8px;
  font-size: 12px;
  color: #64748b;
  font-weight: 400;
}
.notice {
  margin: 10px 0;
  padding: 8px 12px;
  border-radius: 6px;
  background: #eef4ff;
  border: 1px solid #c7d9ff;
  color: #1e40af;
  font-size: 13px;
}
.notice.error {
  background: #fef2f2;
  border-color: #fecaca;
  color: #b91c1c;
}
.reconcile-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}
.panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
}
.panel h4 {
  margin: 0 0 10px;
  font-size: 14px;
}
.panel-pending {
  border-left: 4px solid #d97706;
}
.panel-idle {
  border-left: 4px solid #2563eb;
}
.panel-adopted {
  margin-top: 14px;
  border-left: 4px solid #16a34a;
}
.panel-failed {
  margin-top: 14px;
  border-left: 4px solid #dc2626;
}
.run-card {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px;
  margin-bottom: 10px;
  background: #f8fafc;
}
.run-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.run-line {
  margin: 4px 0;
  font-size: 13px;
}
.muted {
  color: #64748b;
  word-break: break-all;
}
.run-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}
.btn.small {
  padding: 4px 10px;
  font-size: 12px;
}
.tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  background: #e2e8f0;
  color: #334155;
}
.tag-late {
  background: #fef3c7;
  color: #92400e;
}
.tag-idle {
  background: #dbeafe;
  color: #1d4ed8;
}
.tag-adopted {
  background: #dcfce7;
  color: #166534;
}
.tag-failed {
  background: #fee2e2;
  color: #991b1b;
}
.tag-superseded {
  background: #f1f5f9;
  color: #64748b;
}
.tag-pending {
  background: #ffedd5;
  color: #9a3412;
}
.attempt-form label {
  display: block;
  margin-bottom: 8px;
  font-size: 12px;
  color: #475569;
}
.attempt-form input {
  width: 100%;
  margin-top: 4px;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
}
.fail-detail {
  margin-bottom: 8px;
  font-size: 13px;
}
.fail-detail summary {
  cursor: pointer;
  color: #b91c1c;
}
.cred-cell {
  font-family: ui-monospace, monospace;
  font-size: 12px;
  max-width: 260px;
  word-break: break-all;
}
.empty-inline {
  color: #94a3b8;
  font-size: 13px;
}
@media (max-width: 980px) {
  .reconcile-grid {
    grid-template-columns: 1fr;
  }
}
</style>
