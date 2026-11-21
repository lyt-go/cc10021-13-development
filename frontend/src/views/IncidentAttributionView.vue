<template>
  <section class="page" data-module="dq_incident_attribution">
    <header class="page-head">
      <div>
        <h2>质量事件归属</h2>
        <p class="page-desc">
          按「表.字段」身份把事件归属到业务域责任组，共享表的同一字段会分别派发给各自责任组。
        </p>
      </div>
      <div class="page-actions">
        <label class="filter-item">
          <span>责任组</span>
          <select v-model="activeGroup" data-testid="group-select" @change="reload">
            <option v-for="group in groups" :key="group" :value="group">{{ group }}</option>
          </select>
        </label>
        <button class="btn" data-testid="backfill" type="button" @click="runBackfill">
          回填归属记录
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">计入事件数</span>
        <strong class="stat-value" data-testid="event-count">{{ report?.计入事件数 ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">可认领处置项</span>
        <strong class="stat-value">{{ dispatch?.可认领处置项.length ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已认领处置项</span>
        <strong class="stat-value">{{ dispatch?.已认领处置项.length ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待复核积压</span>
        <strong class="stat-value" data-testid="review-count">{{ dispatch?.待复核积压 ?? 0 }}</strong>
      </article>
    </div>

    <h3 class="section-title">本组可见字段</h3>
    <table class="data-table">
      <thead>
        <tr><th>字段</th><th>业务域</th><th>责任组</th><th>写回权限</th></tr>
      </thead>
      <tbody>
        <tr v-for="field in dispatch?.可见字段 ?? []" :key="field.字段键" :data-testid="`field-${field.字段键}`">
          <td>{{ field.字段键 }}</td>
          <td>{{ field.业务域 }}</td>
          <td>{{ field.责任组 }}</td>
          <td>{{ field.可写回 ? '可写回' : '只读' }}</td>
        </tr>
        <tr v-if="!dispatch?.可见字段.length">
          <td colspan="4" class="empty-state">该责任组暂无可见字段</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">可认领处置项</h3>
    <table class="data-table">
      <thead>
        <tr><th>归属编号</th><th>事件编号</th><th>表.字段</th><th>业务域</th><th>处置项</th><th>影响行数</th><th>操作</th></tr>
      </thead>
      <tbody>
        <tr v-for="item in dispatch?.可认领处置项 ?? []" :key="item.归属编号" :data-testid="`claimable-${item.归属编号}`">
          <td>{{ item.归属编号 }}</td>
          <td>{{ item.事件编号 }}</td>
          <td>{{ item.表名 }}.{{ item.字段名 }}</td>
          <td>{{ item.业务域 }}</td>
          <td>{{ item.处置项 }}</td>
          <td>{{ item.影响行数 }}</td>
          <td class="row-actions">
            <button class="link" type="button" :data-testid="`claim-${item.归属编号}`" @click="claim(item)">
              认领
            </button>
          </td>
        </tr>
        <tr v-if="!dispatch?.可认领处置项.length">
          <td colspan="7" class="empty-state">没有可认领的处置项</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">已认领处置项</h3>
    <table class="data-table">
      <thead>
        <tr><th>归属编号</th><th>事件编号</th><th>表.字段</th><th>业务域</th><th>处置项</th><th>写回</th></tr>
      </thead>
      <tbody>
        <tr v-for="item in dispatch?.已认领处置项 ?? []" :key="item.归属编号" :data-testid="`claimed-${item.归属编号}`">
          <td>{{ item.归属编号 }}</td>
          <td>{{ item.事件编号 }}</td>
          <td>{{ item.表名 }}.{{ item.字段名 }}</td>
          <td>{{ item.业务域 }}</td>
          <td>{{ item.处置项 }}</td>
          <td class="row-actions">
            <input v-model="conclusions[item.归属编号]" :data-testid="`conclusion-${item.归属编号}`" placeholder="处置结论" />
            <button class="link" type="button" :data-testid="`write-${item.归属编号}`" @click="writeBack(item)">
              写回
            </button>
          </td>
        </tr>
        <tr v-if="!dispatch?.已认领处置项.length">
          <td colspan="6" class="empty-state">本组还没有认领处置项</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">待复核队列（缺字段映射的存量异常）</h3>
    <table class="data-table">
      <thead>
        <tr><th>归属编号</th><th>事件编号</th><th>关联规则</th><th>复核原因</th><th>影响行数</th></tr>
      </thead>
      <tbody>
        <tr v-for="item in reviewQueue" :key="item.归属编号" :data-testid="`review-${item.归属编号}`">
          <td>{{ item.归属编号 }}</td>
          <td>{{ item.事件编号 }}</td>
          <td>{{ item.关联规则 }}</td>
          <td>{{ item.复核原因 }}</td>
          <td>{{ item.影响行数 }}</td>
        </tr>
        <tr v-if="!reviewQueue.length">
          <td colspan="5" class="empty-state">暂无待复核记录</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>日报事件总数 {{ report?.日报事件总数 ?? 0 }}，按事件编号去重，同一共享事件不重复计入。</span>
      <span v-if="message" class="message-text">{{ message }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  backfillAttribution,
  claimAttribution,
  loadDispatch,
  loadGroupReport,
  loadReviewQueue,
  writeBackAttribution,
} from '@/api/backend-service'
import type { AttributionItem, DispatchResult, GroupReport } from '@/api/backend-service'

const groups = ['客户保障组', '营销策略组', '保单运营组', '风控组']
const activeGroup = ref(groups[0])
const dispatch = ref<DispatchResult | null>(null)
const report = ref<GroupReport | null>(null)
const reviewQueue = ref<AttributionItem[]>([])
const conclusions = ref<Record<string, string>>({})
const message = ref('')
const errorMessage = ref('')

async function reload() {
  errorMessage.value = ''
  try {
    const [dispatchPayload, reportPayload, reviewPayload] = await Promise.all([
      loadDispatch(activeGroup.value),
      loadGroupReport(activeGroup.value),
      loadReviewQueue(),
    ])
    dispatch.value = dispatchPayload
    report.value = reportPayload
    reviewQueue.value = reviewPayload.items
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '归属视图读取失败'
  }
}

async function runBackfill() {
  errorMessage.value = ''
  message.value = ''
  try {
    const result = await backfillAttribution()
    message.value = `回填完成，新增 ${result["新增归属记录"] ?? 0} 条归属记录，待复核 ${
      result["待复核记录数"] ?? 0
    } 条`
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回填失败'
  }
}

async function claim(item: AttributionItem) {
  errorMessage.value = ''
  message.value = ''
  try {
    const result = await claimAttribution(item.归属编号, activeGroup.value)
    if (!result.ok) throw new Error(String(result.原因 ?? '认领失败'))
    message.value = `已认领 ${item.事件编号}，主记录 ${result.主记录编号}`
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '认领失败'
  }
}

async function writeBack(item: AttributionItem) {
  errorMessage.value = ''
  message.value = ''
  try {
    const conclusion = conclusions.value[item.归属编号] || '已处置'
    const result = await writeBackAttribution(item.归属编号, activeGroup.value, conclusion)
    if (!result.ok) throw new Error(String(result.原因 ?? '写回失败'))
    message.value = `已写回 ${item.归属编号}`
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '写回失败'
  }
}

onMounted(reload)
</script>
