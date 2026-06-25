# CLAUDE.md - tatara-observability

This repo holds the tatara platform's observability-as-code: agent-adjustable
Grafana alert rules, applied to Grafana via terraform on merge. It is platform
infra, not a tatara code component, but it follows the tatara per-repo contract.

## What this repo is

`tatara-observability` is where the tatara platform defines (and the agents tune)
the Grafana alert rules for the tatara components. Agents edit only
`alerts/tatara-*.yaml` - the simple `name`/`queries[].expression`/`math_operator`/
`threshold`/`for`/`decimal_points`/`annotations`/`labels` schema. The
`modules/grafana_alert` module renders each file (one rule group) into the Grafana
`Tatara` folder. GitHub Actions runs terraform: a PR posts a sticky `terraform
plan`; merge to `main` runs `terraform apply` (Grafana Editor SA token, S3 state).

Alerts carry `homelab=true` + `system=tatara` + `component` + `severity`. The
`system=tatara` notification-policy route + the operator-incident-webhook contact
point live in `infra/terraform/grafana` (global homelab routing, label-based) -
NOT here. So a firing tatara alert routes to `/operator/webhooks/tatara/grafana`
-> an `incident` Task (emergency-brainstorm) -> a `brainstorming` issue.

## What this repo is NOT

- Not a code component. No Go, no Dockerfile, no chart. Terraform + alert YAML only.
- Not the routing. Contact points + notification policy stay in
  `infra/terraform/grafana`; this repo only owns the `Tatara` folder + `tatara-*`
  rule groups. Routing is by label and works regardless of folder/owner.
- Not dashboards (yet). The repo may grow dashboards-as-code later; out of scope now.

## Hard rules

1. **Newest stable Go** for any Go service. Pin the Go directive to the
   exact minor in `go.mod`. (N/A here - no Go.)
2. **KISS, always.** Prefer simplicity over cleverness. Three similar
   lines is better than a premature abstraction.
3. **Boy-scout rule on adjacent issues.** If you see something easy to
   fix alongside current work, fix it. Do not ask.
4. **NEVER introduce tech-debt.** If a thing is complex, call it out in
   `MEMORY.md` with the rationale. Never defer cleanup to "later".
5. **Charts created via `helm create <name>`** then edited. Never
   hand-rolled. (N/A here - charts are pulled by version.)
6. **No plain ENVs in values.yaml. No lists in values.yaml.** All inputs
   map: camelCase scalar in `values.yaml` -> kebab-case key in
   ConfigMap/Secret -> workload consumes via `envFrom`. Genuinely
   list-shaped data is rendered into a templated ConfigMap and read at
   runtime.
7. **Sonnet for implementation. Opus for merges.** Implementation
   subagents are sonnet; the merge subagent is opus. Plan and review run
   in opus.
8. **EVERYTHING through superpowers.** brainstorming, writing-plans,
   test-driven-development, systematic-debugging, requesting-code-review,
   verification-before-completion, subagent-driven-development,
   using-git-worktrees, finishing-a-development-branch are mandatory.
9. **Subagent-driven, parallel development** where tasks are independent.
   Dispatch in a single message for true parallelism.
10. **Branch flow:** worktree off `main` -> develop in worktree -> merge
    back to source repo `main` -> cleanup worktree -> deploy from `main`
    only. NEVER deploy from a worktree.
11. **JSON logs only.** (N/A - no service code.)
12. **Log every business action at INFO** with structured fields. (N/A.)
13. **Metrics for everything that counts, times out, or can fail.** (N/A.)
14. **Charts are cluster-agnostic.** A component's helm chart MUST assume
    nothing about the cluster. All cluster-specific customization comes
    from THIS repo's values tree (per-bucket `values/common.yaml` +
    per-release `values/<name>/{common,default}.yaml` + sops
    `default.secrets.yaml`).

## Writing rules

- No em dashes. No smart quotes. No arrows. No decorative Unicode. Plain
  hyphens and straight quotes.
- No preamble. No recap unless asked. One line at most.
- Show diffs, not whole files, for anything > 30 lines that exists.
- No docstrings/comments on code not being changed.

## What I want from a Claude session here

- Read `MEMORY.md` and `ROADMAP.md` before non-trivial work.
- Update `MEMORY.md` on any non-obvious decision or dead-end. One dated
  line per entry.
- Update `ROADMAP.md` on phase completion / re-scope.
- Use `/handoff` near context limits; do not soldier on.
- NEVER `docker buildx`/`helm push` charts or images locally; component
  CI builds + pushes to harbor on merge to main.
- The deploy runner SA is cluster-admin scoped - the single highest-risk
  element. Any code in `arc-runner-tatara-helmfile` can do anything to the
  cluster. Keep this repo bot-only-write and private.
