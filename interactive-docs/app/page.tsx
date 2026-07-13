import Link from "next/link";

import { ArticleHeader, Callout, DocumentationTable } from "./_docs/components";
import { DocsShell, MobileToc } from "./_docs/DocsShell";
import { referenceNavigation, roleNavigation } from "./_docs/navigation";

const toc = [
  { id: "choose-role", label: "Choose your role" },
  { id: "journey", label: "Document journey" },
  { id: "ownership", label: "System ownership" },
  { id: "common-tasks", label: "Common tasks" },
  { id: "scope", label: "Scope and limitations" },
];

export default function Home() {
  return (
    <DocsShell activePath="/" toc={toc}>
      <ArticleHeader
        category="Overview"
        title="PDF Bridge documentation"
        summary="Internal documentation for the PDF intake control plane, organized around the people who use, operate, integrate, review, and maintain it."
        facts={[
          { term: "Audience", detail: "Internal project team" },
          { term: "Deployment", detail: "Linux-only proof of concept" },
          { term: "Application", detail: "Litestar, SQLAlchemy, SQLite, ClamAV" },
        ]}
      />
      <MobileToc toc={toc} />

      <Callout title="Role guides are responsibility views, not application roles">
        <p>
          Browser users currently share one capability set. Trusted-header mode supplies identity
          attribution but not role authorization. Jenkins is the only separate enforced boundary,
          through its bearer token. One person may hold several of these responsibilities.
        </p>
      </Callout>

      <section id="choose-role">
        <h2>Choose your role</h2>
        <p>Start with the guide that matches the decision or system you own.</p>
        <div className="role-index">
          {roleNavigation.map((role) => (
            <Link href={role.href} key={role.href}>
              <strong>{role.label}</strong>
              <span>{role.description}</span>
            </Link>
          ))}
        </div>
      </section>

      <section id="journey">
        <h2>Document journey</h2>
        <div className="journey compact-journey">
          <div><span>Library operator</span><strong>Select collection and upload.</strong><p>The framework spools the multipart part, then the bridge copies it into private quarantine while validating, hashing, and scanning. Only clean content is promoted atomically.</p></div>
          <div><span>Jenkins owner</span><strong>Claim and stage an immutable batch.</strong><p>The CLI checks canonical paths, byte counts, hashes, and the complete operation set before acknowledgement.</p></div>
          <div><span>RAG pipeline owner</span><strong>Parse, index, or delete.</strong><p>Every operation receives a strict result covering PDF source, Markdown, BM25, and dense components.</p></div>
          <div><span>PDF Bridge</span><strong>Apply results and expose recoverable state.</strong><p>The catalog records success, failure, or cleanup without optimistic deletion.</p></div>
          <div><span>Retrieval and chatbot owners</span><strong>Return correlated results within authorized collections.</strong><p>Collection and bridge UUID remain consistent from catalog through Qdrant and policy.</p></div>
        </div>
      </section>

      <section id="ownership">
        <h2>System ownership</h2>
        <DocumentationTable
          headings={["System", "Owns", "Does not own"]}
          rows={[
            ["PDF Bridge", "Clean canonical PDF bytes, catalog, lifecycle, queue, audit, browser workspace", "PDF parsing, chunks, indexes, end-user authorization"],
            ["ClamAV", "First-pass malware signature scanning", "Proof that a PDF is safe to parse"],
            ["Jenkins client", "Authenticated claim, verified download, atomic staging, report submission", "Parsing or catalog mutation"],
            ["RAG pipeline", "Parsing, derived PDF/Markdown, BM25, dense/Qdrant, downstream delete", "Collection placement or canonical bridge storage"],
            ["Retrieval service", "Search execution, ranking, grouped response contract", "End-user authorization"],
            ["Chatbot manager", "Authenticated user policy and allowed-collection intersection", "Bridge operator workflows"],
          ]}
        />
      </section>

      <section id="common-tasks">
        <h2>Technical reference</h2>
        <div className="reference-index">
          {referenceNavigation.map((item) => (
            <Link href={item.href} key={item.href}>
              <strong>{item.label}</strong>
              <span>{item.description}</span>
            </Link>
          ))}
        </div>
      </section>

      <section id="scope">
        <h2>Scope and limitations</h2>
        <p>
          This POC is intended for a restricted internal network. Anonymous mode is not identity,
          collection audience labels are not authorization, SQLite supports one application
          process, and a clean ClamAV result does not neutralize parser risk. The security reviewer
          guide and OSS reference separate implemented controls from work required before a
          controlled pilot or enterprise claim.
        </p>
      </section>

    </DocsShell>
  );
}
