<template>
  <main class="admin-page page-container">
    <div class="page-heading">
      <p class="eyebrow">ADMINISTRATION</p>
      <h1>账号管理</h1>
      <p>由管理员创建账号。每个人只能查看自己的课堂和录音。</p>
    </div>
    <div class="admin-grid">
      <section class="surface create-panel" aria-labelledby="create-title">
        <h2 id="create-title">创建账号</h2>
        <form @submit.prevent="createUser">
          <label for="new-username">用户名</label>
          <input id="new-username" v-model.trim="newUsername" autocomplete="off" required maxlength="64" />
          <label for="new-password">初始密码</label>
          <input id="new-password" v-model="newPassword" type="password" autocomplete="new-password" required minlength="12" />
          <label class="check-row"><input v-model="newIsAdmin" type="checkbox" /> 赋予管理员权限</label>
          <button class="button button-primary" :disabled="saving || !newUsername || !newPassword">{{ saving ? '创建中…' : '创建账号' }}</button>
        </form>
        <p v-if="message" class="form-success" role="status">{{ message }}</p>
        <p v-if="error" class="form-error" role="alert">{{ error }}</p>
      </section>
      <section class="surface users-panel" aria-labelledby="users-title">
        <div class="panel-heading"><h2 id="users-title">现有账号</h2><span>{{ users.length }} 个</span></div>
        <p v-if="loading" role="status">正在加载…</p>
        <div v-else-if="!users.length" class="empty-state">暂无账号。</div>
        <div v-for="item in users" :key="item.id" class="user-row">
          <div class="user-info"><strong>{{ item.username }}</strong><span>{{ item.is_admin ? '管理员' : '成员' }} · {{ item.is_active ? '可使用' : '已停用' }}</span></div>
          <button class="button button-secondary" :disabled="busyUserId === item.id || item.id === auth.user?.id" @click="toggleActive(item)">{{ item.is_active ? '停用' : '启用' }}</button>
          <button class="button button-secondary" :disabled="busyUserId === item.id" @click="startReset(item)">重设密码</button>
        </div>
      </section>
    </div>
    <div v-if="resetUser" class="modal-backdrop" @click.self="resetUser = null">
      <form class="surface modal-card" role="dialog" aria-modal="true" aria-labelledby="reset-title" @submit.prevent="resetPassword">
        <h2 id="reset-title">重设 {{ resetUser.username }} 的密码</h2>
        <label for="reset-password">新密码</label>
        <input id="reset-password" v-model="resetValue" type="password" autocomplete="new-password" minlength="12" required autofocus />
        <p v-if="resetError" class="form-error" role="alert">{{ resetError }}</p>
        <div class="modal-actions"><button type="button" class="button button-secondary" @click="resetUser = null">取消</button><button type="submit" class="button button-primary" :disabled="busyUserId === resetUser.id">保存</button></div>
      </form>
    </div>
  </main>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import axios from 'axios'
import { useAuthStore } from '../stores/auth.js'

const auth = useAuthStore()
const users = ref([])
const loading = ref(true)
const saving = ref(false)
const busyUserId = ref(null)
const newUsername = ref('')
const newPassword = ref('')
const newIsAdmin = ref(false)
const message = ref('')
const error = ref('')
const resetUser = ref(null)
const resetValue = ref('')
const resetError = ref('')

function apiError(e, fallback) {
  const detail = e.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map(item => item.msg).filter(Boolean).join('；') || fallback
  return fallback
}

async function loadUsers() {
  loading.value = true
  try {
    const { data } = await axios.get('/api/admin/users')
    users.value = data
  } catch (e) { error.value = apiError(e, '读取账号失败。') }
  finally { loading.value = false }
}
onMounted(loadUsers)

async function createUser() {
  saving.value = true
  error.value = ''
  message.value = ''
  try {
    const { data } = await axios.post('/api/admin/users', { username: newUsername.value, password: newPassword.value, is_admin: newIsAdmin.value })
    users.value.push(data)
    message.value = `已创建 ${data.username}。请通过安全渠道交付初始密码。`
    newUsername.value = ''
    newPassword.value = ''
    newIsAdmin.value = false
  } catch (e) { error.value = apiError(e, '创建账号失败。') }
  finally { saving.value = false }
}

async function toggleActive(item) {
  if (item.is_active && !confirm(`停用 ${item.username}？该账号将无法继续登录。`)) return
  busyUserId.value = item.id
  error.value = ''
  try {
    const { data } = await axios.patch(`/api/admin/users/${item.id}`, { is_active: !item.is_active })
    Object.assign(item, data)
  } catch (e) { error.value = apiError(e, '更新账号失败。') }
  finally { busyUserId.value = null }
}

function startReset(item) {
  resetUser.value = item
  resetValue.value = ''
  resetError.value = ''
}

async function resetPassword() {
  busyUserId.value = resetUser.value.id
  resetError.value = ''
  try {
    await axios.patch(`/api/admin/users/${resetUser.value.id}`, { password: resetValue.value })
    message.value = `已重设 ${resetUser.value.username} 的密码。`
    resetUser.value = null
  } catch (e) { resetError.value = apiError(e, '重设密码失败。') }
  finally { busyUserId.value = null }
}
</script>

<style scoped>
.admin-page { padding-block: 2.25rem 4rem; }
.page-heading { margin-bottom: 1.5rem; }
.page-heading p:last-child { color: var(--text-muted); margin-top: .4rem; }
.admin-grid { display: grid; grid-template-columns: minmax(260px, 360px) minmax(0, 1fr); gap: 1.25rem; align-items: start; }
.create-panel,.users-panel { padding: 1.5rem; }
h2 { font-size: 1.12rem; margin: 0 0 1.2rem; }
form { display: grid; gap: .55rem; }
form label { margin-top: .5rem; }
form .button { margin-top: .8rem; }
.check-row { display: flex; align-items: center; gap: .6rem; }
.check-row input { width: 1.1rem; height: 1.1rem; }
.panel-heading { display: flex; justify-content: space-between; color: var(--text-muted); }
.user-row { display: flex; align-items: center; gap: .5rem; padding: .8rem 0; border-top: 1px solid var(--border); }
.user-info { flex: 1; display: grid; min-width: 0; }
.user-info span { color: var(--text-muted); font-size: .85rem; }
.modal-backdrop { position: fixed; inset: 0; z-index: 100; display: grid; place-items: center; background: rgba(20,27,40,.42); padding: 1rem; }
.modal-card { width: min(100%, 420px); padding: 1.5rem; }
.modal-actions { display: flex; justify-content: flex-end; gap: .5rem; margin-top: 1rem; }
@media(max-width:800px) { .admin-grid { grid-template-columns: 1fr; } .user-row { flex-wrap: wrap; } }
</style>
