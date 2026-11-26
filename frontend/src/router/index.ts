import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import ParserRouteView from '@/views/ParserRouteView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 解析路由的回退恢复工作台：固定路径需放在动态模块路由之前
    { path: '/parser_route/recovery', name: 'parser-route-recovery', component: ParserRouteView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
