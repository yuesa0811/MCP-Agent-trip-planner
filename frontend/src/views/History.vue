<template>
  <div class="history-page">
    <div class="history-heading">
      <div>
        <h1>历史行程</h1>
        <p>查看规划进度、失败原因和已经生成的行程。</p>
      </div>
      <a-space>
        <a-select
          v-model:value="statusFilter"
          allow-clear
          placeholder="全部状态"
          style="width: 150px"
          @change="changeFilter"
        >
          <a-select-option value="queued">排队中</a-select-option>
          <a-select-option value="running">执行中</a-select-option>
          <a-select-option value="succeeded">已完成</a-select-option>
          <a-select-option value="degraded">降级完成</a-select-option>
          <a-select-option value="failed">失败</a-select-option>
        </a-select>
        <a-button type="primary" @click="router.push('/')">新建规划</a-button>
      </a-space>
    </div>

    <a-card :bordered="false" class="history-card">
      <a-table
        :columns="columns"
        :data-source="items"
        :loading="loading"
        :pagination="false"
        row-key="task_id"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'status'">
            <a-tag :color="statusColor(record.status)">{{ statusText(record.status) }}</a-tag>
          </template>
          <template v-else-if="column.key === 'dates'">
            {{ record.start_date }} 至 {{ record.end_date }}
          </template>
          <template v-else-if="column.key === 'duration'">
            {{ durationText(record.total_duration_ms) }}
          </template>
          <template v-else-if="column.key === 'created_at'">
            {{ formatTime(record.created_at) }}
          </template>
          <template v-else-if="column.key === 'action'">
            <a-button type="link" @click="openTask(record.task_id)">查看详情</a-button>
          </template>
        </template>
        <template #emptyText>
          <a-empty description="暂无行程记录" />
        </template>
      </a-table>
      <a-pagination
        v-if="total > pageSize"
        v-model:current="page"
        :page-size="pageSize"
        :total="total"
        show-less-items
        class="pagination"
        @change="loadHistory"
      />
    </a-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { apiErrorMessage, getTripTasks } from '@/services/api'
import type { TaskListItem, TaskStatus } from '@/types'

const router = useRouter()
const items = ref<TaskListItem[]>([])
const loading = ref(false)
const page = ref(1)
const pageSize = 20
const total = ref(0)
const statusFilter = ref<TaskStatus | undefined>()
const columns = [
  { title: '目的地', dataIndex: 'city', key: 'city' },
  { title: '日期', key: 'dates' },
  { title: '状态', key: 'status' },
  { title: '当前阶段', dataIndex: 'stage', key: 'stage' },
  { title: '总耗时', key: 'duration' },
  { title: '创建时间', key: 'created_at' },
  { title: '操作', key: 'action' }
]

const statusLabels: Record<TaskStatus, string> = {
  queued: '排队中', running: '执行中', succeeded: '已完成', degraded: '降级完成', failed: '失败'
}
const statusColors: Record<TaskStatus, string> = {
  queued: 'default', running: 'processing', succeeded: 'success', degraded: 'warning', failed: 'error'
}

function statusText(status: TaskStatus): string { return statusLabels[status] }
function statusColor(status: TaskStatus): string { return statusColors[status] }
function durationText(value?: number | null): string {
  return value == null ? '—' : `${(value / 1000).toFixed(2)} 秒`
}
function formatTime(value: string): string { return new Date(value).toLocaleString('zh-CN') }
function openTask(taskId: string): void { void router.push({ name: 'Result', params: { taskId } }) }

async function loadHistory(): Promise<void> {
  loading.value = true
  try {
    const data = await getTripTasks({ page: page.value, pageSize, status: statusFilter.value })
    items.value = data.items
    total.value = data.total
  } catch (error) {
    message.error(apiErrorMessage(error, '历史行程加载失败'))
  } finally {
    loading.value = false
  }
}

function changeFilter(): void {
  page.value = 1
  void loadHistory()
}

onMounted(loadHistory)
</script>

<style scoped>
.history-page { max-width: 1200px; margin: 0 auto; padding: 24px 0; }
.history-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; margin-bottom: 24px; }
.history-heading h1 { margin: 0 0 6px; }
.history-heading p { margin: 0; color: #667085; }
.history-card { border-radius: 16px; box-shadow: 0 10px 30px rgba(31, 35, 48, 0.08); }
.pagination { margin-top: 24px; text-align: right; }
@media (max-width: 720px) { .history-heading { align-items: flex-start; flex-direction: column; } }
</style>
