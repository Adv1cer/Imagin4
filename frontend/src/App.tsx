import { useState } from 'react'
import { useAuth } from './hooks/useAuth'
import { LoginScreen } from './screens/LoginScreen'
import { RegisterScreen } from './screens/RegisterScreen'
import { ChatScreen } from './screens/ChatScreen'
import { AdminTestScreen } from './screens/AdminTestScreen'

function App() {
  const { status, user, login, register, logout } = useAuth()
  const [authView, setAuthView] = useState<'login' | 'register'>('login')

  // No routing library is installed (see package.json) -- this is intentionally the
  // simplest possible route: /admin is a standalone load-test console that manages its
  // own auth (bearer API keys typed into the page, see admin/adminApi.ts), independent
  // of the cookie-session login flow below, so it's checked before that flow runs at all.
  if (window.location.pathname.startsWith('/admin')) {
    return <AdminTestScreen />
  }

  if (status === 'checking') {
    return (
      <div className="flex h-screen items-center justify-center bg-gray-50">
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-gray-300 border-t-gray-700" />
      </div>
    )
  }

  if (status === 'anon' || !user) {
    return authView === 'register' ? (
      <RegisterScreen onRegister={register} onSwitchToLogin={() => setAuthView('login')} />
    ) : (
      <LoginScreen onLogin={login} onSwitchToRegister={() => setAuthView('register')} />
    )
  }

  return <ChatScreen user={user} onLogout={logout} />
}

export default App
