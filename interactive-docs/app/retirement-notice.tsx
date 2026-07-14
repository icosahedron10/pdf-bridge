export function RetirementNotice() {
  return (
    <>
      <header className="site-header">
        <strong>PDF Bridge</strong>
      </header>
      <main>
        <p className="status">Status: Retired</p>
        <h1>Interactive documentation has moved.</h1>
        <p className="lead">
          This site is no longer an authoritative source for PDF Bridge. It remains online only to
          direct readers to the current project documentation.
        </p>
        <dl className="source-list">
          <div>
            <dt>Architecture</dt>
            <dd>
              Use the repository <code>README</code> and <code>docs/</code> directory for current
              service behavior, interfaces, configuration, and operations.
            </dd>
          </div>
          <div>
            <dt>Operator facade</dt>
            <dd>
              Streamlit is the target operator facade for collection-based PDF stores, document
              inspection, uploads, and deletions.
            </dd>
          </div>
        </dl>
      </main>
    </>
  );
}
