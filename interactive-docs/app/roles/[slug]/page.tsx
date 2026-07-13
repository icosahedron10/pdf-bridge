import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { ArticleHeader, RelatedGuides } from "../../_docs/components";
import { DocsShell, MobileToc } from "../../_docs/DocsShell";
import { roleGuides } from "../../_docs/role-content";
import { relatedRoleGuides } from "../../_docs/navigation";

type PageProps = {
  params: Promise<{ slug: string }>;
};

export function generateStaticParams() {
  return Object.keys(roleGuides).map((slug) => ({ slug }));
}

export async function generateMetadata({ params }: PageProps): Promise<Metadata> {
  const { slug } = await params;
  const guide = roleGuides[slug];
  return guide
    ? { title: `${guide.title} · PDF Bridge documentation`, description: guide.summary }
    : {};
}

export default async function RolePage({ params }: PageProps) {
  const { slug } = await params;
  const guide = roleGuides[slug];
  if (!guide) notFound();

  const activePath = `/roles/${slug}`;
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
      <RelatedGuides links={relatedRoleGuides[slug] ?? []} />
    </DocsShell>
  );
}
