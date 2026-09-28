// frontend/src/router/index.js
import { createRouter, createWebHistory } from 'vue-router'
import ClassroomView from '../views/ClassroomView.vue'
import HistoryView from '../views/HistoryView.vue'
import AdminUsersView from '../views/AdminUsersView.vue'
import { useAuthStore } from '../stores/auth.js'
import { useSessionStore } from '../stores/session.js'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: ClassroomView },
    { path: '/history', component: HistoryView },
    { path: '/admin/users', component: AdminUsersView, meta: { admin: true } },
  ],
})

router.beforeEach(async to => {
  if (useSessionStore().isRecording && to.path !== '/') return false
  if (!to.meta.admin) return
  const auth = useAuthStore()
  if (!auth.ready) {
    try { await auth.refresh() }
    catch (_) { return '/' }
  }
  if (!auth.user?.is_admin) return '/'
})

export default router
