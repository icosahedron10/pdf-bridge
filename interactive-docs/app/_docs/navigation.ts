export type NavigationItem = {
  href: string;
  label: string;
  description: string;
};

export const roleNavigation: NavigationItem[] = [
  {
    href: "/roles/library-operator",
    label: "Library operator",
    description: "Upload, search, follow, retry, and remove documents.",
  },
  {
    href: "/roles/jenkins-owner",
    label: "Jenkins owner",
    description: "Claim, verify, stage, and submit scheduled work safely.",
  },
  {
    href: "/roles/rag-pipeline-owner",
    label: "RAG pipeline owner",
    description: "Parse, index, delete, and produce strict component results.",
  },
  {
    href: "/roles/platform-operator",
    label: "Platform operator",
    description: "Configure, run, monitor, back up, and upgrade the service.",
  },
  {
    href: "/roles/retrieval-integrator",
    label: "Retrieval service integrator",
    description: "Implement grouped search and keep responses correlated to the catalog.",
  },
  {
    href: "/roles/chatbot-integrator",
    label: "Chatbot integrator",
    description: "Apply authenticated collection authorization before retrieval.",
  },
  {
    href: "/roles/security-reviewer",
    label: "Security reviewer",
    description: "Evaluate the upload boundary, residual risk, and enterprise gates.",
  },
  {
    href: "/roles/code-maintainer",
    label: "Code maintainer",
    description: "Navigate the layers, invariants, tests, and change points.",
  },
];

export const referenceNavigation: NavigationItem[] = [
  {
    href: "/reference/lifecycle",
    label: "Lifecycle states",
    description: "Document, operation, and batch transitions.",
  },
  {
    href: "/reference/batch-contract",
    label: "Batch contract",
    description: "Claim, manifest, staging, result, and replay rules.",
  },
  {
    href: "/reference/code-map",
    label: "Code map",
    description: "Layer direction and module responsibilities.",
  },
  {
    href: "/reference/configuration",
    label: "Configuration & operations",
    description: "Settings, health, persistence, backups, and upgrades.",
  },
  {
    href: "/reference/search-boundary",
    label: "Search boundary",
    description: "Request/response correlation and collection isolation.",
  },
  {
    href: "/reference/oss-review",
    label: "Playwright & ClamAV",
    description: "Point-in-time OSS decisions and follow-up work.",
  },
];

export const allNavigation: NavigationItem[] = [
  { href: "/", label: "Start here", description: "Choose a guide by responsibility." },
  ...roleNavigation,
  ...referenceNavigation,
];

const related = (...hrefs: string[]): NavigationItem[] =>
  hrefs.map((href) => {
    const item = allNavigation.find((candidate) => candidate.href === href);
    if (!item) throw new Error(`Unknown related documentation path: ${href}`);
    return item;
  });

export const relatedRoleGuides: Record<string, NavigationItem[]> = {
  "library-operator": related(
    "/roles/jenkins-owner",
    "/reference/lifecycle",
    "/reference/search-boundary",
  ),
  "jenkins-owner": related(
    "/roles/rag-pipeline-owner",
    "/reference/batch-contract",
    "/reference/configuration",
  ),
  "rag-pipeline-owner": related(
    "/roles/jenkins-owner",
    "/reference/lifecycle",
    "/reference/search-boundary",
  ),
  "platform-operator": related(
    "/reference/configuration",
    "/roles/security-reviewer",
    "/reference/oss-review",
  ),
  "retrieval-integrator": related(
    "/roles/chatbot-integrator",
    "/reference/search-boundary",
    "/reference/code-map",
  ),
  "chatbot-integrator": related(
    "/roles/retrieval-integrator",
    "/reference/search-boundary",
    "/roles/security-reviewer",
  ),
  "security-reviewer": related(
    "/reference/oss-review",
    "/reference/configuration",
    "/roles/chatbot-integrator",
  ),
  "code-maintainer": related(
    "/reference/code-map",
    "/reference/lifecycle",
    "/reference/batch-contract",
  ),
};

export function adjacentPages(activePath: string): {
  previous: NavigationItem | null;
  next: NavigationItem | null;
} {
  const pages = activePath.startsWith("/reference/") ? referenceNavigation : allNavigation;
  const index = pages.findIndex((item) => item.href === activePath);
  return {
    previous: index > 0 ? pages[index - 1] : null,
    next: index >= 0 && index < pages.length - 1 ? pages[index + 1] : null,
  };
}
