# PDF Bridge documentation wiki

Internal role guides and technical reference for the PDF Bridge proof of concept, built with
vinext and deployed through OpenAI Sites.

The wiki uses separate, linkable routes for the library operator, semantic intake owner, platform
operator, retrieval service integrator, chatbot integrator, security reviewer, and code maintainer.
Reference pages cover lifecycle states, the intake API, code architecture, configuration and
operations, search correlation, and the Playwright/ClamAV review.

These guides describe responsibilities, not application RBAC. Browser users currently share one
capability set. The lifespan-owned worker is internal to the single application process, while
provider and Qdrant credentials protect service boundaries.

## Local development

Requires Node.js 22.13 or newer.

```sh
npm ci
npm run dev
```

Use `npm run lint` and `npm test` before publishing. The site has no persistence, uploads, or
application-owned authentication.
