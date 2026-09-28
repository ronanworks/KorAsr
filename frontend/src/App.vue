<template>
  <main v-if="!auth.ready" class="loading-screen" role="status">正在连接课堂服务…</main>
  <main v-else-if="!auth.authenticated" class="auth-page">
    <form class="auth-card" @submit.prevent="login">
      <div class="brand-mark" aria-hidden="true">语</div>
      <p class="eyebrow">KORASR · CLASSROOM STUDIO</p>
      <h1>进入课堂</h1>
      <p class="auth-description">实时转写、翻译、录音和课堂笔记集中在一个工作区。</p>
      <p v-if="!auth.configured" class="form-error" role="alert">管理员账号尚未配置，请先在服务器上创建管理员。</p>
      <template v-else>
        <label for="username">用户名</label>
        <input id="username" v-model.trim="username" type="text" autocomplete="username" required autofocus />
        <label for="password">密码</label>
        <input id="password" v-model="password" type="password" autocomplete="current-password" required />
        <button class="button button-primary" type="submit" :disabled="busy || !username || !password">{{ busy ? '登录中…' : '登录' }}</button>
      </template>
      <p v-if="error" class="form-error" role="alert">{{ error }}</p>
    </form>
  </main>
  <div v-else class="app-shell">
    <header class="app-header">
      <router-link to="/" class="brand" aria-label="KorASR 课堂">
        <span class="brand-mark small" aria-hidden="true">语</span><span>KorASR</span>
      </router-link>
      <nav class="primary-nav" aria-label="主导航">
        <router-link to="/">实时课堂</router-link>
        <span v-if="session.isRecording" class="nav-disabled" aria-disabled="true" title="结束录音后可查看课堂记录">课堂记录</span>
        <router-link v-else to="/history">课堂记录</router-link>
        <template v-if="auth.user?.is_admin">
          <span v-if="session.isRecording" class="nav-disabled" aria-disabled="true" title="结束录音后可管理账号">账号管理</span>
          <router-link v-else to="/admin/users">账号管理</router-link>
        </template>
      </nav>
      <div class="account-actions">
        <span class="account-name" :title="auth.user?.username">{{ auth.user?.username }}</span>
        <button class="button button-quiet" type="button" :disabled="session.isRecording || session.isFinalizing" :title="session.isRecording || session.isFinalizing ? '请先保存录音' : '退出账号'" @click="logout">退出</button>
      </div>
    </header>
    <router-view class="route-view" />
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import axios from 'axios'
import { useAuthStore } from './stores/auth.js'
import { useSessionStore } from './stores/session.js'

const auth = useAuthStore()
const session = useSessionStore()
const router = useRouter()
const username = ref('')
const password = ref('')
const busy = ref(false)
const error = ref('')
let interceptorId

onMounted(async () => {
  interceptorId = axios.interceptors.response.use(
    response => response,
    failure => {
      if (failure.response?.status === 401 && !failure.config?.url?.startsWith('/api/auth/')) {
        session.resetSession()
        auth.expire()
        error.value = '登录已过期，请重新登录。'
      }
      return Promise.reject(failure)
    },
  )
  try { await auth.refresh() }
  catch (_) { error.value = '无法连接课堂服务，请稍后重试。' }
})
onUnmounted(() => axios.interceptors.response.eject(interceptorId))

async function login() {
  if (busy.value || !username.value || !password.value) return
  busy.value = true
  error.value = ''
  try {
    session.resetSession()
    await auth.login(username.value, password.value)
    password.value = ''
    router.push('/')
  } catch (e) {
    error.value = e.response?.status === 401 ? '用户名或密码错误。' : '登录失败，请检查服务状态。'
  } finally { busy.value = false }
}

async function logout() {
  if (session.isRecording || session.isFinalizing) return
  try { await auth.logout() }
  catch (_) {
    alert('退出失败，请检查网络连接后重试。')
    return
  }
  session.resetSession()
  router.push('/')
}
</script>
