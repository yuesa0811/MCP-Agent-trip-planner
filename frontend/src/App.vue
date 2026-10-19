<template>
  <div id="app">
    <a-layout style="min-height: 100vh">
      <a-layout-header class="app-header">
        <router-link to="/" class="brand">🌍 MCP多智能体旅行规划系统</router-link>
        <a-space v-if="currentUser">
          <router-link to="/" class="nav-link">新建规划</router-link>
          <router-link to="/history" class="nav-link">历史行程</router-link>
          <router-link v-if="currentUser.is_admin" to="/admin/tasks" class="nav-link">管理后台</router-link>
          <span class="username">{{ currentUser.username }}</span>
          <a-button ghost size="small" @click="signOut">退出</a-button>
        </a-space>
      </a-layout-header>
      <a-layout-content style="padding: 24px">
        <router-view />
      </a-layout-content>
      <a-layout-footer style="text-align: center">
        MCP多智能体旅行规划系统 · LangChain + FastAPI + Vue 3
      </a-layout-footer>
    </a-layout>
  </div>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { currentUser, logout, restoreCurrentUser } from '@/services/auth'

const router = useRouter()

function signOut(): void {
  logout()
  void router.replace('/login')
}

onMounted(() => {
  void restoreCurrentUser().catch(() => undefined)
})
</script>

<style>
#app {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial,
    'Noto Sans', sans-serif;
}

.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  padding: 0 50px;
  background: #001529;
}

.brand {
  color: white;
  font-size: 22px;
  font-weight: 700;
}

.nav-link, .username { color: rgba(255, 255, 255, 0.86); }
.nav-link:hover { color: white; }

@media (max-width: 760px) {
  .app-header { height: auto; min-height: 64px; padding: 10px 16px; flex-wrap: wrap; }
  .brand { font-size: 17px; }
  .username { display: none; }
}
</style>
