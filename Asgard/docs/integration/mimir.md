# Mímir and Muninn

Not an app and not an identity · a module to add: `asgard.mimir` · the one switch between AI tiers

Every AI feature (Loki's BLUFs, Freya's drafts, Baldur's optional commit tagging) asks Mímir for a completion and never cares which tier answers.

| Tier | How | Needs |
| --- | --- | --- |
| 1. API | Mímir calls the approved endpoint | Endpoint, credential, an approved data flow |
| 2. MCP | The AI client calls Asgard through Ysildir | Copilot's MCP policy on |
| 3. Clipboard | Mímir builds the pack and prompt; you paste it into M365 Copilot Chat and paste the answer back | Nothing new; works on day one |

## Interface

```python
answer = mimir.complete(purpose="loki.bluf", prompt=text, evidence=pack)
answer.text, answer.tier, answer.model, answer.prompt_version
```

- `purpose` picks the prompt template and which tiers are allowed for that data (set in Asgard's settings, per purpose, by you or your ISSO).
- Tier 3 returns after the paste-back; the caller's UI owns that wait, and holds no Muninn transaction during it.

## Muninn's part

Mímir writes nothing to Muninn. The caller stores `tier`, `model` and `prompt_version` on the row it writes (`blufs`, `review_drafts`), which is how a change in output is traced to a change in prompt or model. Prompts and answers aren't logged anywhere else; the row is the record.

Credentials for tier 1 live in Credential Manager. Any error text a provider returns goes through `muninn.scrub()` before it reaches a row or a log.
