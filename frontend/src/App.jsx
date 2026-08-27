import { useState, useRef, useEffect } from 'react'
import './App.css'

function App() {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState('')
  const messagesEndRef = useRef(null)

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }

  useEffect(() => {
    scrollToBottom()
  }, [messages])

  const handleSend = async (e) => {
    e.preventDefault()
    if (!input.trim() || isLoading) return

    const userMessage = input.trim()
    setMessages(prev => [...prev, { role: 'user', content: userMessage }])
    setInput('')
    setIsLoading(true)
    setError('')

    try {
      const response = await fetch('http://localhost:8000/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: userMessage })
      })

      const data = await response.json()

      if (!response.ok) {
        throw new Error(data.detail || 'Failed to get response')
      }

      setMessages(prev => [...prev, { role: 'assistant', content: data.answer }])
    } catch (err) {
      setError(err.message)
      setMessages(prev => [...prev, { role: 'assistant', content: 'Sorry, something went wrong. Please try again.' }])
    } finally {
      setIsLoading(false)
    }
  }

  return (
    <div className="app">
      <header className="header">
        <h1>AI Chatbot</h1>
        <p>Powered by hy3-free</p>
      </header>
      
      <main className="chat-container">
        <div className="messages" role="log" aria-live="polite">
          {messages.map((msg, index) => (
            <div key={index} className={`message ${msg.role}`}>
              <div className="message-bubble">
                <span className="message-role">{msg.role === 'user' ? 'You' : 'AI'}</span>
                <p className="message-text">{msg.content}</p>
              </div>
            </div>
          ))}
          {isLoading && (
            <div className="message assistant loading">
              <div className="message-bubble">
                <span className="message-role">AI</span>
                <div className="loading-dots">
                  <span></span>
                  <span></span>
                  <span></span>
                </div>
              </div>
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>

        {error && (
          <div className="error-banner" role="alert">
            {error}
          </div>
        )}

        <form className="input-form" onSubmit={handleSend}>
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={isLoading ? 'Waiting for response...' : 'Type a message...'}
            disabled={isLoading}
            className="message-input"
            aria-label="Message input"
          />
          <button
            type="submit"
            disabled={isLoading || !input.trim()}
            className="send-button"
            aria-label="Send message"
          >
            {isLoading ? 'Sending...' : 'Send'}
          </button>
        </form>
      </main>
    </div>
  )
}

export default App