---
egress:
  routes:
    - host: bot-bottle.com
extends: basic
git_gate:
  repos:
    - github.com/diderisoft/bot-bottle:write
    - github.com/diderisoft/bot-bottle-brand:write
    - github.com/diderisoft/bot-bottle-console:write
    - github.com/diderisoft/bot-bottle-design:write
  forges:
    github.com:
      kind: github
---
