import { useState, useRef, useEffect, useCallback, useMemo } from 'react'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/core'
import javascript from 'highlight.js/lib/languages/javascript'
import typescript from 'highlight.js/lib/languages/typescript'
import python from 'highlight.js/lib/languages/python'
import xml from 'highlight.js/lib/languages/xml'
import css from 'highlight.js/lib/languages/css'
import json from 'highlight.js/lib/languages/json'
import bash from 'highlight.js/lib/languages/bash'
import sql from 'highlight.js/lib/languages/sql'
import './App.css'

// Conversations and messages live on the backend (SQLite) behind a login; the
// browser only remembers the user's session token so refresh keeps them signed
// in. Theme preference is the one purely local setting.
const SESSION_KEY = 'ai-chatbot-session'
const THEME_KEY = 'ai-chatbot-theme'

// Syntax highlighting in "core + a handful of common languages" mode only —
// registering these individually keeps the bundle lean instead of pulling in
// the full highlight.js language and theme pack. Token colors are applied via
// CSS variables that follow the app's dark/light theme (no theme files).
const HIGHLIGHT_LANGUAGES = { javascript, typescript, python, xml, css, json, bash, sql }
Object.entries(HIGHLIGHT_LANGUAGES).forEach(([name, definition]) => hljs.registerLanguage(name, definition))

// Suggested prompts shown on the empty-state welcome screen. Clicking one
// populates the input; it does not auto-send.
const SUGGESTED_PROMPTS = [
  'Explain a concept',
  'Help me debug something',
  'Draft an email',
  'Summarize a topic',
]

// Backend base URL. Defaults to the local dev backend; override with
// VITE_API_URL in frontend/.env(.local) for production.
const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

// Small unique id generator for conversations. crypto.randomUUID is used when
// available (secure contexts); the fallback covers non-secure origins.
function makeId() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return 'c-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10)
}

// Conversation titles are derived on the fly from the first user message (or
// left empty so the sidebar renders "New chat"). Truncated to ~40 chars.
function deriveTitle(messages) {
  const firstUser = Array.isArray(messages) ? messages.find(m => m.role === 'user') : null
  if (!firstUser) return ''
  const text = String(firstUser.content || '').trim()
  return text.length > 40 ? text.slice(0, 40) + '…' : text
}

// Session helpers: {token, email} is the only thing persisted locally. The
// token is a server-side session id (SQLite sessions table); everything else —
// conversations, messages, published sites — is fetched from the backend.
function loadSession() {
  try {
    const stored = localStorage.getItem(SESSION_KEY)
    if (stored) {
      const parsed = JSON.parse(stored)
      if (parsed && parsed.token) return parsed
    }
  } catch (e) {
    console.warn('Failed to load session from localStorage:', e)
  }
  return null
}

function saveSession(session) {
  try {
    if (session) localStorage.setItem(SESSION_KEY, JSON.stringify(session))
    else localStorage.removeItem(SESSION_KEY)
  } catch {}
}

// Server timestamps arrive as ISO-8601 strings; convert to a millisecond epoch
// once so every consumer (message time, sidebar sort) shares one representation.
function parseIso(value) {
  if (typeof value === 'number') return value
  const ms = Date.parse(String(value || ''))
  return Number.isNaN(ms) ? Date.now() : ms
}

// Mirror of the server-side leak cleanup, applied client-side so leaked
// reasoning is also stripped from the CURRENT streamed response (the server
// only cleans past turns before resending them to the model). Chosen over
// buffering the SSE stream server-side because the client already accumulates
// the full reply, so the same strip is a single function call at final render.
function cleanReasoningLeak(content) {
  const head = content.slice(0, 150)
  if (head.includes('The user ') || head.includes('As an AI ')) {
    const doctypeIdx = content.indexOf('<!DOCTYPE')
    if (doctypeIdx !== -1) return content.slice(doctypeIdx)
    const fenceIdx = content.indexOf('```')
    if (fenceIdx !== -1) return content.slice(fenceIdx)
  }
  return content
}

// Extract the single ```html fenced block from an assistant reply. The system
// prompt instructs the model to emit exactly one self-contained block, so the
// first match wins. Returns the inner markup (trimmed) or null when the message
// contains no HTML block.
function extractHtmlBlock(content) {
  if (!content) return null
  const match = content.match(/```\s*html\s*([\s\S]*?)```/i)
  return match ? match[1].trim() : null
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

function MessageBubble({ message, isStreaming, isEditable, isEditing, editDraft, onCopy, onRetry, onErrorDismiss, onEdit, onEditDraftChange, onResendEdit, onCancelEdit, onPublish }) {
  const isUser = message.role === 'user'
  const content = message.content || ''
  const showError = message.error && !isStreaming
  const showRetry = message.failed && !isStreaming
  // Only finished assistant messages can expose a preview card. The html field
  // is set by the current client; messages from older history fall back to
  // extracting the fenced block on the fly.
  const htmlBlock = isUser || isStreaming ? null : (message.html ?? extractHtmlBlock(content))

  return (
    <div className={`message-bubble ${isUser ? 'user' : 'assistant'} ${isStreaming ? 'streaming' : ''}`}>
      <div className="message-header">
        <span className="message-role">{isUser ? 'You' : 'AI'}</span>
        <span className="message-time">{formatRelativeTime(message.timestamp)}</span>
      </div>
      <div className="message-content">
        {isUser ? (
          isEditing ? (
            <div className="edit-box">
              <textarea
                className="edit-input"
                value={editDraft}
                onChange={(e) => onEditDraftChange(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault()
                    onResendEdit()
                  }
                }}
                aria-label="Edit message"
                rows={2}
              />
              <div className="edit-actions">
                <button type="button" className="edit-cancel" onClick={onCancelEdit}>Cancel</button>
                <button type="button" className="retry-button edit-resend" onClick={onResendEdit} disabled={!editDraft.trim()}>Resend</button>
              </div>
            </div>
          ) : isEditable ? (
            <button type="button" className="user-text-btn" onClick={onEdit} aria-label="Edit and resend message">
              <pre className="user-text">{content}</pre>
            </button>
          ) : (
            <pre className="user-text">{content}</pre>
          )
        ) : content ? (
          <>
            <MarkdownRenderer content={content} onCopy={onCopy} />
            {htmlBlock && (
              <GeneratedSiteCard
                html={htmlBlock}
                publishing={!!message.publishing}
                publishedUrl={message.publishedUrl}
                publishError={message.publishError}
                onPublish={onPublish}
                onCopy={onCopy}
              />
            )}
          </>
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
  const containerRef = useRef(null)

  // Sanitize the rendered markdown with DOMPurify before injecting it into
  // the DOM. The model's output is untrusted, so without this any raw HTML
  // it emits (e.g. <script>, event handlers) would execute in the page.
  // Memoized so streaming re-renders with unchanged content skip reparsing.
  const html = useMemo(() => DOMPurify.sanitize(marked.parse(content || '')), [content])

  // Post-process the sanitized DOM to inject a copy button into each <pre>
  // block. This mutates the (already-sanitized) DOM rather than adding markup
  // during markdown parsing, so raw code text never touches an HTML attribute
  // or the parse path before sanitization. Re-runs for every new batch of
  // streamed content; each <pre> is re-processed fresh after React rewrites
  // innerHTML, and the guard skips blocks that already carry a button.
  useEffect(() => {
    if (!containerRef.current) return
    containerRef.current.querySelectorAll('pre').forEach((pre) => {
      const code = pre.querySelector('code')
      if (!code) return

      // Lightweight highlight for fenced blocks with a registered language.
      // Guarded so blocks already highlighted (e.g. a re-run without a DOM
      // rewrite) are skipped. Token colors come from CSS variables that follow
      // the app theme, so no theme stylesheets need bundling.
      const langMatch = (code.className || '').match(/language-([\w-]+)/)
      if (langMatch && hljs.getLanguage(langMatch[1]) && !code.classList.contains('hljs')) {
        try {
          hljs.highlightElement(code)
        } catch {}
      }

      if (pre.querySelector('.copy-code-btn')) return
      const copyBtn = document.createElement('button')
      copyBtn.type = 'button'
      copyBtn.className = 'copy-code-btn'
      copyBtn.setAttribute('aria-label', 'Copy code')
      copyBtn.textContent = 'Copy'
      copyBtn.addEventListener('click', () => {
        navigator.clipboard.writeText(code.textContent || '')
          .then(() => {
            copyBtn.textContent = 'Copied'
            onCopy?.()
          })
          .catch(() => {
            copyBtn.textContent = 'Copy failed'
          })
          .finally(() => {
            setTimeout(() => {
              copyBtn.textContent = 'Copy'
            }, 1500)
          })
      })
      pre.appendChild(copyBtn)
    })
  }, [html, onCopy])

  return (
    <div ref={containerRef} className="markdown-content" dangerouslySetInnerHTML={{ __html: html }} />
  )
}

// Interactive card shown under assistant messages that contain a generated
// HTML site. Defaults to just the markdown message; the Preview / Code tabs let
// the user switch what appears below it. Preview renders the extracted HTML in
// a sandboxed iframe (untrusted model output must never run with same-origin
// powers), Code shows the syntax-highlighted raw markup, and Publish posts it
// to the backend /sites endpoint — nothing is published by default.
function GeneratedSiteCard({ html, publishing, publishedUrl, publishError, onPublish, onCopy }) {
  const [view, setView] = useState(null)
  const [copiedHtml, setCopiedHtml] = useState(false)

  const fullUrl = publishedUrl ? `${API_URL}${publishedUrl}` : null

  // Reuse the already-registered xml grammar (html is one of its aliases) so
  // the Code tab shares the same highlight.js token palette as chat messages.
  const highlightedHtml = useMemo(() => {
    try {
      return hljs.highlight(html, { language: 'xml' }).value
    } catch {
      return ''
    }
  }, [html])

  const copyHtml = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(html)
      setCopiedHtml(true)
      onCopy?.()
      setTimeout(() => setCopiedHtml(false), 1500)
    } catch {}
  }, [html, onCopy])

  const copyUrl = useCallback(async () => {
    if (!fullUrl) return
    try {
      await navigator.clipboard.writeText(fullUrl)
      onCopy?.()
    } catch {}
  }, [fullUrl, onCopy])

  const toggleTab = (tab) => setView(v => (v === tab ? null : tab))

  return (
    <div className="site-card">
      <div className="site-tabs" role="tablist" aria-label="Generated site options">
        <button
          type="button"
          role="tab"
          aria-selected={view === 'preview'}
          className={`site-tab ${view === 'preview' ? 'active' : ''}`}
          onClick={() => toggleTab('preview')}
        >
          Preview
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={view === 'code'}
          className={`site-tab ${view === 'code' ? 'active' : ''}`}
          onClick={() => toggleTab('code')}
        >
          Code
        </button>
      </div>

      {view === 'preview' && (
        <iframe
          srcDoc={html}
          sandbox="allow-scripts allow-forms"
          title="Generated site preview"
          className="site-preview-frame"
        />
      )}

      {view === 'code' && (
        <div className="site-code">
          <div className="site-code-header">
            <span className="site-code-name">index.html</span>
            <button type="button" className="site-code-copy-btn" onClick={copyHtml}>
              {copiedHtml ? 'Copied' : 'Copy'}
            </button>
          </div>
          <pre className="site-code-block">
            <code className="language-html" dangerouslySetInnerHTML={{ __html: highlightedHtml }} />
          </pre>
        </div>
      )}

      <div className="site-publish">
        {publishedUrl ? (
          <div className="site-published">
            <a href={fullUrl} target="_blank" rel="noopener noreferrer" className="site-url">
              {fullUrl}
            </a>
            <button type="button" className="site-code-copy-btn" onClick={copyUrl}>Copy link</button>
          </div>
        ) : (
          <button type="button" className="publish-button" onClick={onPublish} disabled={publishing}>
            {publishing ? 'Publishing...' : 'Publish'}
          </button>
        )}
      </div>

      {publishError && (
        <div className="message-error">
          <span>{publishError}</span>
        </div>
      )}
    </div>
  )
}

function TextareaInput({ value, onChange, onSend, disabled, placeholder, inputRef }) {
  const textareaRef = useRef(null)

  const setRefs = useCallback((node) => {
    textareaRef.current = node
    if (inputRef) inputRef.current = node
  }, [inputRef])

  useEffect(() => {
    textareaRef.current?.focus()
  }, [])

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
      ref={setRefs}
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

function WelcomeScreen({ onPick }) {
  return (
    <div className="welcome">
      <div className="welcome-icon" aria-hidden="true">🤖</div>
      <h2 className="welcome-title">Hi, I'm your AI assistant</h2>
      <p className="welcome-subtitle">Ask me anything — code, concepts, debugging, writing.</p>
      <div className="suggestion-chips">
        {SUGGESTED_PROMPTS.map((prompt) => (
          <button key={prompt} type="button" className="suggestion-chip" onClick={() => onPick(prompt)}>
            {prompt}
          </button>
        ))}
      </div>
    </div>
  )
}

function AuthScreen({ onAuth }) {
  const [mode, setMode] = useState('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const switchMode = (next) => {
    setMode(next)
    setError('')
  }

  const submit = async (e) => {
    e.preventDefault()
    if (submitting) return
    setError('')
    if (!email.trim() || !password) {
      setError('Please enter your email and password')
      return
    }
    setSubmitting(true)
    try {
      const res = await fetch(`${API_URL}/auth/${mode}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: email.trim(), password })
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || 'Something went wrong')
      onAuth({ token: data.token, email: data.email })
    } catch (err) {
      setError(err.message || 'Something went wrong')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="auth-screen" data-theme="dark">
      <div className="auth-card">
        <div className="auth-icon" aria-hidden="true">🤖</div>
        <h2 className="auth-title">{mode === 'login' ? 'Welcome back' : 'Create an account'}</h2>
        <p className="auth-subtitle">
          {mode === 'login'
            ? 'Log in to pick up your conversations and published sites.'
            : 'Your conversations and sites are stored per account.'}
        </p>
        <form className="auth-form" onSubmit={submit}>
          <label className="auth-field">
            <span>Email</span>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
              autoComplete="email"
              required
            />
          </label>
          <label className="auth-field">
            <span>Password</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={mode === 'register' ? 'At least 8 characters' : 'Your password'}
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              required
              minLength={mode === 'register' ? 8 : undefined}
            />
          </label>
          {error && <p className="auth-error" role="alert">{error}</p>}
          <button type="submit" className="auth-submit" disabled={submitting}>
            {submitting ? 'Please wait…' : mode === 'login' ? 'Log in' : 'Sign up'}
          </button>
        </form>
        <button type="button" className="auth-switch" onClick={() => switchMode(mode === 'login' ? 'register' : 'login')}>
          {mode === 'login' ? "Don't have an account? Sign up" : 'Already have an account? Log in'}
        </button>
      </div>
    </div>
  )
}

function App() {
  const [history, setHistory] = useState({ conversations: [], activeId: null })
  const conversations = history.conversations
  const activeId = history.activeId
  const [session, setSession] = useState(() => loadSession())
  const activeConversation = useMemo(() => conversations.find(c => c.id === activeId), [conversations, activeId])
  const messages = useMemo(() => activeConversation?.messages ?? [], [activeConversation])
  // Live mirror of the list so event callbacks read the freshest data without
  // clutching onto stale closures.
  const conversationsRef = useRef(conversations)
  conversationsRef.current = conversations
  const [input, setInput] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [globalError, setGlobalError] = useState('')
  const [theme, setTheme] = useState(() => loadTheme())
  const [isOnline, setIsOnline] = useState(() => navigator.onLine)
  const [toast, setToast] = useState('')
  const [nearBottom, setNearBottom] = useState(true)
  const [editing, setEditing] = useState(null)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [renamingId, setRenamingId] = useState(null)
  const [renameDraft, setRenameDraft] = useState('')
  const [confirmDeleteId, setConfirmDeleteId] = useState(null)
  const [sitesOpen, setSitesOpen] = useState(false)
  const [mySites, setMySites] = useState([])
  const [sitesLoading, setSitesLoading] = useState(false)
  const [sitesError, setSitesError] = useState('')
  const [deletingSiteId, setDeletingSiteId] = useState(null)
  const messagesRef = useRef(null)
  const messagesEndRef = useRef(null)
  const inputRef = useRef(null)
  const abortControllerRef = useRef(null)
  const nearBottomRef = useRef(true)
  const toastTimeoutRef = useRef(null)
  const confirmDeleteTimeoutRef = useRef(null)

  // Clear pending timeouts on unmount so nothing lingers after the page goes.
  useEffect(() => {
    return () => {
      if (toastTimeoutRef.current) clearTimeout(toastTimeoutRef.current)
      if (confirmDeleteTimeoutRef.current) clearTimeout(confirmDeleteTimeoutRef.current)
    }
  }, [])

  const handleAuth = useCallback((next) => {
    setSession(next)
    saveSession(next)
  }, [])

  const handleLogout = useCallback(async () => {
    try {
      if (session?.token) {
        await fetch(`${API_URL}/auth/logout`, {
          method: 'POST',
          headers: { 'Authorization': `Bearer ${session.token}` }
        }).catch(() => null)
      }
    } finally {
      setSession(null)
      saveSession(null)
      setHistory({ conversations: [], activeId: null })
      setMySites([])
    }
  }, [session])

  // Every authenticated call goes through this: it attaches the session Bearer
  // token and, on a 401, drops the (expired/revoked) session so the auth screen
  // reappears instead of a broken half-logged-in state.
  const authedFetch = useCallback(async (path, init = {}) => {
    const headers = { 'Content-Type': 'application/json', ...(init.headers || {}) }
    if (session?.token) headers['Authorization'] = `Bearer ${session.token}`
    let res
    try {
      res = await fetch(`${API_URL}${path}`, { ...init, headers })
    } catch {
      throw new Error('Could not reach the server')
    }
    if (res.status === 401 && session?.token) {
      handleLogout()
      throw new Error('Session expired — please log in again')
    }
    return res
  }, [session, handleLogout])

  const refreshConversations = useCallback(async () => {
    if (!session?.token) return
    try {
      const res = await authedFetch('/conversations')
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || 'Failed to load conversations')
      const normalized = (data.conversations || []).map(c => ({
        id: c.id,
        serverId: c.id,
        title: c.title || '',
        createdAt: parseIso(c.created_at),
        updatedAt: parseIso(c.updated_at),
        loaded: false,
        messages: []
      }))
      setHistory(prev => {
        const activeStillExists = prev.activeId !== null && normalized.some(c => c.id === prev.activeId)
        return { conversations: normalized, activeId: activeStillExists ? prev.activeId : null }
      })
    } catch (err) {
      setGlobalError(err.message || 'Failed to load conversations')
    }
  }, [session, authedFetch])

  // Fetch the persisted messages of a server-side conversation on first open.
  // The `loaded` flag keeps already-in-memory conversations (e.g. one the user
  // is still streaming into) from being refetched and clobbered.
  const loadConversationMessages = useCallback(async (conversationId) => {
    const res = await authedFetch(`/conversations/${conversationId}/messages`)
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data.detail || 'Failed to load messages')
    const mapped = (data.messages || []).map(m => ({
      ...m,
      timestamp: parseIso(m.created_at),
      html: m.role === 'assistant' ? extractHtmlBlock(m.content || '') : null,
      publishedUrl: m.published_url || null
    }))
    setHistory(prev => ({
      ...prev,
      conversations: prev.conversations.map(c => c.id === conversationId
        ? { ...c, messages: mapped, loaded: true, updatedAt: Math.max(c.updatedAt, parseIso(data.conversation?.updated_at)) }
        : c)
    }))
  }, [authedFetch])

  // Load the conversation list at the start of every session (login or reload).
  useEffect(() => {
    if (session) refreshConversations()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.token])

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    localStorage.setItem(THEME_KEY, theme)
  }, [theme])

  useEffect(() => {
    const handleOnline = () => setIsOnline(true)
    const handleOffline = () => setIsOnline(false)
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

  // Track whether the user is scrolled near the bottom of the message pane.
  // Auto-scroll only while they are, so reading an older reply mid-stream no
  // longer yanks the viewport back down. A ref mirrors the state so the
  // scroll-to-bottom effect reads the freshest value without re-subscribing.
  const updateNearBottom = useCallback(() => {
    const el = messagesRef.current
    if (!el) return
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    nearBottomRef.current = near
    setNearBottom(near)
  }, [])

  useEffect(() => {
    const el = messagesRef.current
    if (!el) return
    el.addEventListener('scroll', updateNearBottom, { passive: true })
    window.addEventListener('resize', updateNearBottom)
    return () => {
      el.removeEventListener('scroll', updateNearBottom)
      window.removeEventListener('resize', updateNearBottom)
    }
  }, [messages.length, updateNearBottom])

  useEffect(() => {
    if (nearBottomRef.current) {
      scrollToBottom()
    }
  }, [messages, scrollToBottom])

  const handleScrollDown = useCallback(() => {
    nearBottomRef.current = true
    setNearBottom(true)
    scrollToBottom()
  }, [scrollToBottom])

  const showToast = useCallback((msg) => {
    if (toastTimeoutRef.current) clearTimeout(toastTimeoutRef.current)
    setToast(msg)
    toastTimeoutRef.current = setTimeout(() => setToast(''), 2000)
  }, [])

  const handleCopy = useCallback(() => {
    showToast('Copied to clipboard')
  }, [showToast])

  // History accessors. All message mutations go through updateMessages so the
  // conversation's updatedAt (sidebar sort order) and auto-title stay current.
  const setConversations = useCallback((updater) => {
    setHistory(prev => ({ ...prev, conversations: typeof updater === 'function' ? updater(prev.conversations) : updater }))
  }, [])

  const setActiveId = useCallback((id) => {
    setHistory(prev => ({ ...prev, activeId: id }))
  }, [])

  const updateMessages = useCallback((targetId, updater) => {
    if (!targetId) return
    setConversations(prev => prev.map(c => {
      if (c.id !== targetId) return c
      const nextMessages = typeof updater === 'function' ? updater(c.messages) : updater
      return { ...c, messages: nextMessages, title: c.title || deriveTitle(nextMessages), updatedAt: Date.now() }
    }))
  }, [setConversations])

  // sendConversation is the single path that starts a chat request. It accepts
  // the target conversation id (so streaming keeps landing in the conversation
  // that started it even if the user switches away mid-stream) plus the full
  // message list (already including the new user message) and owns the fetch,
  // streaming, and error handling. Both the form submit handler and the retry
  // handler call it with the exact message list they want to send — this avoids
  // the previous retry bug where setInput(...) then handleSend() relied on
  // React state having flushed within the same tick.
  // sendConversation is the single path that starts a chat request. It accepts
  // the target conversation's local id plus its full message list and, for an
  // already-persisted conversation, its server id. A brand-new chat is created
  // on the backend before the stream starts so it is owned by the account and
  // its messages persist on the server (no localStorage).
  const sendConversation = useCallback(async (targetId, conversation, serverId, pendingTitle) => {
    setIsLoading(true)
    setGlobalError('')

    abortControllerRef.current = new AbortController()
    const timeoutId = setTimeout(() => {
      abortControllerRef.current?.abort()
    }, 120000)

    try {
      let conversationId = serverId || null
      if (!conversationId) {
        const created = await authedFetch('/conversations', {
          method: 'POST',
          body: JSON.stringify({ title: pendingTitle || deriveTitle(conversation) })
        })
        const createdData = await created.json().catch(() => ({}))
        if (!created.ok) throw new Error(createdData.detail || 'Failed to start conversation')
        conversationId = createdData.id
        setConversations(prev => prev.map(c => c.id === targetId
          ? { ...c, serverId: conversationId, title: createdData.title || c.title }
          : c))
      }

      updateMessages(targetId, conversation)

      const response = await authedFetch('/chat/stream', {
        method: 'POST',
        body: JSON.stringify({ messages: conversation, conversation_id: conversationId, stream: true }),
        signal: abortControllerRef.current.signal
      })

      clearTimeout(timeoutId)

      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        if (response.status === 429 && data.retry_after) {
          throw new Error(`Too many requests — try again in ${Math.ceil(data.retry_after / 60)} min.`)
        }
        throw new Error(data.detail || 'Failed to get response')
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let assistantMessage = ''
      let assistantMessageId = null
      const assistantTimestamp = Date.now()

      updateMessages(targetId, prev => [...prev, { role: 'assistant', content: '', timestamp: assistantTimestamp, streaming: true }])

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
                  updateMessages(targetId, prev => {
                    const updated = [...prev]
                    if (updated.length > 0 && updated[updated.length - 1].role === 'assistant') {
                      updated[updated.length - 1] = { ...updated[updated.length - 1], thinking: true }
                    }
                    return updated
                  })
                }
                if (parsed.content) {
                  assistantMessage += parsed.content
                  updateMessages(targetId, prev => {
                    const updated = [...prev]
                    if (updated.length > 0 && updated[updated.length - 1].role === 'assistant') {
                      updated[updated.length - 1] = { role: 'assistant', content: assistantMessage, timestamp: assistantTimestamp, streaming: true, thinking: false }
                    }
                    return updated
                  })
                }
                if (parsed.assistant_message_id) {
                  assistantMessageId = parsed.assistant_message_id
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

      // Strip any leaked reasoning preamble from the live (current) response
      // before it is finalized and rendered to the user.
      const finalContent = cleanReasoningLeak(assistantMessage)
      updateMessages(targetId, prev => {
        const updated = [...prev]
        if (updated.length > 0 && updated[updated.length - 1].role === 'assistant') {
          updated[updated.length - 1] = {
            role: 'assistant',
            content: finalContent,
            timestamp: assistantTimestamp,
            streaming: false,
            thinking: false,
            failed: false,
            html: extractHtmlBlock(finalContent),
            // The persisted server message id (sent with the stream) lets a
            // later Publish store the URL on the exact stored message.
            messageId: assistantMessageId
          }
        }
        return updated
      })
    } catch (err) {
      clearTimeout(timeoutId)
      if (err.name === 'AbortError') {
        setGlobalError('Request timed out')
        updateMessages(targetId, prev => {
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
      updateMessages(targetId, prev => {
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
      // Refocus the input so the user can keep typing right after a request
      // finishes (success or failure) without reaching for the mouse.
      requestAnimationFrame(() => inputRef.current?.focus())
    }
  }, [updateMessages, authedFetch, setConversations])

  const handleSend = async (e) => {
    e?.preventDefault()
    if (!input.trim() || isLoading || !isOnline || !session) return

    const userMessage = input.trim()
    const timestamp = Date.now()
    let targetId = activeConversation?.id || null
    let serverId = activeConversation?.serverId || null
    const pendingTitle = activeConversation?.title || ''
    let conversation
    if (targetId) {
      conversation = [...messages, { role: 'user', content: userMessage, timestamp }]
    } else {
      // No active conversation yet (fresh history): create it client-side only;
      // the backend row is created on the first send so an unsent empty chat
      // never leaves garbage on the server.
      targetId = makeId()
      const now = Date.now()
      conversation = [{ role: 'user', content: userMessage, timestamp }]
      setConversations(prev => [{ id: targetId, serverId: null, title: '', messages: conversation, createdAt: now, updatedAt: now, loaded: true }, ...prev])
      setActiveId(targetId)
    }
    setInput('')
    setEditing(null)
    await sendConversation(targetId, conversation, serverId, pendingTitle)
  }

  const handleRetry = useCallback((index) => {
    if (isLoading || !activeConversation) return
    const msg = messages[index]
    if (!msg || msg.role !== 'assistant' || !msg.failed) return

    const userMsgIndex = messages.findLastIndex((m, i) => i < index && m.role === 'user')
    if (userMsgIndex === -1) return

    // Re-send the exact stored user message content by replicating the message
    // list up to and including that user message — no dependence on the input
    // state, which fixes the previous stale-closure retry bug.
    const conversationUpToUser = messages.slice(0, userMsgIndex + 1)
    sendConversation(activeConversation.id, conversationUpToUser, activeConversation.serverId)
  }, [messages, activeConversation, isLoading, sendConversation])

  const handleDismissError = useCallback((index) => {
    if (!activeId) return
    updateMessages(activeId, prev => prev.map((m, i) => i === index ? { ...m, error: null } : m))
  }, [activeId, updateMessages])

  // Publish the extracted HTML of one assistant message to the backend /sites
  // endpoint. Each publish creates a fresh id (previous versions stay reachable),
  // and the returned URL is stored back on the message so it survives page
  // reloads alongside the rest of the chat history in localStorage.
  const publishSite = useCallback(async (index) => {
    const msg = messages[index]
    if (!msg || msg.role !== 'assistant' || !activeId || !session) return
    const htmlBlock = msg.html ?? extractHtmlBlock(msg.content || '')
    if (!htmlBlock || msg.publishing || msg.publishedUrl) return

    updateMessages(activeId, prev => prev.map((m, i) => i === index
      ? { ...m, publishing: true, publishError: null }
      : m))
    try {
      // The server persists the published URL back onto the stored message
      // (via message_id) so the link survives a reload alongside the history.
      const res = await authedFetch('/sites', {
        method: 'POST',
        body: JSON.stringify({ html: htmlBlock, message_id: msg.messageId || msg.id || undefined })
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        if (res.status === 429 && data.retry_after) {
          throw new Error(`Too many publishes — try again in ${Math.ceil(data.retry_after / 60)} min.`)
        }
        throw new Error(data.detail || `Failed to publish (${res.status})`)
      }
      updateMessages(activeId, prev => prev.map((m, i) => i === index
        ? { ...m, publishing: false, publishedUrl: data.url, publishError: null, html: htmlBlock }
        : m))
    } catch (err) {
      updateMessages(activeId, prev => prev.map((m, i) => i === index
        ? { ...m, publishing: false, publishError: err.message || 'Failed to publish' }
        : m))
    }
  }, [messages, activeId, updateMessages, authedFetch, session])

  // "My Sites" management panel: lists every site THIS publisher owns (newest
  // first) and lets them unpublish one. Deleting is optimistic — the row
  // disappears immediately and is rolled back if the backend rejects it.
  const refreshMySites = useCallback(async () => {
    setSitesLoading(true)
    setSitesError('')
    try {
      const res = await authedFetch('/sites')
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || `Failed to load sites (${res.status})`)
      setMySites(data.sites || [])
    } catch (err) {
      setSitesError(err.message || 'Failed to load sites')
    } finally {
      setSitesLoading(false)
    }
  }, [authedFetch])

  const toggleMySites = useCallback(() => {
    setSitesOpen(open => {
      if (!open) refreshMySites()
      return !open
    })
  }, [refreshMySites])

  const deleteSite = useCallback(async (site) => {
    if (deletingSiteId) return
    setDeletingSiteId(site.id)
    const previous = mySites
    setMySites(prev => prev.filter(s => s.id !== site.id))
    // Unpublish: also drop the "Publish" affordance off any chat message whose
    // stored URL pointed at this site so the UI matches backend state.
    const siteUrl = site.url
    setHistory(prev => ({
      ...prev,
      conversations: prev.conversations.map(c => ({
        ...c,
        messages: c.messages.map(m =>
          m.publishedUrl === siteUrl ? { ...m, publishedUrl: null } : m
        )
      }))
    }))
    try {
      const res = await authedFetch(site.url, { method: 'DELETE' })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to delete (${res.status})`)
      }
    } catch (err) {
      setMySites(previous)
      setSitesError(err.message || 'Failed to delete site')
    } finally {
      setDeletingSiteId(null)
    }
  }, [deletingSiteId, mySites, authedFetch, setHistory])

  // Create a fresh empty conversation and switch to it. This replaces the old
  // "Clear chat" button — previous conversations stay usable in the sidebar.
  const handleNewChat = useCallback(() => {
    const id = makeId()
    const now = Date.now()
    setConversations(prev => [{ id, serverId: null, title: '', messages: [], createdAt: now, updatedAt: now, loaded: true }, ...prev])
    setActiveId(id)
    setRenamingId(null)
    setEditing(null)
    setConfirmDeleteId(null)
    setInput('')
    setGlobalError('')
    setSidebarOpen(false)
  }, [setConversations, setActiveId])

  // Switching to a server conversation loads its stored messages the first time
  // (the `loaded` flag keeps this at one fetch even across back-and-forth).
  const handleSelectConversation = useCallback((id) => {
    if (id === activeId) return
    setActiveId(id)
    setEditing(null)
    setConfirmDeleteId(null)
    setSidebarOpen(false)
    const target = conversationsRef.current.find(c => c.id === id)
    if (target?.serverId && !target.loaded) {
      loadConversationMessages(id).catch(err => setGlobalError(err.message || 'Failed to load messages'))
    }
  }, [activeId, loadConversationMessages, setActiveId])

  const handleStartRename = useCallback((c) => {
    setRenamingId(c.id)
    setRenameDraft(c.title || '')
  }, [])

  const handleCommitRename = useCallback(async (id) => {
    if (renamingId !== id) return
    const title = renameDraft.trim()
    if (!title) {
      setRenamingId(null)
      setRenameDraft('')
      return
    }
    const target = conversationsRef.current.find(c => c.id === id)
    setConversations(prev => prev.map(c => c.id === id ? { ...c, title } : c))
    setRenamingId(null)
    setRenameDraft('')
    // Persist renames on the server when the conversation exists there; a
    // pending (not yet sent) chat is renamed locally only.
    if (target?.serverId) {
      authedFetch(`/conversations/${target.serverId}`, {
        method: 'PATCH',
        body: JSON.stringify({ title })
      }).catch(err => setGlobalError(err.message || 'Failed to rename conversation'))
    }
  }, [renamingId, renameDraft, authedFetch, setConversations])

  // Two-click delete: first click arms a 3s "Sure?" state per conversation, a
  // second click within that window removes it — history loss is never a single
  // misclick away, matching the old clear-chat confirm behavior.
  const handleDeleteConversation = useCallback((id) => {
    if (confirmDeleteId !== id) {
      setConfirmDeleteId(id)
      if (confirmDeleteTimeoutRef.current) clearTimeout(confirmDeleteTimeoutRef.current)
      confirmDeleteTimeoutRef.current = setTimeout(() => setConfirmDeleteId(null), 3000)
      return
    }
    if (confirmDeleteTimeoutRef.current) {
      clearTimeout(confirmDeleteTimeoutRef.current)
      confirmDeleteTimeoutRef.current = null
    }
    setConfirmDeleteId(null)
    const next = conversationsRef.current.filter(c => c.id !== id)
    const target = conversationsRef.current.find(c => c.id === id)
    setConversations(next)
    if (id === activeId) {
      setActiveId(next.length > 0 ? next[0].id : null)
    }
    setRenamingId(null)
    setEditing(null)
    setSidebarOpen(false)
    // Persist deletion server-side for real conversations; pending (unsent)
    // chats only ever existed locally, so nothing else to clean up.
    if (target?.serverId) {
      authedFetch(`/conversations/${target.serverId}`, { method: 'DELETE' })
        .catch(err => setGlobalError(err.message || 'Failed to delete conversation'))
    }
  }, [confirmDeleteId, activeId, authedFetch, setActiveId, setConversations])

  const handleSuggestionClick = useCallback((prompt) => {
    setInput(prompt)
    inputRef.current?.focus()
  }, [])

  // Edit-and-resend for the most recent user message: open an inline editor,
  // then truncate the conversation back to before that message and send the
  // edited content as the new tail — the same sendConversation() path handleRetry
  // uses.
  const startEdit = useCallback((editingIndex) => {
    const msg = messages[editingIndex]
    if (!msg || msg.role !== 'user') return
    setEditing({ index: editingIndex, draft: msg.content })
  }, [messages])

  const changeEditDraft = useCallback((draft) => {
    setEditing(prev => prev ? { ...prev, draft } : prev)
  }, [])

  const cancelEdit = useCallback(() => {
    setEditing(null)
  }, [])

  const resendEdit = useCallback(() => {
    if (!editing || !editing.draft.trim() || isLoading || !activeConversation) return
    const before = messages.slice(0, editing.index)
    const newMessages = [
      ...before,
      { role: 'user', content: editing.draft.trim(), timestamp: Date.now() }
    ]
    setEditing(null)
    setInput('')
    sendConversation(activeConversation.id, newMessages, activeConversation.serverId)
  }, [editing, messages, isLoading, sendConversation, activeConversation])

  const toggleTheme = useCallback(() => {
    setTheme(t => t === 'dark' ? 'light' : 'dark')
  }, [])

  // Sidebar shows conversations most-recently-active first.
  const sortedConversations = useMemo(() => {
    return [...conversations].sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0))
  }, [conversations])

  // Everything below needs a logged-in session: the auth screen is the gate.
  if (!session) {
    return <AuthScreen onAuth={handleAuth} />
  }

  return (
    <div className="app" data-theme={theme}>
      <header className="header">
        <div className="header-title-group">
          <button
            type="button"
            className={`sidebar-toggle ${sidebarOpen ? 'active' : ''}`}
            onClick={() => setSidebarOpen(o => !o)}
            aria-label="Toggle conversation list"
            aria-expanded={sidebarOpen}
          >
            ☰
          </button>
          <div className="header-content">
            <h1>
              AI Chatbot
              <span
                className={`status-dot ${isOnline ? 'online' : 'offline'}`}
                role="img"
                title={isOnline ? 'Online' : 'Offline'}
                aria-label={isOnline ? 'Online' : 'Offline'}
              />
            </h1>
            <p>Powered by hy3-free</p>
          </div>
        </div>
        <div className="header-actions">
          <div className="account-chip" title={`Signed in as ${session?.email}`}>
            <span className="account-email">{session?.email}</span>
          </div>
          <button type="button" className="logout-button" onClick={handleLogout} aria-label="Log out">
            Log out
          </button>
          <button type="button" className="theme-toggle" onClick={toggleTheme} aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}>
            {theme === 'dark' ? '☀️' : '🌙'}
          </button>
        </div>
      </header>

      <div className={`app-body ${sidebarOpen ? 'sidebar-open' : ''}`}>
        <div className="sidebar-backdrop" onClick={() => setSidebarOpen(false)} aria-hidden="true" />

        <nav className="sidebar" aria-label="Conversations">
          <button type="button" className="sidebar-new-chat" onClick={handleNewChat}>
            ＋ New chat
          </button>
          <div className="sidebar-list">
            {sortedConversations.length === 0 ? (
              <p className="sidebar-empty">No conversations yet</p>
            ) : (
              sortedConversations.map(c => {
                const isActive = c.id === activeId
                const isRenaming = renamingId === c.id
                const isConfirming = confirmDeleteId === c.id
                return (
                  <div key={c.id} className={`sidebar-conv ${isActive ? 'active' : ''}`}>
                    {isRenaming ? (
                      <input
                        type="text"
                        className="sidebar-rename-input"
                        value={renameDraft}
                        onChange={(e) => setRenameDraft(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') {
                            e.preventDefault()
                            handleCommitRename(c.id)
                          }
                          if (e.key === 'Escape') {
                            setRenamingId(null)
                            setRenameDraft('')
                          }
                        }}
                        onBlur={() => handleCommitRename(c.id)}
                        autoFocus
                        aria-label="Rename conversation"
                      />
                    ) : (
                      <button type="button" className="sidebar-conv-main" onClick={() => handleSelectConversation(c.id)}>
                        <span className="sidebar-conv-title">{c.title || 'New chat'}</span>
                        <span className="sidebar-conv-time">{formatRelativeTime(c.updatedAt)}</span>
                      </button>
                    )}
                    {!isRenaming && (
                      <div className="sidebar-conv-actions">
                        <button type="button" className="sidebar-icon-btn" onClick={() => handleStartRename(c)} aria-label="Rename conversation">✏️</button>
                        <button
                          type="button"
                          className={`sidebar-icon-btn delete ${isConfirming ? 'confirming' : ''}`}
                          onClick={() => handleDeleteConversation(c.id)}
                          aria-label={isConfirming ? 'Confirm delete conversation' : 'Delete conversation'}
                        >
                          {isConfirming ? 'Sure?' : '🗑️'}
                        </button>
                      </div>
                    )}
                  </div>
                )
              })
            )}
          </div>

          <div className="sidebar-sites">
            <button type="button" className="sidebar-sites-toggle" onClick={toggleMySites} aria-expanded={sitesOpen}>
              <span>My Sites</span>
              <span className="sidebar-sites-caret">{sitesOpen ? '▾' : '▸'}</span>
            </button>
            {sitesOpen && (
              <div className="sidebar-sites-body">
                {sitesLoading ? (
                  <p className="sidebar-empty">Loading published sites…</p>
                ) : sitesError ? (
                  <p className="sidebar-sites-error" role="alert">{sitesError}</p>
                ) : mySites.length === 0 ? (
                  <p className="sidebar-empty">No published sites yet</p>
                ) : (
                  <ul className="sidebar-sites-list">
                    {mySites.map(site => (
                      <li key={site.id} className="sidebar-sites-item">
                        <a
                          className="sidebar-sites-link"
                          href={`${API_URL}${site.url}`}
                          target="_blank"
                          rel="noreferrer"
                          title="Open published site"
                        >
                          <span className="sidebar-sites-title">{site.title || site.id}</span>
                          <span className="sidebar-sites-sub">{formatRelativeTime(parseIso(site.created_at))} · {(site.size_bytes / 1024).toFixed(1)} KB</span>
                        </a>
                        <button
                          type="button"
                          className="sidebar-icon-btn delete"
                          disabled={deletingSiteId === site.id}
                          onClick={() => deleteSite(site)}
                          aria-label={`Delete site ${site.id}`}
                        >
                          🗑️
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>
        </nav>

        <main className="chat-container">
          {!isOnline && (
            <div className="offline-banner" role="alert">
              📴 You're offline — messages will send when connection restores
            </div>
          )}

          {messages.length === 0 ? (
            <WelcomeScreen onPick={handleSuggestionClick} />
          ) : (
            <div className="messages" role="log" aria-live="polite" ref={messagesRef}>
              {messages.map((msg, index) => {
                const isEditable = msg.role === 'user' && index === messages.length - 1 && !isLoading
                return (
                  <div key={index} className={`message ${msg.role} ${msg.streaming ? 'streaming' : ''} ${msg.failed ? 'failed' : ''}`}>
                    <MessageBubble
                      message={msg}
                      isStreaming={msg.streaming}
                      isEditable={isEditable}
                      isEditing={editing?.index === index}
                      editDraft={editing?.index === index ? editing.draft : ''}
                      onCopy={handleCopy}
                      onRetry={() => handleRetry(index)}
                      onErrorDismiss={() => handleDismissError(index)}
                      onEdit={() => startEdit(index)}
                      onEditDraftChange={changeEditDraft}
                      onResendEdit={resendEdit}
                      onCancelEdit={cancelEdit}
                      onPublish={() => publishSite(index)}
                    />
                  </div>
                )
              })}
              <div ref={messagesEndRef} />
            </div>
          )}

          {messages.length > 0 && !nearBottom && (
            <button type="button" className="scroll-bottom-btn" onClick={handleScrollDown} aria-label="Scroll to newest messages">
              ↓ New messages
            </button>
          )}

          {globalError && !isLoading && (
            <div className="error-banner" role="alert">
              {globalError}
            </div>
          )}

          {toast && (
            <div className="toast" role="status">
              {toast}
            </div>
          )}

          <form className="input-form" onSubmit={handleSend}>
            <TextareaInput
              value={input}
              onChange={setInput}
              onSend={handleSend}
              disabled={isLoading || !isOnline}
              inputRef={inputRef}
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
        </main>
      </div>
    </div>
  )
}

export default App