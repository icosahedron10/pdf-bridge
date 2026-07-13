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
          attribution but not role authorization. Every trusted operator may Keep, Replace, Cancel,
          and delete. Service and provider credentials protect machine boundaries; they do not add
          browser roles. One person may hold several of these responsibilities.
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
          <div><span>Library operator</span><strong>Select a collection and upload.</strong><p>The bridge streams, validates, hashes, and scans each PDF. Clean bytes are promoted atomically and return as durable analysis work.</p></div>
          <div><span>Internal worker</span><strong>Extract and compare.</strong><p>A limited parser child produces page-mapped text. The worker searches active and private screening indexes inside the selected collection.</p></div>
          <div><span>Operator or policy</span><strong>Publish or review.</strong><p>A clear analysis proceeds automatically. Advisory findings wait indefinitely for an explicit Keep, Replace, or Cancel decision.</p></div>
          <div><span>PDF Bridge</span><strong>Mutate Qdrant safely.</strong><p>Durable outbox steps publish complete dense and BM25 points, or remove and verify old points before a replacement becomes active.</p></div>
          <div><span>Retrieval and chatbot owners</span><strong>Query only authorized active content.</strong><p>The stable collection alias and bridge UUID remain consistent from catalog through Qdrant and user policy.</p></div>
        </div>
      </section>

      <section id="ownership">
        <h2>System ownership</h2>
        <DocumentationTable
          headings={["System", "Owns", "Does not own"]}
          rows={[
            ["PDF Bridge", "Canonical bytes, lifecycle, analysis artifacts, decisions, audit, outbox, and Qdrant mutation", "Chatbot-user authorization"],
            ["ClamAV", "Synchronous malware signature verdict", "Parser containment or lifecycle state"],
            ["Parser subprocess", "Page-mapped extraction under hard limits", "Network access, durable state, decisions, or indexing"],
            ["Embedding and LLM providers", "Configured model inference", "Candidate suppression, publication, replacement, or deletion"],
            ["Qdrant", "Active and private screening vector persistence", "Catalog authority or operator decisions"],
            ["Retrieval service", "Keyword, semantic, and hybrid search over active aliases", "Screening access or end-user authorization"],
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
          process, and a clean ClamAV result does not neutralize parser risk. The resource-limited
          parser subprocess is defense in depth, not a complete sandbox. The security reviewer
          guide and OSS reference separate implemented controls from the work required before a
          controlled pilot or enterprise claim.
        </p>
      </section>

    </DocsShell>
  );
}
