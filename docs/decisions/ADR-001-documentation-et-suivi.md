# ADR-001 — Documentation et suivi du projet

- **Statut :** accepté
- **Date :** 2026-09-03

## Contexte

Le projet possède un README, une vision, un tracker d’audit et un backlog
technique. Maintenir le même statut dans plusieurs fichiers crée rapidement des
contradictions et rend le dépôt difficile à reprendre.

## Décision

- `ROADMAP.md` est la source canonique du statut, des limites, des jalons et
  des epics.
- `docs/vision.md` résume la thèse produit ; il ne recopie pas le backlog.
- `docs/architecture.md`, `docs/api.md`, `docs/deployment.md` et
  `docs/observability.md` décrivent le comportement livré.
- GitHub Issues suivent le travail actionnable ; les epics regroupent les
  chantiers et les checklists évitent les micro-issues prématurées.
- Les choix non tranchés passent par une RFC/Discussion, puis une ADR lors de
  la décision.
- `codex-analyse.md` est conservé comme archive historique et n’est plus le
  tracker courant.

## Conséquences

Les changements de comportement doivent mettre à jour la documentation
technique et le jalon de roadmap concernés. Un agent qui reprend le projet lit
`ROADMAP.md` avant le code.
