import type { ReactNode } from 'react'
import { Link } from 'react-router'

export function Brand() {
  return <Link className="brand" to="/" aria-label="AgentSphere AI home">
    <span className="brand-mark" aria-hidden="true"><span /><span /><span /><span /></span>
    <span>agentsphere<span className="brand-ai">.ai</span></span>
  </Link>
}

export function AuthLayout({ children }: { children: ReactNode }) {
  return <div className="auth-shell">
    <aside className="story-panel">
      <div className="story-top"><Brand /><span className="story-version">WORKSPACE ACCESS / 01</span></div>
      <div className="story-main">
        <div className="eyebrow"><span className="eyebrow-line" /> YOUR RESEARCH, ORGANIZED</div>
        <h1>A clearer way<br />to <em>work together.</em></h1>
        <p>One secure place for your team, workspaces, and document records.</p>
        <div className="orbit-art" aria-hidden="true"><div className="orbit orbit-one" /><div className="orbit orbit-two" /><div className="orbit orbit-three" /><div className="orbit-core"><span className="brand-mark"><span /><span /><span /><span /></span></div><i className="orbit-dot dot-one" /><i className="orbit-dot dot-two" /><i className="orbit-dot dot-three" /></div>
      </div>
      <div className="story-bottom"><span>SECURE BY DESIGN</span><span>BUILT FOR FOCUSED WORK</span></div>
    </aside>
    <main className="auth-main">
      <div className="mobile-brand"><Brand /></div>
      <div className="auth-content">{children}</div>
      <footer className="auth-footer"><span>© 2026 AgentSphere AI</span><span>Private workspace access</span></footer>
    </main>
  </div>
}
