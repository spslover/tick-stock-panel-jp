import { lazy } from 'react'
import { Globe } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'

export const GlobalAnalysisPage = lazy(() =>
  import('./Page').then(m => ({ default: m.GlobalAnalysisPage })),
)

const extension: FrontendExtension = {
  id: 'official.global-analysis',
  apiVersion: 1,
  routes: [
    { id: 'global-analysis', path: '/global-analysis', component: GlobalAnalysisPage },
  ],
  navigation: [
    { id: 'global-analysis', routeId: 'global-analysis', label: '日美股市 AI', icon: Globe },
  ],
}

export default extension
