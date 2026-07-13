import Link from "next/link";
import type { ReactNode } from "react";

import { referenceNavigation, roleNavigation } from "./navigation";

export type TocItem = {
  id: string;
  label: string;
};

export function MobileToc({ toc }: { toc: TocItem[] }) {
  if (toc.length === 0) return null;
  return (
    <details className="mobile-toc">
      <summary>On this page</summary>
      <nav aria-label="Mobile table of contents">
        {toc.map((item) => (
          <a href={`#${item.id}`} key={item.id}>
            {item.label}
          </a>
        ))}
      </nav>
    </details>
  );
}

type DocsShellProps = {
  activePath: string;
  toc?: TocItem[];
  children: ReactNode;
};

function NavigationLinks({ activePath }: { activePath: string }) {
  return (
    <>
      <div className="nav-section">
        <p>Overview</p>
        <Link href="/" aria-current={activePath === "/" ? "page" : undefined}>
          Start here
        </Link>
      </div>
      <div className="nav-section">
        <p>Role guides</p>
        {roleNavigation.map((item) => (
          <Link
            href={item.href}
            key={item.href}
            aria-current={activePath === item.href ? "page" : undefined}
          >
            {item.label}
          </Link>
        ))}
      </div>
      <div className="nav-section">
        <p>Reference</p>
        {referenceNavigation.map((item) => (
          <Link
            href={item.href}
            key={item.href}
            aria-current={activePath === item.href ? "page" : undefined}
          >
            {item.label}
          </Link>
        ))}
      </div>
    </>
  );
}

export function DocsShell({ activePath, toc = [], children }: DocsShellProps) {
  return (
    <div className="wiki-shell">
      <a className="skip-link" href="#main-content">Skip to content</a>
      <aside className="wiki-sidebar" aria-label="Documentation navigation">
        <Link className="docs-brand" href="/">
          <strong>PDF Bridge</strong>
          <span>Documentation</span>
        </Link>
        <nav>
          <NavigationLinks activePath={activePath} />
        </nav>
        <p className="sidebar-scope">Internal proof-of-concept documentation</p>
      </aside>

      <header className="mobile-header">
        <Link href="/">PDF Bridge documentation</Link>
        <details>
          <summary>Menu</summary>
          <nav aria-label="Mobile documentation navigation">
            <NavigationLinks activePath={activePath} />
          </nav>
        </details>
      </header>

      <div className="wiki-content-grid">
        <main className="article-column" id="main-content">{children}</main>
        {toc.length > 0 && (
          <aside className="toc" aria-label="On this page">
            <p>On this page</p>
            {toc.map((item) => (
              <a href={`#${item.id}`} key={item.id}>
                {item.label}
              </a>
            ))}
          </aside>
        )}
      </div>
    </div>
  );
}
