<template>
  <div class="auth-page">
    <a-card class="auth-card" :bordered="false">
      <div class="auth-heading">
        <div class="auth-icon">✈️</div>
        <h1>智能旅行助手</h1>
        <p>{{ mode === 'login' ? '登录后规划并管理你的行程' : '创建账号，保存每一次旅行规划' }}</p>
      </div>

      <a-segmented v-model:value="mode" :options="modeOptions" block class="mode-switch" />
      <a-form :model="form" layout="vertical" @finish="submit">
        <a-form-item
          label="用户名"
          name="username"
          :rules="[
            { required: true, message: '请输入用户名' },
            { pattern: /^[a-zA-Z0-9_]{3,32}$/, message: '请输入 3–32 位字母、数字或下划线' }
          ]"
        >
          <a-input v-model:value="form.username" size="large" autocomplete="username" />
        </a-form-item>
        <a-form-item
          label="密码"
          name="password"
          :rules="[
            { required: true, message: '请输入密码' },
            { min: 8, max: 128, message: '密码长度应为 8–128 位' }
          ]"
        >
          <a-input-password
            v-model:value="form.password"
            size="large"
            :autocomplete="mode === 'login' ? 'current-password' : 'new-password'"
          />
        </a-form-item>
        <a-button type="primary" html-type="submit" size="large" block :loading="submitting">
          {{ mode === 'login' ? '登录' : '注册并登录' }}
        </a-button>
      </a-form>
      <p class="auth-tip">用户名不区分大小写，密码仅用于本地账号认证。</p>
    </a-card>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { apiErrorMessage } from '@/services/api'
import { login, register } from '@/services/auth'

const route = useRoute()
const router = useRouter()
const mode = ref<'login' | 'register'>('login')
const submitting = ref(false)
const modeOptions = [
  { label: '登录', value: 'login' },
  { label: '注册', value: 'register' }
]
const form = reactive({ username: '', password: '' })

function safeRedirect(): string {
  const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
  return redirect.startsWith('/') && !redirect.startsWith('//') ? redirect : '/'
}

async function submit(): Promise<void> {
  submitting.value = true
  try {
    if (mode.value === 'register') {
      await register(form.username, form.password)
    }
    await login(form.username, form.password)
    message.success(mode.value === 'register' ? '注册成功，已自动登录' : '登录成功')
    await router.replace(safeRedirect())
  } catch (error) {
    message.error(apiErrorMessage(error, mode.value === 'login' ? '登录失败' : '注册失败'))
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.auth-page {
  min-height: calc(100vh - 136px);
  display: grid;
  place-items: center;
  padding: 40px 16px;
  background:
    radial-gradient(circle at 20% 20%, rgba(255, 255, 255, 0.2), transparent 30%),
    linear-gradient(135deg, #667eea, #764ba2);
}

.auth-card {
  width: min(430px, 100%);
  border-radius: 20px;
  box-shadow: 0 24px 70px rgba(32, 28, 80, 0.35);
}

.auth-heading { text-align: center; margin-bottom: 24px; }
.auth-heading h1 { margin: 8px 0; font-size: 28px; }
.auth-heading p, .auth-tip { color: #667085; }
.auth-icon { font-size: 46px; }
.mode-switch { margin-bottom: 24px; }
.auth-tip { margin: 18px 0 0; text-align: center; font-size: 12px; }
</style>
