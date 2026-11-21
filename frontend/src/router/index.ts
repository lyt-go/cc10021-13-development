import { createRouter, createWebHistory } from 'vue-router'

import IncidentAttributionView from '@/views/IncidentAttributionView.vue'
import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    {
      path: '/dq_incident_attribution',
      name: 'incident-attribution',
      component: IncidentAttributionView,
    },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
