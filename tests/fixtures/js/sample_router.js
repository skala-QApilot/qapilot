import { createRouter, createWebHistory } from 'vue-router'
import Home from '../pages/Home.vue'
import Detail from '../pages/Detail.vue'

const routes = [
  { path: '/', component: Home },
  { path: '/items/:id', component: Detail, meta: { requiresAuth: true } },
  { path: '/admin', component: Home, meta: { requiresRole: 'admin' } },
  { path: '/old', redirect: '/' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
