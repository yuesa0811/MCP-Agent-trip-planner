<template>
  <a-card title="工具调用过程" :bordered="false" class="tool-card">
    <a-table :columns="columns" :data-source="calls" :pagination="false" row-key="id" size="small">
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'agent'">{{ agentText(record.agent_name) }}</template>
        <template v-else-if="column.key === 'status'">
          <a-tag :color="statusColor(record.status)">{{ statusText(record.status) }}</a-tag>
        </template>
        <template v-else-if="column.key === 'duration'">{{ durationText(record.duration_ms) }}</template>
        <template v-else-if="column.key === 'details'">
          <span v-if="record.error" class="error-text">
            {{ record.error.code }}：{{ record.error.message }}
          </span>
          <details v-else-if="record.result_summary || Object.keys(record.arguments).length">
            <summary>查看摘要</summary>
            <pre>{{ summaryText(record) }}</pre>
          </details>
          <span v-else>—</span>
        </template>
      </template>
      <template #emptyText>尚未发生真实 MCP 工具调用</template>
    </a-table>
  </a-card>
</template>

<script setup lang="ts">
import type { ToolCall, ToolCallStatus } from '@/types'

defineProps<{ calls: ToolCall[] }>()

const columns = [
  { title: 'Agent', key: 'agent', width: 120 },
  { title: '工具', dataIndex: 'tool_name', key: 'tool', width: 190 },
  { title: '状态', key: 'status', width: 110 },
  { title: '耗时', key: 'duration', width: 100 },
  { title: '调用信息', key: 'details' }
]
const agentLabels: Record<string, string> = {
  attraction: '景点', weather: '天气', hotel: '酒店', planner: '行程'
}
const statusLabels: Record<ToolCallStatus, string> = {
  running: '执行中', succeeded: '成功', empty: '空结果', failed: '失败'
}
const statusColors: Record<ToolCallStatus, string> = {
  running: 'processing', succeeded: 'success', empty: 'warning', failed: 'error'
}

function agentText(value: string): string { return agentLabels[value] || value }
function statusText(value: ToolCallStatus): string { return statusLabels[value] }
function statusColor(value: ToolCallStatus): string { return statusColors[value] }
function durationText(value?: number | null): string { return value == null ? '—' : `${value.toFixed(1)} ms` }
function summaryText(call: ToolCall): string {
  return JSON.stringify({ arguments: call.arguments, result: call.result_summary || undefined }, null, 2)
}
</script>

<style scoped>
.tool-card { margin-bottom: 20px; border-radius: 14px; }
.error-text { color: #cf1322; }
details { cursor: pointer; }
pre { max-height: 190px; overflow: auto; margin: 8px 0 0; padding: 10px; border-radius: 8px; background: #f6f8fa; white-space: pre-wrap; word-break: break-word; font-size: 12px; }
</style>
