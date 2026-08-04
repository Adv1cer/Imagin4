import { useState } from 'react'
import { useAuth } from './hooks/useAuth'
import { LoginScreen } from './screens/LoginScreen'
import { RegisterScreen } from './screens/RegisterScreen'
import { ChatScreen } from './screens/ChatScreen'

function App() {
  const { status, user, login, register, logout } = useAuth()
  const [authView, setAuthView] = useState<'login' | 'register'>('login')

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
