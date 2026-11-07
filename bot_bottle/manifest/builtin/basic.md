---
egress:
  routes:
    # Package registries: downloads only.
    - host: registry.npmjs.org
      inspect:
        matches:
          - methods: [GET, HEAD]
    - host: pypi.org
      inspect:
        matches:
          - methods: [GET, HEAD]
    - host: files.pythonhosted.org
      inspect:
        matches:
          - methods: [GET, HEAD]
    - host: crates.io
      inspect:
        matches:
          - methods: [GET, HEAD]
    - host: index.crates.io
      inspect:
        matches:
          - methods: [GET, HEAD]
    - host: static.crates.io
      inspect:
        matches:
          - methods: [GET, HEAD]
    - host: proxy.golang.org
      inspect:
        matches:
          - methods: [GET, HEAD]
    - host: sum.golang.org
      inspect:
        matches:
          - methods: [GET, HEAD]
    - host: rubygems.org
      inspect:
        matches:
          - methods: [GET, HEAD]
    # Public code: reads, and read-only git clone/fetch. No pushes.
    - host: github.com
      inspect:
        git:
          fetch: true
        matches:
          - methods: [GET, HEAD]
    - host: api.github.com
      inspect:
        matches:
          - methods: [GET, HEAD]
    - host: codeload.github.com
      inspect:
        matches:
          - methods: [GET, HEAD]
    - host: objects.githubusercontent.com
      inspect:
        matches:
          - methods: [GET, HEAD]
    - host: raw.githubusercontent.com
      inspect:
        matches:
          - methods: [GET, HEAD]
    - host: gitlab.com
      inspect:
        git:
          fetch: true
        matches:
          - methods: [GET, HEAD]
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
