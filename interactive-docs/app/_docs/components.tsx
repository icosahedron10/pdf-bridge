import Link from "next/link";
import type { ReactNode } from "react";

import { adjacentPages } from "./navigation";
import type { NavigationItem } from "./navigation";

type ArticleHeaderProps = {
  category: string;
  title: string;
  summary: string;
  facts?: Array<{ term: string; detail: string }>;
};

export function ArticleHeader({ category, title, summary, facts = [] }: ArticleHeaderProps) {
  return (
    <header className="article-header">
      <nav className="breadcrumbs" aria-label="Breadcrumb">
        <Link href="/">Docs</Link>
        <span aria-hidden="true">/</span>
        <span>{category}</span>
      </nav>
      <h1>{title}</h1>
      <p className="article-summary">{summary}</p>
      {facts.length > 0 && (
        <dl className="article-facts">
          {facts.map((fact) => (
            <div key={fact.term}>
              <dt>{fact.term}</dt>
              <dd>{fact.detail}</dd>
            </div>
          ))}
        </dl>
      )}
    </header>
  );
}

export function Callout({
  title,
  children,
  tone = "note",
}: {
  title: string;
  children: ReactNode;
  tone?: "note" | "warning";
}) {
  return (
    <aside className={`callout ${tone}`}>
      <strong>{title}</strong>
      <div>{children}</div>
    </aside>
  );
}

export function CodeBlock({ children }: { children: string }) {
  return (
    <pre className="code-block">
      <code>{children}</code>
    </pre>
  );
}

export function ModuleReferences({
  items,
}: {
  items: Array<{ path: string; purpose: string }>;
}) {
  return (
    <div className="module-list">
      {items.map((item) => (
        <div key={item.path}>
          <code>{item.path}</code>
          <span>{item.purpose}</span>
        </div>
      ))}
    </div>
  );
}

export function PageNavigation({ activePath }: { activePath: string }) {
  const { previous, next } = adjacentPages(activePath);
  return (
    <nav className="page-navigation" aria-label="Previous and next documentation pages">
      {previous ? (
        <Link href={previous.href}>
          <span>Previous</span>
          {previous.label}
        </Link>
      ) : (
        <span />
      )}
      {next && (
        <Link href={next.href} className="next-page">
          <span>Next</span>
          {next.label}
        </Link>
      )}
    </nav>
  );
}

export function RelatedGuides({ links }: { links: NavigationItem[] }) {
  if (links.length === 0) return null;
  return (
    <section className="related-guides" aria-labelledby="related-guides-title">
      <h2 id="related-guides-title">Related guides</h2>
      <div>
        {links.map((link) => (
          <Link href={link.href} key={link.href}>
            <strong>{link.label}</strong>
            <span>{link.description}</span>
          </Link>
        ))}
      </div>
    </section>
  );
}

export function DocumentationTable({
  headings,
  rows,
}: {
  headings: string[];
  rows: ReactNode[][];
}) {
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>{headings.map((heading) => <th key={heading}>{heading}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
