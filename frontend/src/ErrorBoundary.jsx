// One bad field name must not blank the whole dashboard.
//
// The Hiring contacts tab read `data.candidates` where the route returns `contact_candidates`.
// `undefined.length` threw during render, React unmounted the entire tree, and the operator got
// a **white page with nothing on it** -- no message, no nav, no way back except reloading onto a
// screen that would do it again.
//
// The typo is a one-line fix. The blank page is the defect worth building for: it turns any
// single mistake in any panel into a total outage of the tool, and it tells the person nothing
// they could report. The same shape as the 500 that arrived outside `CORSMiddleware` and made a
// duplicate key look like an unreachable backend -- a real fault, reported as something else
// entirely, with the useful detail thrown away.
//
// So a render error is caught at the screen boundary: the sidebar stays, the message names the
// screen and the error, and every other screen still works. A boundary is the **only** way to
// catch this -- a try/catch cannot reach inside React's render, which is why this is a class
// component, the one place this codebase needs one.

import React from 'react'

export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    // Logged as well as shown, because the stack says which component and the message does not.
    // This is the one thing an operator can paste into a report.
    console.error('A screen failed to render', error, info?.componentStack)
  }

  componentDidUpdate(previous) {
    // Leaving the broken screen clears the error. Without this the boundary latches: the
    // operator clicks another nav item, the state still holds an error, and the whole dashboard
    // stays dead until a reload -- which is most of the original problem back again.
    if (previous.resetKey !== this.props.resetKey && this.state.error) {
      this.setState({ error: null })
    }
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children

    return (
      <div className="card">
        <h1 className="page-title">This screen did not load</h1>
        <p className="page-note">
          Something on <strong>{this.props.label || 'this screen'}</strong> failed while drawing,
          so it has been stopped here rather than taking the rest of the dashboard with it. Every
          other screen still works — pick one from the sidebar.
        </p>
        <p className="hint">
          {error.name}: {error.message}
        </p>
        <p className="hint">
          If you report this, that line is the useful part. There is more detail in the browser
          console.
        </p>
        <button type="button" className="btn" onClick={() => this.setState({ error: null })}>
          Try again
        </button>
      </div>
    )
  }
}
