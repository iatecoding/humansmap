import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'

const routes = [
  { path: '/', name: 'home', component: App, meta: { isRoot: true } },
  { path: '/person/:slug', name: 'person', component: App, meta: { isRoot: true } },
  { path: '/organization/:slug', name: 'organization', component: App, meta: { isRoot: true } },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})
