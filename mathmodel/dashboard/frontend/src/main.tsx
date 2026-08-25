import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import { LanguageProvider } from './i18n'
import 'katex/dist/katex.min.css'
import './styles.css'

function WorkspaceEntry() {
  const authRequired = import.meta.env.VITE_AUTH_REQUIRED === 'true'
  const token = window.localStorage.getItem('access_token') || window.localStorage.getItem('token')

  if (authRequired && !token) {
    return <main className="workspace-auth-gate">
      <div className="workspace-auth-card">
        <span>MATHMODEL / WORKSPACE</span>
        <h1>登录后进入工作区</h1>
        <p>项目记录、运行过程与交付物会绑定到你的账号。</p>
        <a href="/?workspaceLogin=1">返回首页并登录 <b>→</b></a>
      </div>
    </main>
  }

  return <App />
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <LanguageProvider>
      <WorkspaceEntry />
    </LanguageProvider>
  </StrictMode>,
)
