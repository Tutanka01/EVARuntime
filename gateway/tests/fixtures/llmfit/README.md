# Sorties enregistrées — adaptateur LLMfit (AUT-004)

**Ces fichiers sont SYNTHÉTIQUES.** Aucun n'est une capture d'un LLMfit réel.

Ils sont préfixés `synthetic-` pour que ce statut soit visible depuis
l'arborescence, sans avoir à ouvrir un fichier.

## D'où vient la forme supposée

La forme exacte de `llmfit recommend --json` n'est pas documentée publiquement :
le dépôt <https://github.com/AlexsJones/llmfit> et `docs/cli.md` annoncent
« top picks as JSON (agent/script consumption) » sans fixer les noms de champs
ni un exemple de sortie. Vérifié le 31 juillet 2026.

Les clés consommées ici sont donc **dérivées de la description du plan
d'amorçage** (quantification recommandée, tenue en VRAM, MoE,
multi-GPU) et non d'une capture. C'est une hypothèse assumée, pas une
observation.

## Ce qu'il faut faire avant la production

Remplacer ces fichiers par de vraies captures obtenues sur un hôte disposant du
binaire épinglé :

```sh
llmfit recommend --json > real-<hôte>-<version>.json
```

puis réaligner `parse_llmfit_json()` sur la forme observée. Le coût d'une
hypothèse fausse est borné par construction : une sortie qui ne correspond pas
au validateur produit un constat `llmfit_schema_invalid` de niveau `warn` et le
plan se poursuit sans recommandation. À aucun moment une donnée non validée
n'entre dans le plan.
