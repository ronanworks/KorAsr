import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import axios from 'axios'

export const useAuthStore = defineStore('auth', () => {
  const ready = ref(false)
  const configured = ref(true)
  const user = ref(null)
  const authenticated = computed(() => Boolean(user.value))

  async function refresh() {
    try {
      const { data } = await axios.get('/api/auth/status')
      configured.value = data.configured
      user.value = data.authenticated ? data.user : null
    } finally {
      ready.value = true
    }
  }

  async function login(username, password) {
    const { data } = await axios.post('/api/auth/login', { username, password })
    user.value = data.user
    configured.value = true
  }

  async function logout() {
    await axios.post('/api/auth/logout')
    user.value = null
  }

  function expire() { user.value = null }

  return { ready, configured, user, authenticated, refresh, login, logout, expire }
})
