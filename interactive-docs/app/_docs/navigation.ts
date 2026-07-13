export type NavigationItem = {
  href: string;
  label: string;
  description: string;
};

export const roleNavigation: NavigationItem[] = [
  {
    href: "/roles/library-operator",
    label: "Library operator",
    description: "Upload, review, follow, retry, and remove documents.",
  },
  {
    href: "/roles/semantic-intake-owner",
    label: "Semantic intake owner",
    description: "Own analysis, worker recovery, indexing, and safe replacement.",
  },
  {
    href: "/roles/platform-operator",
    label: "Platform operator",
    description: "Configure, run, monitor, back up, reset, and upgrade the service.",
  },
  {
    href: "/roles/retrieval-integrator",
    label: "Retrieval service integrator",
    description: "Query active aliases and keep responses correlated to the catalog.",
  },
  {
    href: "/roles/chatbot-integrator",
    label: "Chatbot integrator",
    description: "Apply authenticated collection authorization before retrieval.",
  },
  {
    href: "/roles/security-reviewer",
    label: "Security reviewer",
    description: "Evaluate intake, parser, model, index, and retrieval boundaries.",
  },
  {
    href: "/roles/code-maintainer",
    label: "Code maintainer",
    description: "Navigate the layers, invariants, worker, tests, and change points.",
  },
];

export const referenceNavigation: NavigationItem[] = [
  {
    href: "/reference/lifecycle",
    label: "Lifecycle states",
    description: "Document, operation, phase, decision, and replacement transitions.",
  },
  {
    href: "/reference/intake-api",
    label: "Intake API",
    description: "Preflight, upload, polling, evidence, decision, retry, and deletion.",
  },
  {
    href: "/reference/code-map",
    label: "Code map",
    description: "Layer direction, worker flow, and module responsibilities.",
  },
  {
    href: "/reference/configuration",
    label: "Configuration & operations",
    description: "Settings, health, recovery, backups, reset, and upgrades.",
  },
  {
    href: "/reference/search-boundary",
    label: "Search boundary",
    description: "Active-only retrieval, response correlation, and collection isolation.",
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
    "/roles/semantic-intake-owner",
    "/reference/lifecycle",
    "/reference/intake-api",
  ),
  "semantic-intake-owner": related(
    "/roles/platform-operator",
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
    "/roles/semantic-intake-owner",
  ),
  "code-maintainer": related(
    "/reference/code-map",
    "/reference/lifecycle",
    "/reference/intake-api",
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
