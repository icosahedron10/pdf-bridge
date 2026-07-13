import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { ArticleHeader, PageNavigation } from "../../_docs/components";
import { DocsShell, MobileToc } from "../../_docs/DocsShell";
import { referenceGuides } from "../../_docs/reference-content";

type PageProps = {
  params: Promise<{ slug: string }>;
};

export function generateStaticParams() {
  return Object.keys(referenceGuides).map((slug) => ({ slug }));
}

export async function generateMetadata({ params }: PageProps): Promise<Metadata> {
  const { slug } = await params;
  const guide = referenceGuides[slug];
  return guide
    ? { title: `${guide.title} · PDF Bridge documentation`, description: guide.summary }
    : {};
}

export default async function ReferencePage({ params }: PageProps) {
  const { slug } = await params;
  const guide = referenceGuides[slug];
  if (!guide) notFound();

  const activePath = `/reference/${slug}`;
  return (
    <DocsShell activePath={activePath} toc={guide.toc}>
      <ArticleHeader
        category={guide.category}
        title={guide.title}
        summary={guide.summary}
        facts={guide.facts}
      />
      <MobileToc toc={guide.toc} />
      {guide.content}
      <PageNavigation activePath={activePath} />
    </DocsShell>
  );
}
