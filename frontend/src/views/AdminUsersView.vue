<template>
  <main class="admin-page page-container">
    <div class="page-heading">
      <p class="eyebrow">{{ t('admin.eyebrow') }}</p>
      <h1>{{ t('admin.title') }}</h1>
      <p>{{ t('admin.description') }}</p>
    </div>
    <div class="admin-grid">
      <section class="surface create-panel" aria-labelledby="create-title">
        <h2 id="create-title">{{ t('admin.createTitle') }}</h2>
        <form @submit.prevent="createUser">
          <label for="new-username">{{ t('admin.username') }}</label>
          <input id="new-username" v-model.trim="newUsername" autocomplete="off" required maxlength="64" />
          <label for="new-password">{{ t('admin.initialPassword') }}</label>
          <input id="new-password" v-model="newPassword" type="password" autocomplete="new-password" required minlength="12" />
          <label class="check-row"><input v-model="newIsAdmin" type="checkbox" /> {{ t('admin.makeAdmin') }}</label>
          <button class="button button-primary" :disabled="saving || !newUsername || !newPassword">{{ saving ? t('admin.creating') : t('admin.create') }}</button>
        </form>
        <p v-if="message" class="form-success" role="status">{{ message }}</p>
        <p v-if="error" class="form-error" role="alert">{{ error }}</p>
      </section>
      <section class="surface users-panel" aria-labelledby="users-title">
        <div class="panel-heading"><h2 id="users-title">{{ t('admin.usersTitle') }}</h2><span>{{ t('admin.count', { count: users.length }) }}</span></div>
        <p v-if="loading" role="status">{{ t('common.loading') }}</p>
        <div v-else-if="!users.length" class="empty-state">{{ t('admin.empty') }}</div>
        <div v-for="item in users" :key="item.id" class="user-row">
          <div class="user-info"><strong>{{ item.username }}</strong><span>{{ item.is_admin ? t('admin.roleAdmin') : t('admin.roleMember') }} · {{ item.is_active ? t('admin.active') : t('admin.disabled') }}</span></div>
          <button class="button button-secondary" :disabled="busyUserId === item.id || item.id === auth.user?.id" @click="toggleActive(item)">{{ item.is_active ? t('admin.disable') : t('admin.enable') }}</button>
          <button class="button button-secondary" :disabled="busyUserId === item.id" @click="startReset(item)">{{ t('admin.resetPassword') }}</button>
        </div>
      </section>
    </div>
    <div v-if="resetUser" class="modal-backdrop" @click.self="resetUser = null">
      <form class="surface modal-card" role="dialog" aria-modal="true" aria-labelledby="reset-title" @submit.prevent="resetPassword">
        <h2 id="reset-title">{{ t('admin.resetTitle', { user: resetUser.username }) }}</h2>
        <label for="reset-password">{{ t('admin.newPassword') }}</label>
        <input id="reset-password" v-model="resetValue" type="password" autocomplete="new-password" minlength="12" required autofocus />
        <p v-if="resetError" class="form-error" role="alert">{{ resetError }}</p>
        <div class="modal-actions"><button type="button" class="button button-secondary" @click="resetUser = null">{{ t('common.cancel') }}</button><button type="submit" class="button button-primary" :disabled="busyUserId === resetUser.id">{{ t('common.save') }}</button></div>
      </form>
    </div>
  </main>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import axios from 'axios'
import { useAuthStore } from '../stores/auth.js'
import { t } from '../i18n/index.js'
import { serverMessage } from '../i18n/apiErrors.js'

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
  return serverMessage(e) || fallback
}

async function loadUsers() {
  loading.value = true
  try {
    const { data } = await axios.get('/api/admin/users')
    users.value = data
  } catch (e) { error.value = apiError(e, t('admin.loadFailed')) }
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
    message.value = t('admin.created', { user: data.username })
    newUsername.value = ''
    newPassword.value = ''
    newIsAdmin.value = false
  } catch (e) { error.value = apiError(e, t('admin.createFailed')) }
  finally { saving.value = false }
}

async function toggleActive(item) {
  if (item.is_active && !confirm(t('admin.confirmDisable', { user: item.username }))) return
  busyUserId.value = item.id
  error.value = ''
  try {
    const { data } = await axios.patch(`/api/admin/users/${item.id}`, { is_active: !item.is_active })
    Object.assign(item, data)
  } catch (e) { error.value = apiError(e, t('admin.updateFailed')) }
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
    message.value = t('admin.resetDone', { user: resetUser.value.username })
    resetUser.value = null
  } catch (e) { resetError.value = apiError(e, t('admin.resetFailed')) }
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
