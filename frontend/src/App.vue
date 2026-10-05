<template>
  <main v-if="!auth.ready" class="loading-screen" role="status">{{ t('app.connecting') }}</main>
  <main v-else-if="!auth.authenticated" class="auth-page">
    <form class="auth-card" @submit.prevent="login">
      <div class="auth-top">
        <div class="brand-mark" aria-hidden="true">{{ BRAND_MARK }}</div>
        <LanguageSwitcher />
      </div>
      <p class="eyebrow">{{ t('auth.eyebrow') }}</p>
      <h1>{{ t('auth.title') }}</h1>
      <p class="auth-description">{{ t('auth.description') }}</p>
      <p v-if="!auth.configured" class="form-error" role="alert">{{ t('auth.notConfigured') }}</p>
      <template v-else>
        <label for="username">{{ t('auth.username') }}</label>
        <input id="username" v-model.trim="username" type="text" autocomplete="username" required autofocus />
        <label for="password">{{ t('auth.password') }}</label>
        <input id="password" v-model="password" type="password" autocomplete="current-password" required />
        <button class="button button-primary" type="submit" :disabled="busy || !username || !password">{{ busy ? t('auth.loggingIn') : t('auth.login') }}</button>
      </template>
      <p v-if="errorMessage" class="form-error" role="alert">{{ errorMessage }}</p>
    </form>
  </main>
  <div v-else class="app-shell">
    <header class="app-header">
      <router-link to="/" class="brand" :aria-label="t('nav.brand')">
        <span class="brand-mark small" aria-hidden="true">{{ BRAND_MARK }}</span><span>KorASR</span>
      </router-link>
      <nav class="primary-nav" :aria-label="t('nav.main')">
        <router-link to="/">{{ t('nav.live') }}</router-link>
        <span v-if="session.isLive" class="nav-disabled" aria-disabled="true" :title="t('nav.historyLocked')">{{ t('nav.history') }}</span>
        <router-link v-else to="/history">{{ t('nav.history') }}</router-link>
        <template v-if="auth.user?.is_admin">
          <span v-if="session.isLive" class="nav-disabled" aria-disabled="true" :title="t('nav.adminLocked')">{{ t('nav.admin') }}</span>
          <router-link v-else to="/admin/users">{{ t('nav.admin') }}</router-link>
        </template>
      </nav>
      <div class="account-actions">
        <LanguageSwitcher />
        <span class="account-name" :title="auth.user?.username">{{ auth.user?.username }}</span>
        <button class="button button-quiet" type="button" :disabled="session.isLive || session.isFinalizing" :title="session.isLive || session.isFinalizing ? t('nav.logoutLocked') : t('nav.logoutTitle')" @click="logout">{{ t('nav.logout') }}</button>
      </div>
    </header>
    <router-view class="route-view" />
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watchEffect } from 'vue'
import { useRouter } from 'vue-router'
import axios from 'axios'
import LanguageSwitcher from './components/LanguageSwitcher.vue'
import { useAuthStore } from './stores/auth.js'
import { useSessionStore } from './stores/session.js'
import { locale, localeTag, t } from './i18n/index.js'
import { serverMessage } from './i18n/apiErrors.js'

// 语: the logo glyph, the same in every interface language.
const BRAND_MARK = '\u8bed'

const auth = useAuthStore()
const session = useSessionStore()
const router = useRouter()
const username = ref('')
const password = ref('')
const busy = ref(false)
// The login error as an i18n key (follows a language switch) or a server message as sent.
const errorKey = ref('')
const errorText = ref('')
const errorMessage = computed(() => (errorKey.value ? t(errorKey.value) : errorText.value))
let interceptorId

function showError(key = '', text = '') {
  errorKey.value = key
  errorText.value = text
}

// <html lang> and the tab title follow the interface language.
watchEffect(() => {
  document.documentElement.lang = localeTag(locale.value)
  document.title = t('meta.title')
})

onMounted(async () => {
  interceptorId = axios.interceptors.response.use(
    response => response,
    failure => {
      if (failure.response?.status === 401 && !failure.config?.url?.startsWith('/api/auth/')) {
        session.resetSession()
        auth.expire()
        showError('app.expired')
      }
      return Promise.reject(failure)
    },
  )
  try { await auth.refresh() }
  catch (_) { showError('app.unreachable') }
})
onUnmounted(() => axios.interceptors.response.eject(interceptorId))

async function login() {
  if (busy.value || !username.value || !password.value) return
  busy.value = true
  showError()
  try {
    session.resetSession()
    await auth.login(username.value, password.value)
    password.value = ''
    router.push('/')
  } catch (e) {
    if (e.response?.status === 401) showError('auth.invalid')
    else {
      const message = serverMessage(e)
      showError(message ? '' : 'auth.failed', message)
    }
  } finally { busy.value = false }
}

async function logout() {
  if (session.isLive || session.isFinalizing) return
  try { await auth.logout() }
  catch (_) {
    alert(t('nav.logoutFailed'))
    return
  }
  session.resetSession()
  router.push('/')
}
</script>
