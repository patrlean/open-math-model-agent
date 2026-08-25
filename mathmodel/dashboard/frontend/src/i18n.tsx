import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react'

export type Language = 'en' | 'zh'

const STORAGE_KEY = 'mathmodel-ui-language'

interface LanguageContextValue {
  language: Language
  setLanguage: (language: Language) => void
  toggleLanguage: () => void
}

const LanguageContext = createContext<LanguageContextValue | null>(null)

export function storedLanguage(): Language {
  if (typeof window === 'undefined') return 'en'
  return window.localStorage.getItem(STORAGE_KEY) === 'zh' ? 'zh' : 'en'
}

export function LanguageProvider({ children }: { children: ReactNode }) {
  const [language, setLanguageState] = useState<Language>(storedLanguage)

  const setLanguage = useCallback((next: Language) => {
    setLanguageState(next)
    window.localStorage.setItem(STORAGE_KEY, next)
  }, [])

  const toggleLanguage = useCallback(() => {
    setLanguage(language === 'en' ? 'zh' : 'en')
  }, [language, setLanguage])

  useEffect(() => {
    document.documentElement.lang = language === 'en' ? 'en' : 'zh-CN'
  }, [language])

  const value = useMemo(
    () => ({ language, setLanguage, toggleLanguage }),
    [language, setLanguage, toggleLanguage],
  )

  return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>
}

export function useLanguage() {
  const context = useContext(LanguageContext)
  if (!context) throw new Error('useLanguage must be used inside LanguageProvider')
  return context
}

export function LanguageSwitcher({ className = '' }: { className?: string }) {
  const { language, toggleLanguage } = useLanguage()
  const nextLabel = language === 'en' ? 'Switch to Chinese' : '切换到英文'

  return (
    <button
      type="button"
      className={`language-switcher ${className}`.trim()}
      onClick={toggleLanguage}
      aria-label={nextLabel}
      title={nextLabel}
    >
      <span className={language === 'en' ? 'active' : ''}>EN</span>
      <i aria-hidden="true" />
      <span className={language === 'zh' ? 'active' : ''}>中文</span>
    </button>
  )
}
