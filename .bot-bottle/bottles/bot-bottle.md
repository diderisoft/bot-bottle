---
extends: basic
egress:
  routes:
    - host: news.ycombinator.com
      inspect:
        matches:
          - methods:
              - GET
              - HEAD
---

The bottle for this project. bot-bottle adds a route here when you
allow a request "for project"; review changes like any other code.
