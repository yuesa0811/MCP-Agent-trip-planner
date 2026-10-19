<template>
  <div class="admin-page">
    <div class="admin-heading">
      <div>
        <h1>任务管理</h1>
        <p>查看所有用户的规划任务及真实工具执行过程。</p>
      </div>
      <a-button @click="loadTasks">刷新列表</a-button>
    </div>

    <a-card :bordered="false" class="panel-card filter-card">
      <a-space wrap>
        <a-input
          v-model:value="usernameFilter"
          allow-clear
          placeholder="按用户名筛选"
          style="width: 210px"
          @press-enter="applyFilters"
        />
        <a-select v-model:value="statusFilter" allow-clear placeholder="全部状态" style="width: 150px">
          <a-select-option value="queued">排队中</a-select-option>
          <a-select-option value="running">执行中</a-select-option>
          <a-select-option value="succeeded">已完成</a-select-option>
          <a-select-option value="degraded">降级完成</a-select-option>
          <a-select-option value="failed">失败</a-select-option>
        </a-select>
        <a-button type="primary" @click="applyFilters">查询</a-button>
        <a-button @click="clearFilters">重置</a-button>
      </a-space>
    </a-card>

    <a-card :bordered="false" class="panel-card">
      <a-table
        :columns="columns"
        :data-source="items"
        :loading="listLoading"
        :pagination="false"
        :row-class-name="rowClassName"
        row-key="task_id"
        @row="rowEvents"
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
            <a-button type="link" @click.stop="selectTask(record.task_id)">查看过程</a-button>
          </template>
        </template>
        <template #emptyText><a-empty description="没有符合条件的任务" /></template>
      </a-table>
      <a-pagination
        v-if="total > pageSize"
        v-model:current="page"
        :page-size="pageSize"
        :total="total"
        show-less-items
        class="pagination"
        @change="loadTasks"
      />
    </a-card>

    <a-spin :spinning="detailLoading">
      <a-card v-if="detail" title="任务详情" :bordered="false" class="panel-card detail-card">
        <a-descriptions bordered :column="3" size="small">
          <a-descriptions-item label="所属用户">{{ detail.username }}</a-descriptions-item>
          <a-descriptions-item label="目的地">{{ detail.request.city }}</a-descriptions-item>
          <a-descriptions-item label="状态">
            <a-tag :color="statusColor(detail.status)">{{ statusText(detail.status) }}</a-tag>
          </a-descriptions-item>
          <a-descriptions-item label="出行日期">
            {{ detail.request.start_date }} 至 {{ detail.request.end_date }}
          </a-descriptions-item>
          <a-descriptions-item label="当前阶段">{{ detail.stage }}</a-descriptions-item>
          <a-descriptions-item label="任务总耗时">{{ durationText(detail.total_duration_ms) }}</a-descriptions-item>
        </a-descriptions>
        <a-alert
          v-if="detail.error"
          type="error"
          show-icon
          :message="detail.error.message"
          :description="detail.error.code"
          class="detail-alert"
        />
        <a-alert
          v-for="(warning, index) in detail.warnings"
          :key="`${index}-${warning}`"
          type="warning"
          show-icon
          :message="warning"
          class="detail-alert"
        />
        <ToolCallTable :calls="detail.tool_calls" class="tool-table" />
      </a-card>
    </a-spin>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import ToolCallTable from '@/components/ToolCallTable.vue'
import { apiErrorMessage, getAdminTask, getAdminTasks } from '@/services/api'
import type { AdminTaskDetail, AdminTaskListItem, TaskStatus } from '@/types'

const items = ref<AdminTaskListItem[]>([])
const detail = ref<AdminTaskDetail | null>(null)
const selectedTaskId = ref('')
const listLoading = ref(false)
const detailLoading = ref(false)
const page = ref(1)
const pageSize = 20
const total = ref(0)
const statusFilter = ref<TaskStatus | undefined>()
const usernameFilter = ref('')
let pollTimer: number | undefined

const columns = [
  { title: '用户', dataIndex: 'username', key: 'username' },
  { title: '目的地', dataIndex: 'city', key: 'city' },
  { title: '日期', key: 'dates' },
  { title: '状态', key: 'status' },
  { title: '阶段', dataIndex: 'stage', key: 'stage' },
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

function stopPolling(): void {
  if (pollTimer) window.clearTimeout(pollTimer)
  pollTimer = undefined
}

async function loadTasks(): Promise<void> {
  listLoading.value = true
  try {
    const data = await getAdminTasks({
      page: page.value,
      pageSize,
      status: statusFilter.value,
      username: usernameFilter.value.trim()
    })
    items.value = data.items
    total.value = data.total
  } catch (error) {
    message.error(apiErrorMessage(error, '全局任务列表加载失败'))
  } finally {
    listLoading.value = false
  }
}

async function selectTask(taskId: string, showLoading = true): Promise<void> {
  stopPolling()
  selectedTaskId.value = taskId
  if (showLoading) detailLoading.value = true
  try {
    const data = await getAdminTask(taskId)
    if (selectedTaskId.value !== taskId) return
    detail.value = data
    const item = items.value.find(task => task.task_id === taskId)
    if (item) {
      item.status = data.status
      item.stage = data.stage
      item.total_duration_ms = data.total_duration_ms
      item.updated_at = data.updated_at
    }
    if (['queued', 'running'].includes(data.status)) {
      pollTimer = window.setTimeout(() => { void selectTask(taskId, false) }, 1000)
    }
  } catch (error) {
    message.error(apiErrorMessage(error, '任务详情加载失败'))
  } finally {
    detailLoading.value = false
  }
}

function applyFilters(): void {
  page.value = 1
  void loadTasks()
}

function clearFilters(): void {
  usernameFilter.value = ''
  statusFilter.value = undefined
  applyFilters()
}

function rowEvents(record: AdminTaskListItem): { onClick: () => void } {
  return { onClick: () => { void selectTask(record.task_id) } }
}

function rowClassName(record: AdminTaskListItem): string {
  return record.task_id === selectedTaskId.value ? 'selected-row' : 'clickable-row'
}

onMounted(loadTasks)
onBeforeUnmount(stopPolling)
</script>

<style scoped>
.admin-page { max-width: 1400px; margin: 0 auto; padding: 24px 0; }
.admin-heading { display: flex; justify-content: space-between; align-items: flex-end; gap: 20px; margin-bottom: 22px; }
.admin-heading h1 { margin: 0 0 6px; }
.admin-heading p { margin: 0; color: #667085; }
.panel-card { margin-bottom: 20px; border-radius: 16px; box-shadow: 0 8px 28px rgba(31, 35, 48, 0.07); }
.filter-card { margin-bottom: 16px; }
.pagination { margin-top: 22px; text-align: right; }
.detail-card { margin-top: 20px; }
.detail-alert { margin-top: 14px; }
.tool-table { margin-top: 18px; }
:deep(.clickable-row), :deep(.selected-row) { cursor: pointer; }
:deep(.selected-row) { background: #f0f5ff; }
@media (max-width: 720px) { .admin-heading { align-items: flex-start; flex-direction: column; } }
</style>
