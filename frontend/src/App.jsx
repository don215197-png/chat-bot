import { useState, useRef, useEffect, useCallback, useMemo } from 'react'
import { marked } from 'marked'
import './App.css'

const STORAGE_KEY = 'ai-chatbot-messages'
const THEME_KEY = 'ai-chatbot-theme'

function loadMessages() {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored) {
      const parsed = JSON.parse(stored)
      if (Array.isArray(parsed)) return parsed
    }
  } catch (e) {
    console.warn('Failed to load messages from localStorage:', e)
  }
  return []
}

function saveMessages(messages) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(messages))
  } catch (e) {
    console.warn('Failed to save messages to localStorage:', e)
  }
}

function loadTheme() {
  try {
    const stored = localStorage.getItem(THEME_KEY)
    if (stored === 'light' || stored === 'dark') return stored
  } catch {}
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

function formatRelativeTime(timestamp) {
  const now = Date.now()
  const diff = now - timestamp
  const seconds = Math.floor(diff / 1000)
  const minutes = Math.floor(seconds / 60)
  const hours = Math.floor(minutes / 60)
  const days = Math.floor(hours / 24)

  if (seconds < 60) return 'just now'
  if (minutes < 60) return `${minutes}m ago`
  if (hours < 24) return `${hours}h ago`
  if (days < 7) return `${days}d ago`
  return new Date(timestamp).toLocaleDateString()
}

function MessageBubble({ message, isStreaming, onCopy, onRetry, onErrorDismiss }) {
  const isUser = message.role === 'user'
  const content = message.content || ''
  const showError = message.error && !isStreaming
  const showRetry = message.failed && !isStreaming

  return (
    <div className={`message-bubble ${isUser ? 'user' : 'assistant'} ${isStreaming ? 'streaming' : ''}`}>
      <div className="message-header">
        <span className="message-role">{isUser ? 'You' : 'AI'}</span>
        <span className="message-time">{formatRelativeTime(message.timestamp)}</span>
      </div>
      <div className="message-content">
        {isUser ? (
          <pre className="user-text">{content}</pre>
        ) : content ? (
          <MarkdownRenderer content={content} onCopy={onCopy} />
        ) : message.thinking || isStreaming ? (
          <div className="thinking-indicator">
            <span className="thinking-spinner" />
            <span>Thinking...</span>
          </div>
        ) : null}
        {isStreaming && <span className="streaming-cursor" aria-hidden="true" />}
        {showError && (
          <div className="message-error">
            <span>{message.error}</span>
            <button type="button" className="dismiss-error" onClick={onErrorDismiss} aria-label="Dismiss error">×</button>
          </div>
        )}
        {showRetry && (
          <button type="button" className="retry-button" onClick={onRetry}>
            Retry
          </button>
        )}
      </div>
    </div>
  )
}

function MarkdownRenderer({ content, onCopy }) {
  const [html, setHtml] = useState('')

  useEffect(() => {
    setHtml(marked.parse(content || ''))
  }, [content])

  const copyCode = (code) => {
    navigator.clipboard.writeText(code).then(() => {
      onCopy?.()
    })
  }

  return (
    <div className="markdown-content" dangerouslySetInnerHTML={{ __html: html }} />
  )
}

function TextareaInput({ value, onChange, onSend, disabled, placeholder }) {
  const textareaRef = useRef(null)

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 150)}px`
    }
  }, [value])

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      if (value.trim() && !disabled) onSend(e)
    }
  }

  return (
    <textarea
      ref={textareaRef}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={handleKeyDown}
      disabled={disabled}
      placeholder={placeholder}
      className="message-input"
      aria-label="Message input"
      rows={1}
    />
  )
}

function App() {
  const [messages, setMessages] = useState(() => loadMessages())
  const [input, setInput] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [globalError, setGlobalError] = useState('')
  const [theme, setTheme] = useState(() => loadTheme())
  const [isOnline, setIsOnline] = useState(true)
  const messagesEndRef = useRef(null)
  const abortControllerRef = useRef(null)
  const retryTimeoutRef = useRef(null)

  useEffect(() => {
    saveMessages(messages)
  }, [messages])

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    localStorage.setItem(THEME_KEY, theme)
  }, [theme])

  useEffect(() => {
    const handleOnline = () => setIsOnline(true)
    const handleOffline = () => setIsOnline(false)
    setIsOnline(navigator.onLine)
    window.addEventListener('online', handleOnline)
    window.addEventListener('offline', handleOffline)
    return () => {
      window.removeEventListener('online', handleOnline)
      window.removeEventListener('offline', handleOffline)
    }
  }, [])

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [])

  useEffect(() => {
    scrollToBottom()
  }, [messages, scrollToBottom])

  const handleCopy = useCallback(() => {
    setGlobalError('Copied!')
    setTimeout(() => setGlobalError(''), 2000)
  }, [])

  const handleSend = async (e) => {
    e?.preventDefault()
    if (!input.trim() || isLoading || !isOnline) return

    const userMessage = input.trim()
    const timestamp = Date.now()
    const newMessages = [...messages, { role: 'user', content: userMessage, timestamp }]
    setMessages(newMessages)
    setInput('')
    setIsLoading(true)
    setGlobalError('')

    abortControllerRef.current = new AbortController()
    const timeoutId = setTimeout(() => {
      abortControllerRef.current?.abort()
    }, 120000)

    try {
      const response = await fetch('http://localhost:8000/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: newMessages, stream: true }),
        signal: abortControllerRef.current.signal
      })

      clearTimeout(timeoutId)

      if (!response.ok) {
        const data = await response.json()
        throw new Error(data.detail || 'Failed to get response')
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let assistantMessage = ''
      const assistantTimestamp = Date.now()

      setMessages(prev => [...prev, { role: 'assistant', content: '', timestamp: assistantTimestamp, streaming: true }])

      let streamBuffer = ''
      let isStreamFinished = false

      while (!isStreamFinished) {
        const { done, value } = await reader.read()
        if (done) break

        streamBuffer += decoder.decode(value, { stream: true })
        const parts = streamBuffer.split('\n\n')
        streamBuffer = parts.pop() || ''

        for (const block of parts) {
          if (isStreamFinished) break
          const lines = block.split('\n')
          for (const line of lines) {
            const trimmedLine = line.trim()
            if (trimmedLine.startsWith('data: ')) {
              const data = trimmedLine.slice(6).trim()
              if (data === '[DONE]') {
                isStreamFinished = true
                break
              }
              try {
                const parsed = JSON.parse(data)
                if (parsed.thinking) {
                  setMessages(prev => {
                    const updated = [...prev]
                    if (updated.length > 0 && updated[updated.length - 1].role === 'assistant') {
                      updated[updated.length - 1] = { ...updated[updated.length - 1], thinking: true }
                    }
                    return updated
                  })
                }
                if (parsed.content) {
                  assistantMessage += parsed.content
                  setMessages(prev => {
                    const updated = [...prev]
                    if (updated.length > 0 && updated[updated.length - 1].role === 'assistant') {
                      updated[updated.length - 1] = { role: 'assistant', content: assistantMessage, timestamp: assistantTimestamp, streaming: true, thinking: false }
                    }
                    return updated
                  })
                }
                if (parsed.error) throw new Error(parsed.error)
              } catch (e) {
                if (e instanceof SyntaxError) continue
                throw e
              }
            } else if (trimmedLine.startsWith('event: error')) {
              const errorData = trimmedLine.slice(10)
              try {
                const parsed = JSON.parse(errorData)
                if (parsed.data?.error) throw new Error(parsed.data.error)
              } catch { throw new Error('Stream error') }
            }
          }
        }
      }

      if (streamBuffer.trim()) {
        const lines = streamBuffer.split('\n')
        for (const line of lines) {
          const trimmedLine = line.trim()
          if (trimmedLine.startsWith('data: ')) {
            const data = trimmedLine.slice(6).trim()
            if (data !== '[DONE]') {
              try {
                const parsed = JSON.parse(data)
                if (parsed.content) {
                  assistantMessage += parsed.content
                }
              } catch {}
            }
          }
        }
      }

      setMessages(prev => {
        const updated = [...prev]
        if (updated.length > 0 && updated[updated.length - 1].role === 'assistant') {
          updated[updated.length - 1] = { role: 'assistant', content: assistantMessage, timestamp: assistantTimestamp, streaming: false, thinking: false, failed: false }
        }
        return updated
      })
    } catch (err) {
      clearTimeout(timeoutId)
      if (err.name === 'AbortError') {
        setGlobalError('Request timed out')
        setMessages(prev => {
          const updated = [...prev]
          if (updated.length > 0 && updated[updated.length - 1].role === 'assistant' && updated[updated.length - 1].streaming) {
            updated[updated.length - 1] = { ...updated[updated.length - 1], content: updated[updated.length - 1].content || '', streaming: false, failed: true, error: 'Request timed out' }
          }
          return updated
        })
        return
      }
      const errorMsg = err.message || 'Failed to get response'
      setGlobalError(errorMsg)
      setMessages(prev => {
        const updated = [...prev]
        if (updated.length > 0 && updated[updated.length - 1].role === 'assistant' && updated[updated.length - 1].streaming) {
          updated[updated.length - 1] = { ...updated[updated.length - 1], content: updated[updated.length - 1].content || '', streaming: false, failed: true, error: errorMsg }
        } else {
          updated.push({ role: 'assistant', content: '', timestamp: Date.now(), streaming: false, failed: true, error: errorMsg })
        }
        return updated
      })
    } finally {
      setIsLoading(false)
      abortControllerRef.current = null
    }
  }

  const handleRetry = useCallback((index) => {
    if (isLoading) return
    const msg = messages[index]
    if (!msg || msg.role !== 'assistant' || !msg.failed) return

    const userMsgIndex = messages.findLastIndex((m, i) => i < index && m.role === 'user')
    if (userMsgIndex === -1) return

    const conversationUpToUser = messages.slice(0, userMsgIndex + 1)
    setMessages(conversationUpToUser)
    setInput(messages[userMsgIndex].content)
    handleSend(new Event('submit'))
  }, [messages, isLoading, handleSend])

  const handleDismissError = useCallback((index) => {
    setMessages(prev => prev.map((m, i) => i === index ? { ...m, error: null } : m))
  }, [])

  const handleClear = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort()
    }
    if (retryTimeoutRef.current) {
      clearTimeout(retryTimeoutRef.current)
    }
    setMessages([])
    localStorage.removeItem(STORAGE_KEY)
    setGlobalError('')
    setIsLoading(false)
  }, [])

  const toggleTheme = useCallback(() => {
    setTheme(t => t === 'dark' ? 'light' : 'dark')
  }, [])

  return (
    <div className="app" data-theme={theme}>
      <header className="header">
        <div className="header-content">
          <h1>AI Chatbot</h1>
          <p>Powered by hy3-free</p>
        </div>
        <button type="button" className="theme-toggle" onClick={toggleTheme} aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}>
          {theme === 'dark' ? '☀️' : '🌙'}
        </button>
      </header>
      
      <main className="chat-container">
        {!isOnline && (
          <div className="offline-banner" role="alert">
            📴 You're offline — messages will send when connection restores
          </div>
        )}

        <div className="messages" role="log" aria-live="polite">
          {messages.map((msg, index) => (
            <div key={index} className={`message ${msg.role} ${msg.streaming ? 'streaming' : ''} ${msg.failed ? 'failed' : ''}`}>
              <MessageBubble
                message={msg}
                isStreaming={msg.streaming}
                onCopy={handleCopy}
                onRetry={() => handleRetry(index)}
                onErrorDismiss={() => handleDismissError(index)}
              />
            </div>
          ))}
          <div ref={messagesEndRef} />
        </div>

        {globalError && !isLoading && (
          <div className="error-banner" role="alert">
            {globalError}
          </div>
        )}

        <form className="input-form" onSubmit={handleSend}>
          <TextareaInput
            value={input}
            onChange={setInput}
            onSend={handleSend}
            disabled={isLoading || !isOnline}
            placeholder={isLoading ? 'Waiting for response...' : !isOnline ? 'You are offline' : 'Type a message... (Enter to send, Shift+Enter for new line)'}
          />
          <button
            type="submit"
            disabled={isLoading || !input.trim() || !isOnline}
            className="send-button"
            aria-label="Send message"
          >
            {isLoading ? 'Sending...' : 'Send'}
          </button>
        </form>

        {messages.length > 0 && (
          <button
            type="button"
            onClick={handleClear}
            className="clear-button"
            aria-label="Clear chat history"
          >
            Clear chat
          </button>
        )}
      </main>
    </div>
  )
}

export default App