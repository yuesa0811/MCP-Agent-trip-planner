import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import Antd from 'ant-design-vue'
import 'ant-design-vue/dist/reset.css'
import App from './App.vue'
import Home from './views/Home.vue'
import Result from './views/Result.vue'
import History from './views/History.vue'
import Login from './views/Login.vue'
import AdminTasks from './views/AdminTasks.vue'
import { currentUser, isAuthenticated, logout } from './services/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      name: 'Home',
      component: Home,
      meta: { requiresAuth: true }
    },
    {
      path: '/result',
      redirect: '/history'
    },
    {
      path: '/result/:taskId',
      name: 'Result',
      component: Result,
      meta: { requiresAuth: true }
    },
    {
      path: '/history',
      name: 'History',
      component: History,
      meta: { requiresAuth: true }
    },
    {
      path: '/login',
      name: 'Login',
      component: Login
    },
    {
      path: '/admin/tasks',
      name: 'AdminTasks',
      component: AdminTasks,
      meta: { requiresAuth: true, requiresAdmin: true }
    },
    {
      path: '/:pathMatch(.*)*',
      redirect: '/'
    }
  ]
})

router.beforeEach((to) => {
  if (to.meta.requiresAuth && !isAuthenticated()) {
    return { name: 'Login', query: { redirect: to.fullPath } }
  }
  if (to.meta.requiresAdmin && !currentUser.value?.is_admin) return { name: 'Home' }
  if (to.name === 'Login' && isAuthenticated()) return { name: 'Home' }
})

window.addEventListener('auth:unauthorized', () => {
  const current = router.currentRoute.value
  logout()
  if (current.name !== 'Login') {
    void router.replace({ name: 'Login', query: { redirect: current.fullPath } })
  }
})

const app = createApp(App)

app.use(router)
app.use(Antd)

app.mount('#app')
