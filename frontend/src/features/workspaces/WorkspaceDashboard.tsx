import { useEffect, useRef, useState } from 'react'
import { Navigate, useLocation } from 'react-router'
import { Brand } from '../auth/AuthLayout'
import { useAuth } from '../auth/context'
import { CreateWorkspaceForm } from './CreateWorkspaceForm'
import { useWorkspaces } from './useWorkspaces'
import type { Workspace } from '../../api/client'
import './dashboard.css'
import { DocumentLibrary } from '../documents/DocumentLibrary'

const permissions = {
  owner: { title: 'Ownership & access', description: 'You own this workspace. Your role permits reading and editing document records and managing membership.', label: 'Full workspace access' },
  editor: { title: 'Editor permissions', description: 'You can read and edit document records in this workspace. Membership management belongs to its owner.', label: 'Read and edit access' },
  viewer: { title: 'Viewer permissions', description: 'You can read document records in this workspace. Editing and membership management require another role.', label: 'Read-only access' },
}

function dateLabel(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? 'Date unavailable' : date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

function RoleBadge({ role }: { role: Workspace['role'] }) {
  return <span className={`role-badge role-${role}`}>{role}</span>
}

function DashboardContent() {
  const { user, logout } = useAuth()
  const { hash } = useLocation()
  const data = useWorkspaces()
  const [menuOpen, setMenuOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [logoutError, setLogoutError] = useState<string | null>(null)
  const [signingOut, setSigningOut] = useState(false)
  const createButton = useRef<HTMLButtonElement>(null)
  const menuButton = useRef<HTMLButtonElement>(null)
  const sidebar = useRef<HTMLElement>(null)
  const wasCreating = useRef(false)
  useEffect(() => {
    if (wasCreating.current && !creating) createButton.current?.focus()
    wasCreating.current = creating
  }, [creating])
  useEffect(() => {
    if (menuOpen) sidebar.current?.querySelector<HTMLElement>('a.nav-item')?.focus()
  }, [menuOpen])

  function closeMenu(returnFocus = true) {
    setMenuOpen(false)
    if (menuOpen && returnFocus) requestAnimationFrame(() => menuButton.current?.focus())
  }
  const access = data.active ? permissions[data.active.role] : null
  const currentSection = hash || '#overview'
  const navigation = (target: string) => ({
    className: `nav-item ${currentSection === target ? 'nav-current' : ''}`,
    'aria-current': currentSection === target ? 'location' as const : undefined,
    href: target,
  })

  async function signOut() {
    setSigningOut(true)
    setLogoutError(null)
    try { await logout() } catch (error) {
      setLogoutError(error instanceof Error ? error.message : 'Could not sign out.')
    } finally { setSigningOut(false) }
  }

  function created(workspace: Workspace) {
    setCreating(false)
    setNotice(`Workspace “${workspace.name}” created. You’re its owner.`)
    data.select(workspace.id)
    data.reload()
  }

  const pickerItems = data.active && !data.items.some((item) => item.id === data.active?.id)
    ? [data.active, ...data.items] : data.items

  return <div className="dashboard-shell">
    <a className="skip-link" href="#dashboard-content">Skip to content</a>
    <aside ref={sidebar} className={`dashboard-sidebar ${menuOpen ? 'menu-open' : ''}`} id="dashboard-navigation" onKeyDown={(event) => { if (event.key === 'Escape' && menuOpen) { event.preventDefault(); closeMenu() } }}>
      <div className="sidebar-brand"><Brand /></div>
      <div className="sidebar-caption">YOUR WORKSPACE</div>
      <nav aria-label="Main navigation" onClick={(event) => {
        const link = event.target instanceof Element ? event.target.closest('a') : null
        closeMenu(false)
        if (link) requestAnimationFrame(() => {
          const target = document.getElementById(link.hash.slice(1))
          target?.setAttribute('tabindex', '-1')
          target?.focus()
        })
      }}>
        <a {...navigation('#overview')}><span aria-hidden="true">◫</span>Overview</a>
        <a {...navigation('#workspace-list')}><span aria-hidden="true">▦</span>Workspaces</a>
        {data.active && <a {...navigation('#document-library')}><span aria-hidden="true">▤</span>Documents</a>}
        {access && <a {...navigation('#workspace-access')}><span aria-hidden="true">◇</span>{access.title}</a>}
        <a {...navigation('#profile')}><span aria-hidden="true">○</span>My profile</a>
      </nav>
      <div className="sidebar-note"><span className="sidebar-note-dot" />A space for focused work.<p>Your workspaces and permissions come from your account.</p></div>
      <div className="sidebar-user"><span className="user-avatar">{user?.display_name.slice(0, 1).toUpperCase()}</span><div><strong>{user?.display_name}</strong><span>{user?.email}</span></div></div>
      <button type="button" className="sidebar-signout" onClick={signOut} disabled={signingOut}>{signingOut ? 'Signing out…' : 'Sign out'}<span aria-hidden="true">↗</span></button>
    </aside>

    <header className="dashboard-topnav">
      <button ref={menuButton} className="menu-button" type="button" aria-controls="dashboard-navigation" aria-expanded={menuOpen} onClick={() => setMenuOpen(!menuOpen)}>{menuOpen ? 'Close menu' : 'Menu'}</button>
      <div className="dashboard-breadcrumb">Workspace<span>/</span><strong>Overview</strong></div>
      <div className="workspace-picker"><label htmlFor="active-workspace">Active workspace</label><select id="active-workspace" value={data.active?.id ?? ''} onChange={(event) => data.select(event.target.value)} disabled={!pickerItems.length}>
        <option value="" disabled>{data.loading ? 'Loading…' : 'Choose a workspace'}</option>
        {pickerItems.map((workspace) => <option key={workspace.id} value={workspace.id}>{workspace.name}</option>)}
      </select></div>
      <a className="topnav-profile user-avatar" href="#profile" aria-label="View your profile">{user?.display_name.slice(0, 1).toUpperCase()}</a>
    </header>

    <main className="dashboard-content" id="dashboard-content" tabIndex={-1}>
      <section className="dashboard-heading" id="overview"><div><span className="section-label">YOUR WORK, IN ONE PLACE</span><h1 tabIndex={-1}>Welcome, {user?.display_name}.</h1><p>Choose a workspace and make room for your next project.</p></div><button type="button" ref={createButton} className="primary-button" onClick={() => { setCreating(true); setNotice(null) }} disabled={creating}><span aria-hidden="true">＋</span>New workspace</button></section>
      {notice && <div className="dashboard-notice notice success" role="status"><span>{notice}</span><button type="button" aria-label="Dismiss success message" onClick={() => setNotice(null)}>×</button></div>}
      {logoutError && <div className="notice error" role="alert">{logoutError}</div>}
      {creating && <CreateWorkspaceForm onCreated={created} onCancel={() => setCreating(false)} />}

      <div className="dashboard-overview-grid">
        <section className="active-workspace-panel panel" aria-labelledby="active-title">
          <div className="panel-heading"><span className="section-label">CURRENT WORKSPACE</span>{data.active && <RoleBadge role={data.active.role} />}</div>
          {data.active ? <><div className="workspace-symbol" aria-hidden="true">{data.active.name.slice(0, 1).toUpperCase()}</div><h2 id="active-title">{data.active.name}</h2><p>{access?.label}. This is your selected workspace.</p><div className="workspace-meta"><span>Created {dateLabel(data.active.created_at)}</span><span className="real-data-label">Connected to your account</span></div></>
            : data.selectionError ? <div className="selection-error" role="alert"><h2 id="active-title">Workspace unavailable</h2><p>{data.selectionError}</p><button className="secondary-button" type="button" onClick={data.reload}>Retry access check</button></div>
              : <><h2 id="active-title">{data.loading || data.selectedId ? 'Finding your workspace' : 'A fresh start'}</h2><p role="status">{data.loading ? 'Loading your accessible workspaces…' : data.selectedId ? 'Checking workspace access…' : 'Create a workspace to organize your next project.'}</p></>}
        </section>
        <section className="profile-panel panel" id="profile" aria-labelledby="profile-title"><span className="section-label">YOUR PROFILE</span><div className="profile-title"><span className="user-avatar">{user?.display_name.slice(0, 1).toUpperCase()}</span><div><h2 id="profile-title">{user?.display_name}</h2><p>Signed in securely</p></div></div><dl><dt>Email address</dt><dd>{user?.email}</dd><dt>Member since</dt><dd>{user && dateLabel(user.created_at)}</dd><dt>Current role</dt><dd>{data.active ? <RoleBadge role={data.active.role} /> : 'Select a workspace'}</dd></dl></section>
      </div>

      <section className="workspace-list-section" id="workspace-list" aria-labelledby="workspace-list-title"><div className="section-heading"><div><h2 id="workspace-list-title">Your workspaces</h2><p>Only spaces you own or belong to appear here.</p></div><button type="button" className="refresh-button" onClick={data.reload} disabled={data.loading}>Refresh</button></div>
        {data.error && <div className="notice error" role="alert">{data.error}<button className="inline-button" type="button" onClick={data.reload}>Try again</button></div>}
        {data.loading ? <><p className="list-loading" role="status">Loading workspaces…</p><div className="workspace-grid" aria-hidden="true"><div className="workspace-skeleton" /><div className="workspace-skeleton" /></div></>
          : !data.error && !data.items.length ? <div className="empty-workspaces panel"><span className="empty-symbol" aria-hidden="true">▦</span><h3>No workspaces yet</h3><p>Create a workspace to get started. You’ll own it and control its access.</p><button className="secondary-button" type="button" onClick={() => setCreating(true)} disabled={creating}>Create your first workspace</button></div>
            : <div className="workspace-grid">{data.items.map((workspace) => <article className={`workspace-card panel ${data.active?.id === workspace.id ? 'workspace-card-active' : ''}`} key={workspace.id}>
              <div className="workspace-card-top"><span className="card-symbol" aria-hidden="true">{workspace.name.slice(0, 1).toUpperCase()}</span><RoleBadge role={workspace.role} /></div><h3>{workspace.name}</h3><p>Created {dateLabel(workspace.created_at)}</p><button type="button" className="workspace-card-bottom" onClick={() => data.select(workspace.id)} aria-pressed={data.active?.id === workspace.id}><span>Select {workspace.name}</span><span aria-hidden="true">↗</span></button>
            </article>)}</div>}
        {!data.loading && data.hasMore && <button type="button" className="secondary-button load-more" disabled={data.loadingMore} onClick={data.loadMore}>{data.loadingMore ? 'Loading more…' : 'Load more workspaces'}</button>}
      </section>

      {data.active && access && <section className="permissions-panel panel" id="workspace-access" aria-labelledby="permissions-title"><span className="permission-symbol" aria-hidden="true">◇</span><div><h2 id="permissions-title">{access.title}</h2><p>{access.description}</p></div><RoleBadge role={data.active.role} /></section>}
      {data.active && data.selectionError && <div className="notice error" role="alert">{data.selectionError}<button className="inline-button" type="button" onClick={data.reload}>Retry access check</button></div>}
      {data.active && <DocumentLibrary key={`${data.active.id}:${data.active.role}`} workspace={data.active} checkingAccess={data.checkingAccess} onPermissionFailure={data.reload} />}
      <footer className="dashboard-footer"><span>AgentSphere AI</span><span>Your workspace. Your access.</span></footer>
    </main>
  </div>
}

export function WorkspaceDashboard() {
  const { status } = useAuth()
  if (status === 'loading') return <div className="page-loading" role="status">Checking your session…</div>
  if (status !== 'authenticated') return <Navigate to="/login" replace />
  return <DashboardContent />
}
