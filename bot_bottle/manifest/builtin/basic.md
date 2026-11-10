---
# Every route here is read-only: a route allows GET and HEAD unless it lists
# methods.
egress:
  routes:
    # Package registries: downloads only.
    - host: registry.npmjs.org
    - host: pypi.org
    - host: files.pythonhosted.org
    - host: crates.io
    - host: index.crates.io
    - host: static.crates.io
    - host: proxy.golang.org
    - host: sum.golang.org
    - host: rubygems.org
    # Public code: reads, and read-only git clone/fetch. No pushes.
    - host: github.com
      git: { fetch: true }
    - host: api.github.com
    - host: codeload.github.com
    - host: objects.githubusercontent.com
    - host: raw.githubusercontent.com
    - host: gitlab.com
      git: { fetch: true }
---

Package registries and public code, read-only.

The built-in `basic` bottle: an agent's model provider (its own route),
package registries and public code, read-only, and nothing else. Outbound
DLP and inbound scanning stay on for every route. Shadow it with
`~/.bot-bottle/bottles/basic.md`, or extend it from a project bottle:

    ---
    extends: basic
    egress:
      routes:
        - host: api.stripe.com
    ---
