# Guide administrateur — EVARuntime

Ce document s'adresse à l'administrateur du gateway : gestion des utilisateurs,
des clés API, du registre des modèles, surveillance du système et reporting d'usage.

Trois interfaces sont disponibles :
- **Dashboard** (`/admin/dashboard`) : interface web avec graphiques, tableaux et métriques en temps réel — recommandé pour la surveillance quotidienne
- **CLI** (`cli.py`) : à utiliser directement sur le serveur, idéal pour la gestion des utilisateurs, des clés et la vérification du registre
- **API REST** (`/admin/...`) : accessible depuis le réseau campus uniquement, utile pour l'automatisation et la gestion des modèles à chaud

---

## Table des matières

1. [Accès administrateur](#1-accès-administrateur)
2. [Gestion des utilisateurs](#2-gestion-des-utilisateurs)
3. [Gestion des clés API](#3-gestion-des-clés-api)
4. [Surveillance du système](#4-surveillance-du-système)
5. [Rapports d'usage](#5-rapports-dusage)
6. [Contrôle des modèles](#6-contrôle-des-modèles)
7. [Référence API REST admin](#7-référence-api-rest-admin)
8. [Diagnostic préflight — `doctor`](#8-diagnostic-préflight--doctor)
9. [Planificateur d'amorçage — `bootstrap-plan`](#9-planificateur-damorçage--bootstrap-plan)
10. [Applicateur d'amorçage — `bootstrap-apply`](#10-applicateur-damorçage--bootstrap-apply)

---

## 1. Accès administrateur

### Identifier le parcours déployé

```bash
sudo awk -F= '$1 == "CLUSTER_MODE" {print $2}' /etc/llm-gateway/env
systemctl cat llm-gateway | grep '^Description='
curl -fsS http://127.0.0.1:8000/ready | python3 -m json.tool
```

- `local` : le service utilise le profil systemd GPU et lance `llama-server`
  sur cet hôte.
- `cluster` : le service utilise le profil orchestrateur sans GPU; les agents,
  leurs ports et leurs mises à jour sont administrés sur chaque nœud.

Ne modifiez pas `CLUSTER_MODE` avec `sed` pour migrer. Utilisez
`gateway/deploy/update.sh --mode <cible> --allow-mode-change`, qui valide le
profil, choisit l'unité correspondante et sait restaurer le mode précédent.

### Via CLI (sur le serveur)

```bash
# Toujours depuis le répertoire d'installation
cd /opt/llm-gateway

# Raccourci pratique à ajouter dans ~/.bashrc :
alias llmgw='sudo -u llmservice /opt/llm-gateway/venv/bin/python /opt/llm-gateway/cli.py'

# Aide générale
llmgw --help

# Aide d'une commande spécifique
llmgw add-user --help
```

### Via API REST (depuis le réseau campus)

Toutes les routes `/admin/` nécessitent :
- L'`ADMIN_SECRET` (dans `/etc/llm-gateway/env`) en Bearer token
- Être sur le réseau campus (filtrage IP nginx)

> **Fail-closed :** si `ADMIN_SECRET` est vide, laissé à sa valeur d'exemple
> (`CHANGE_ME_*`) ou plus court que 32 caractères, toutes les routes `/admin/`
> répondent 503 tant qu'un secret fort n'est pas configuré. Générer avec :
> `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
> `doctor` signale un secret faible comme un échec bloquant (exit 1).

```bash
# Récupérer l'ADMIN_SECRET
sudo grep ADMIN_SECRET /etc/llm-gateway/env

# L'exporter pour les exemples suivants
export ADMIN_SECRET="votre_secret_ici"
export GW="https://llm.eva.univ-pau.fr"
```

---

## 2. Gestion des utilisateurs

### Créer un utilisateur

```bash
# CLI — minimal
llmgw add-user alice

# CLI — complet
llmgw add-user alice \
  --email alice@univ-pau.fr \
  --rpm 30 \
  --monthly-tokens 500000 \
  --notes "Doctorante L3i, thèse sur les LLMs"

# API REST
curl -s -X POST "$GW/admin/users" \
  -H "Authorization: Bearer $ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "alice",
    "email": "alice@univ-pau.fr",
    "rpm_limit": 30,
    "monthly_token_limit": 500000,
    "notes": "Doctorante L3i"
  }' | python3 -m json.tool
```

**Paramètres :**

| Paramètre | Défaut | Description |
|-----------|--------|-------------|
| `rpm_limit` | 20 | Requêtes par minute maximum |
| `monthly_token_limit` | 0 | Quota tokens/mois (0 = illimité). Appliqué sur une fenêtre glissante de 30 jours : tout dépassement retourne 429 jusqu'à ce que la consommation repasse sous la limite |
| `email` | — | Email institutionnel (optionnel) |
| `notes` | — | Notes libres pour l'admin |

### Lister les utilisateurs

```bash
# CLI — utilisateurs actifs seulement
llmgw list-users

# CLI — tous (y compris désactivés)
llmgw list-users --all

# API REST
curl -s "$GW/admin/users" \
  -H "Authorization: Bearer $ADMIN_SECRET" | python3 -m json.tool
```

Exemple de sortie CLI :

```
┌────┬──────────────────┬────────────────────────────┬───────┬─────┬────────────┐
│ ID │ Username         │ Email                      │ Actif │ RPM │ Créé le    │
├────┼──────────────────┼────────────────────────────┼───────┼─────┼────────────┤
│  1 │ alice            │ alice@univ-pau.fr           │ oui   │  30 │ 2025-03-01 │
│  2 │ bob              │ bob@univ-pau.fr             │ oui   │  20 │ 2025-03-05 │
│  3 │ carol            │ carol@univ-pau.fr           │ non   │  20 │ 2025-02-10 │
└────┴──────────────────┴────────────────────────────┴───────┴─────┴────────────┘
```

### Modifier un utilisateur

```bash
# Changer la limite RPM
curl -s -X PATCH "$GW/admin/users/alice" \
  -H "Authorization: Bearer $ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"rpm_limit": 50}'

# Modifier le quota mensuel de tokens
curl -s -X PATCH "$GW/admin/users/alice" \
  -H "Authorization: Bearer $ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"monthly_token_limit": 1000000}'
```

### Désactiver / réactiver un utilisateur

La désactivation est **immédiate** : toutes les clés de l'utilisateur sont
invalides dès la prochaine requête. Aucune requête en cours n'est interrompue.

```bash
# CLI — désactiver
llmgw disable-user carol

# CLI — réactiver
llmgw enable-user carol

# API REST — désactiver
curl -s -X PATCH "$GW/admin/users/carol" \
  -H "Authorization: Bearer $ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"is_active": false}'
```

### Anonymiser un utilisateur (droit à l'effacement RGPD)

C'est le chemin d'exercice du **droit à l'effacement** (RGPD art. 17). L'opération
est **irréversible** : aucune donnée effacée n'est récupérable.

La politique retenue est l'**anonymisation**, pas la suppression de ligne. La
ligne utilisateur est conservée pour que l'historique de facturation et d'audit
reste exploitable, tandis que la personne cesse d'être ré-identifiable.

| | Champ | Devient |
|---|---|---|
| **Effacé** | `users.username` | pseudonyme stable `anonymized-user:<id>` |
| **Effacé** | `users.email` | `NULL` |
| **Effacé** | `users.notes` | `NULL` |
| **Effacé** | `api_keys.name` (champ libre) | `NULL` |
| **Désactivé** | `users.is_active` | `0` — toutes les requêtes sont rejetées |
| **Révoqué** | `api_keys.is_active` | `0` sur **toutes** les clés du compte |
| **Conservé** | `users.id`, `users.created_at` | inchangés |
| **Conservé** | toutes les lignes `usage_log` | inchangées |
| **Ajouté** | `users.anonymized_at` | horodatage UTC de l'opération |

**Pourquoi pas une suppression.** `usage_log.user_id` référence `users(id)` et
`PRAGMA foreign_keys = ON` est appliqué à chaque connexion : un `DELETE FROM
users` échouait en violation de clé étrangère dès que l'utilisateur avait servi
une requête. Les deux alternatives ont été écartées — `ON DELETE CASCADE` fait
disparaître l'historique de facturation, `ON DELETE SET NULL` casse les jointures
des rapports.

```bash
# CLI — avec confirmation interactive
llmgw anonymize-user alice

# CLI — non interactif (scripts de fin d'année)
llmgw anonymize-user alice --yes

# API REST — le verbe DELETE est conservé pour les scripts existants,
# mais son effet est une anonymisation, décrite dans la réponse.
curl -s -X DELETE "$GW/admin/users/alice" \
  -H "Authorization: Bearer $ADMIN_SECRET" | python3 -m json.tool
```

Réponse de l'API :

```json
{
  "status": "anonymized",
  "message": "Utilisateur anonymisé : données personnelles effacées définitivement, clés révoquées, historique d'usage conservé.",
  "user_id": 1,
  "anonymized_username": "anonymized-user:1",
  "anonymized_at": "2025-03-20 14:05:11",
  "keys_revoked": 2,
  "keys_total": 2,
  "erased_fields": ["username", "email", "notes", "api_keys.name"],
  "retained": ["users.id", "users.created_at", "usage_log"]
}
```

**Comportement à connaître :**

- **Idempotent.** Une seconde anonymisation répond `200` avec
  `"status": "already_anonymized"` et **préserve l'horodatage initial**. Notez
  que l'ancien nom n'existe plus : reciblez le compte par son pseudonyme.
- **Utilisateur inexistant** → `404`, et code de sortie `1` côté CLI.
- **Nom réutilisable.** L'ancien nom d'utilisateur est libéré : recréer un compte
  homonyme fonctionne (cas d'un étudiant qui revient). Le nouveau compte a un
  `id` distinct et ne récupère pas l'historique de l'ancien.
- **Visibilité.** Un compte anonymisé reste listé par `GET /admin/users` et
  `llmgw list-users --all`, comme un compte désactivé, avec un `anonymized_at`
  non nul qui le distingue d'une simple désactivation. Il n'est **jamais** compté
  dans les utilisateurs actifs du dashboard.
- **Rapports.** `GET /admin/usage` et `GET /admin/usage/summary` continuent
  d'inclure ses requêtes, sous le pseudonyme : les totaux de facturation sont
  inchangés. C'est l'objectif même de cette politique.
- **Journaux.** L'opération ne journalise que l'`id` technique — ni le nom, ni
  l'e-mail, ni les notes effacées ne réapparaissent dans les logs applicatifs.
  Les **journaux d'accès** sont rédigés eux aussi, des deux côtés : le
  middleware de la gateway remplace le segment de nom de `/admin/users/<nom>`
  par `<redacted>`, et un filtre posé sur le logger `uvicorn.access` fait de même
  sur SA ligne — celle que journald conserve, puisque `--access-log` est actif
  dans les deux unités systemd. Cette seconde rédaction couvre aussi la **query
  string** : `GET /admin/usage?username=<nom>` est journalisé
  `?username=<redacted>` (SEC-010). Seuls `from_date`, `to_date`, `limit`,
  `force` et `period` gardent une valeur lisible ; la valeur de tout autre
  paramètre, présent ou futur, est rédigée par défaut.

> **Désactiver ≠ anonymiser.** `disable-user` bloque l'accès en conservant toutes
> les données et se réactive avec `enable-user`. `anonymize-user` efface les
> données personnelles définitivement et ne s'annule pas. Pour une suspension
> temporaire, utilisez `disable-user`.

> **Purge de l'historique.** L'anonymisation conserve `usage_log` par conception.
> Si une demande d'effacement impose de retirer aussi les lignes d'usage, la
> purge par rétention (`purge-usage`, section 5) est l'outil approprié — elle
> opère par ancienneté, pas par utilisateur.

---

## 3. Gestion des clés API

### Générer une clé

> **Sécurité :** La clé brute est affichée **une seule fois** et jamais stockée
> côté serveur (on ne conserve que son hash SHA-256). Si l'utilisateur la perd,
> générer une nouvelle clé et révoquer l'ancienne.

```bash
# CLI
llmgw create-key alice --name "these-2025"
llmgw create-key alice --name "local-dev" --expires "2026-01-01"

# Sortie :
# ╔══════════════════════════════════════════════════╗
#   Clé API créée avec succès
#   Utilisateur : alice
#   Nom         : these-2025
#   Préfixe     : llmgw-xK8mP
#   Expire le   : jamais
#
#   CLEF API (à copier maintenant — non récupérable) :
#   llmgw-xK8mP3rNvQw9...
# ╚══════════════════════════════════════════════════╝

# API REST
curl -s -X POST "$GW/admin/users/alice/keys" \
  -H "Authorization: Bearer $ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"name": "these-2025"}' | python3 -m json.tool

# Réponse :
# {
#   "api_key": "llmgw-xK8mP3rNvQw9...",   ← à transmettre à Alice
#   "key_prefix": "llmgw-xK8mP",
#   "name": "these-2025",
#   "created_at": "2025-03-01T10:00:00",
#   "expires_at": null
# }
```

### Lister les clés d'un utilisateur

```bash
# CLI
llmgw list-keys alice

# Sortie :
# ┌────────────────┬────────────────┬────────┬──────────────────────┬────────────┐
# │ Préfixe        │ Nom            │ Active │ Dernière utilisation  │ Expire le  │
# ├────────────────┼────────────────┼────────┼──────────────────────┼────────────┤
# │ llmgw-xK8mP   │ these-2025     │ oui    │ 2025-03-15 14:32:00  │ jamais     │
# │ llmgw-aB2cD   │ local-dev      │ oui    │ jamais               │ 2026-01-01 │
# └────────────────┴────────────────┴────────┴──────────────────────┴────────────┘

# API REST
curl -s "$GW/admin/users/alice/keys" \
  -H "Authorization: Bearer $ADMIN_SECRET" | python3 -m json.tool
```

### Révoquer une clé

La révocation est **immédiate** : la prochaine requête avec cette clé reçoit un `401`.

```bash
# Identifier le préfixe de la clé à révoquer (depuis list-keys)
# CLI
llmgw revoke-key llmgw-xK8mP

# API REST
curl -s -X DELETE "$GW/admin/keys/llmgw-xK8mP" \
  -H "Authorization: Bearer $ADMIN_SECRET"
# → {"message": "Clé 'llmgw-xK8mP' révoquée avec succès."}
```

---

## 4. Surveillance du système

### Dashboard de monitoring (recommandé)

Le gateway embarque un dashboard graphique accessible depuis n'importe quel navigateur
sur le réseau campus. C'est le point d'entrée recommandé pour la surveillance quotidienne.

**Accès :** `https://llm.eva.univ-pau.fr/admin/dashboard`

Connexion avec l'`ADMIN_SECRET`. Le token est stocké dans `sessionStorage` et détruit
à la fermeture de l'onglet.

**Ce qui est visible en un coup d'œil :**
- Requêtes et tokens du jour (avec Δ% par rapport à hier)
- Taux d'erreur et latence sur 24h (P50/P95/P99)
- Budget VRAM : total / utilisé / disponible, avec état de chaque modèle chargé
- Graphiques par heure / par jour sur 24h, 7j ou 30j
- Tableau de tous les utilisateurs avec leur consommation et leur quota
- Métriques GPU en direct par modèle : KV cache fill, slots actifs, tokens/s

Dans le tableau des modèles, le bouton **Info** de la colonne Actions ouvre une
fiche détaillée : capabilities (dont le support des images / vision), contexte
maximum et contexte par slot, VRAM estimée, état runtime (PID, port, uptime) et
tous les paramètres `llama.cpp` du modèle.

Le dashboard se rafraîchit automatiquement toutes les **30 secondes**.

### Registre des modèles (CLI)

```bash
# Affiche la configuration VRAM et tous les modèles du registre
llmgw status
```

Sortie :

```
Configuration VRAM
  Total GPU       : 48.0 GB
  Overhead        : 2.0 GB
  Marge sécurité  : 5%
  Budget net      : 43.6 GB
  Max modèles     : 5
  Pool de ports   : 8081–8085
  Idle timeout    : 300s

┌──────────────────────────┬──────────┬────────┬──────────────────────────────────┬──────────────────────────────────────────────────────────┐
│ ID                       │ VRAM     │ Activé │ Capacités                        │ Chemin                                                   │
├──────────────────────────┼──────────┼────────┼──────────────────────────────────┼──────────────────────────────────────────────────────────┤
│ llama-3.3-70b-instruct   │ 42.0 GB  │ oui    │ text_generation, tool_calls, ... │ /models/Llama-3.3-70B-Instruct-Q4_K_M.gguf               │
│ llama-3.1-8b-instruct    │  5.5 GB  │ non    │ text_generation, streaming       │ /models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf           │
└──────────────────────────┴──────────┴────────┴──────────────────────────────────┴──────────────────────────────────────────────────────────┘

Note : L'état live (READY/LOADING) n'est visible que via GET /admin/status
```

> **Note :** Le CLI affiche l'état statique du registre (fichier `models.yaml`).
> Pour voir l'état dynamique en temps réel (READY, LOADING, usage VRAM), utiliser
> `GET /admin/status` ou le dashboard.

### État en temps réel (API)

```bash
# Statut multi-modèles avec budget VRAM
curl -s "$GW/admin/status" \
  -H "Authorization: Bearer $ADMIN_SECRET" | python3 -m json.tool

# Réponse exemple (deux modèles — l'un chargé, l'autre non) :
# {
#   "status": "ok",
#   "vram_budget": {
#     "total_gb": 48.0,
#     "overhead_gb": 2.0,
#     "used_gb": 42.0,
#     "available_gb": 1.6
#   },
#   "capacity_queue": {
#     "enabled": true,
#     "waiters": 0,
#     "max_waiters": 100,
#     "timeout_seconds": 120
#   },
#   "models": [
#     {
#       "id": "llama-3.3-70b-instruct",
#       "description": "Llama 3.3 70B Instruct, Q4_K_M",
#       "enabled": true,
#       "vram_gb": 42.0,
#       "state": "ready",
#       "pid": 18432,
#       "uptime_seconds": 3742.1,
#       "idle_seconds": 42.3,
#       "last_load_seconds": 87.4,
#       "path": "/models/Llama-3.3-70B-Instruct-Q4_K_M.gguf"
#     },
#     {
#       "id": "llama-3.1-8b-instruct",
#       "description": "Llama 3.1 8B Instruct, Q4_K_M",
#       "enabled": true,
#       "vram_gb": 5.5,
#       "state": "unloaded",
#       "pid": null,
#       "uptime_seconds": null,
#       "idle_seconds": null,
#       "last_load_seconds": null,
#       "path": "/models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
#     }
#   ]
# }
```

#### Champs de `vram_budget` selon le mode de déploiement

`GET /admin/status` a le même contrat en `CLUSTER_MODE=local` et en
`CLUSTER_MODE=cluster`. Trois champs seulement sont garantis dans les deux
modes — `total_gb`, `used_gb`, `available_gb` — les autres sont spécifiques au
mode et valent `null` quand ils ne s'appliquent pas.

| Champ | Local | Cluster |
|---|---|---|
| `total_gb` | `TOTAL_VRAM_GB` de l'hôte | Somme des VRAM physiques des nœuds **ONLINE** |
| `overhead_gb` | `VRAM_OVERHEAD_GB` | Réserve agrégée des nœuds (leur overhead **et** leur marge, en GB) |
| `safety_margin` | `VRAM_SAFETY_MARGIN` (ratio) | `null` — ratio mono-hôte, déjà agrégé en GB dans `overhead_gb` |
| `used_gb` | VRAM des modèles chargés localement | Somme des `used_vram_gb` des nœuds ONLINE |
| `available_gb` | Budget net − utilisé | Somme des `available_vram_gb` annoncés par les agents |
| `budget_net_gb` | `total_gb − overhead_gb − marge` | `used_gb + available_gb` (budget allouable annoncé) |
| `nodes` / `nodes_online` | `null` | Nœuds configurés / actuellement ONLINE |
| `gpu_used_mb_measured`, `vram_drift_mb` | Présents si une sonde `nvidia-smi` a réussi | `null` |

En cluster, `total_gb - overhead_gb == budget_net_gb` exactement ; en local il
faut en plus retirer `safety_margin × total_gb`, la marge n'étant pas incluse
dans `overhead_gb`. Un nœud offline ne contribue à aucun total : cluster entièrement
offline ⇒ tous les champs à `0.0` et `nodes_online: 0`, la route restant en 200.

Les entrées de `models` portent en plus, en cluster, le nœud d'hébergement
(`node`) et la charge live (`active_requests`) ; en local ces deux champs valent
`null`. L'URL interne du `llama-server` n'est jamais exposée ici — elle n'est
lisible que via `GET /admin/cluster`.

#### Durée du dernier chargement (`last_load_seconds`)

Chaque entrée de `models` expose aussi `last_load_seconds` : la durée du dernier
chargement **réussi** du modèle, en secondes — c'est la contrepartie « dernier
échantillon » de l'histogramme Prometheus `eva_model_load_seconds`. Elle vaut
`null` tant que le modèle n'a jamais atteint l'état `ready`.

Comparer cette durée entre deux chargements permet de distinguer un page cache
froid d'un cache chaud (variance dominante observée lors de l'enquête #24), et
sert de baseline pour mesurer un éventuel gain de checkpointing.

La sémantique exacte dépend du mode :

| Mode | Mesure |
|---|---|
| Local | Durée réelle du chargement, mesurée par la gateway du lancement du sous-processus à la santé OK — la même valeur figure désormais dans le log « llama-server prêt … en N.N s » |
| Cluster | Durée de l'appel RPC `load_model` vers le node-agent (réseau inclus), côté orchestrateur — cohérente avec l'histogramme labellisé par nœud |
| Cluster, modèle déjà chargé sur le nœud | `null` : le chargement d'origine n'a pas été observé par l'orchestrateur, aucune valeur n'est inventée |

### Surveiller la VRAM GPU

```bash
# Snapshot
nvidia-smi --query-gpu=name,memory.used,memory.free,power.draw \
  --format=csv,noheader

# Temps réel (toutes les 5s)
watch -n 5 'nvidia-smi --query-gpu=memory.used,memory.free,power.draw \
  --format=csv,noheader'

# Valeurs typiques :
# 70B seul, inactif         : ~40500 MiB utilisés, ~130W
# 70B seul, inférence       : ~41000 MiB utilisés, ~320W
# Aucun modèle chargé       : ~200 MiB,            ~28W  ← GPU libéré ✓
```

### Logs en temps réel

```bash
# Gateway (démarrages, requêtes, erreurs)
sudo journalctl -u llm-gateway -f

# llama-server (chargement, inférence — préfixé par model_id)
# Ex: [llama-3.3-70b-instruct] llama_init: warming up model...
tail -f /var/log/llm-gateway/llama-server.log

# Filtrer les erreurs uniquement
sudo journalctl -u llm-gateway -p err -f

# Dernières 24h
sudo journalctl -u llm-gateway --since "24 hours ago" | less
```

### Métriques Prometheus (intégrées à llama-server)

Lorsque des modèles sont chargés, les métriques sont accessibles par modèle
via leur port respectif (localement uniquement) :

```bash
# Métriques brutes (format Prometheus) — accès local uniquement
# Port attribué dynamiquement (8081 = premier modèle chargé, 8082 = second, etc.)
curl http://127.0.0.1:8081/metrics

# Métriques intéressantes :
# llamacpp:prompt_tokens_total        — tokens en entrée traités
# llamacpp:tokens_predicted_total     — tokens générés
# llamacpp:tokens_per_second          — débit en génération
# llamacpp:kv_cache_usage_ratio       — taux d'utilisation du KV cache (0–1)
# llamacpp:requests_processing        — requêtes en cours
# llamacpp:requests_deferred          — requêtes en attente de slot
```

Ces métriques sont également disponibles en JSON via le gateway (indexées par model_id),
ce qui évite d'avoir à ouvrir un accès direct à llama-server :

```bash
curl -s "$GW/admin/metrics/llama" \
  -H "Authorization: Bearer $ADMIN_SECRET" | python3 -m json.tool
# Exemple de réponse avec deux modèles chargés :
# {
#   "llama-3.3-70b-instruct": {
#     "kv_cache_usage_ratio": 0.12,
#     "kv_cache_tokens": 3932,
#     "requests_processing": 1.0,
#     "requests_deferred": 0.0,
#     "tokens_per_second": 18.4,
#     "prompt_tokens_total": 45230.0,
#     "tokens_predicted_total": 12880.0
#   },
#   "llama-3.1-8b-instruct": {
#     "kv_cache_usage_ratio": 0.05,
#     ...
#   }
# }
# Retourne {} si aucun modèle n'est chargé
```

### Exposition Prometheus texte (`/admin/metrics/prometheus`)

Pour un scraping par un Prometheus mono-binaire local, la gateway expose un
endpoint au **format texte Prometheus** (version 0.0.4), protégé par
`ADMIN_SECRET` comme les autres routes `/admin/*` :

```bash
curl -s "$GW/admin/metrics/prometheus" \
  -H "Authorization: Bearer $ADMIN_SECRET"
```

Métriques exposées (noms exacts) :

| Métrique | Type | Labels | Description |
|----------|------|--------|-------------|
| `eva_requests_total` | counter | `model`, `status` | Requêtes par modèle et code HTTP (fenêtre 24h) |
| `eva_tokens_total` | counter | `model`, `type` (`prompt`/`completion`) | Tokens par modèle et type (fenêtre 24h) |
| `eva_request_latency_seconds` | gauge | `quantile` (0.5/0.95/0.99) | Percentiles de latence (fenêtre 7j) |
| `eva_vram_used_gb` / `eva_vram_total_gb` / `eva_vram_available_gb` | gauge | — | Budget VRAM comptabilisé — mêmes champs que `vram_budget` de `/admin/status` ; en cluster, `eva_vram_total_gb` est la VRAM **physique** des nœuds ONLINE (le budget allouable est `budget_net_gb`) |
| `eva_models_loaded` | gauge | — | Nombre de modèles à l'état `ready` |
| `eva_llama_kv_cache_usage_ratio` | gauge | `model` (+ `node` en cluster) | Occupation du KV cache (0–1) |
| `eva_llama_tokens_per_second` | gauge | `model` (+ `node`) | Débit de génération |
| `eva_llama_requests_processing` | gauge | `model` (+ `node`) | Requêtes en cours d'inférence |
| `eva_llama_requests_deferred` | gauge | `model` (+ `node`) | Requêtes en attente de slot |

Robuste par construction : chaque source indisponible (aucun modèle, pas de
`nvidia-smi`, mode cluster, DB vide) est silencieusement omise, jamais de 500.
Ne divulgue aucun contenu de prompt. Voir [observability.md](observability.md)
pour un exemple de job de scrape et des règles d'alerte.

### Readiness `/ready` (distincte de `/health`)

- **`GET /health`** (non authentifié) : liveness — le process répond. Utilisé
  par nginx et systemd. Renvoie les modèles chargés et la VRAM.
- **`GET /ready`** (non authentifié) : readiness — la gateway peut **servir** au
  moins une requête d'inférence. Renvoie `200` si au moins un modèle est déjà
  `ready`, **ou** s'il reste de la capacité VRAM (mode local) / au moins un nœud
  online (mode cluster). Sinon `503` avec une `reason`
  (`no_model_ready_and_no_capacity` ou `all_nodes_offline`). Le corps ne
  divulgue aucune infra sensible (ni chemin fichier, ni URL).

Utiliser `/ready` pour l'orchestration/supervision (mise en/hors rotation) et
`/health` pour le simple redémarrage automatique. Voir
[observability.md](observability.md).

---

## 5. Rapports d'usage

### Rapport mensuel agrégé

```bash
# CLI — résumé du mois courant
llmgw usage-report --month 2025-03 --summary

# Sortie :
# ┌──────────────┬──────────┬───────────────┬────────────────┬─────────────┬─────────────────┐
# │ Utilisateur  │ Requêtes │ Tokens prompt │ Tokens réponse │ Total tokens│ Durée moy. (ms) │
# ├──────────────┼──────────┼───────────────┼────────────────┼─────────────┼─────────────────┤
# │ alice        │      342 │       458,230 │        892,441 │   1,350,671 │            4230 │
# │ bob          │       87 │        92,100 │        201,338 │     293,438 │            3890 │
# │ carol        │       15 │        18,200 │         41,022 │      59,222 │            4100 │
# └──────────────┴──────────┴───────────────┴────────────────┴─────────────┴─────────────────┘

# API REST — résumé mars 2025
curl -s "$GW/admin/usage/summary?from_date=2025-03-01&to_date=2025-03-31" \
  -H "Authorization: Bearer $ADMIN_SECRET" | python3 -m json.tool
```

### Journal détaillé (une ligne par requête)

```bash
# CLI — détail d'un utilisateur sur une période
llmgw usage-report --user alice --from 2025-03-01 --to 2025-03-07

# API REST — 100 dernières requêtes
curl -s "$GW/admin/usage?limit=100" \
  -H "Authorization: Bearer $ADMIN_SECRET" | python3 -m json.tool

# API REST — filtrer par utilisateur et date
curl -s "$GW/admin/usage?username=alice&from_date=2025-03-01&to_date=2025-03-31" \
  -H "Authorization: Bearer $ADMIN_SECRET" | python3 -m json.tool
```

### Exporter pour un tableur

```bash
# Exporter le résumé mensuel en CSV
curl -s "$GW/admin/usage/summary?from_date=2025-03-01&to_date=2025-03-31" \
  -H "Authorization: Bearer $ADMIN_SECRET" \
  | python3 -c "
import json, sys, csv
data = json.load(sys.stdin)
if not data: sys.exit()
w = csv.DictWriter(sys.stdout, fieldnames=data[0].keys())
w.writeheader()
w.writerows(data)
" > usage-mars-2025.csv
```

### Rétention / purge du journal d'usage

Le journal `usage_log` grossit indéfiniment. La purge est **manuelle et opt-in** :
aucune suppression n'est déclenchée automatiquement. Utilisez la commande CLI
`purge-usage` pour supprimer les entrées plus anciennes que N jours, suivie d'un
`VACUUM` complet qui restitue l'espace disque.

```bash
# Supprimer les entrées usage_log de plus de 365 jours
llmgw purge-usage --older-than-days 365
# → « Purge terminée : N entrée(s) usage_log supprimée(s) (> 365 jours). »
```

> **Attention :** le `VACUUM` verrouille la base pendant son exécution. Exécutez
> cette commande **hors ligne** (fenêtre de maintenance), pas pendant les pics de
> trafic. La suppression est définitive — exportez d'abord les rapports à archiver.
>
> La rétention n'affecte que l'historique de reporting. Les quotas glissants
> (30 jours) ne portent que sur des fenêtres récentes ; conservez donc au moins
> ~30 jours de journal si vous purgez agressivement.

---

## 6. Contrôle des modèles

### Voir l'état de tous les modèles

```bash
# Via API (état live)
curl -s "$GW/admin/models" \
  -H "Authorization: Bearer $ADMIN_SECRET" | python3 -m json.tool
# → liste tous les modèles (registre + état READY/LOADING/UNLOADED)

# Via CLI (registre uniquement, sans état live)
llmgw status
```

### Pré-charger un modèle

Par défaut, les modèles chargent à la première requête. Pour les pré-charger
afin d'éliminer le délai de la première requête :

```bash
# Pré-charger le modèle 8B (appel synchrone : il ne répond qu'une fois le
# modèle réellement chargé et prêt à servir)
curl -s -X POST "$GW/admin/models/llama-3.1-8b-instruct/load" \
  -H "Authorization: Bearer $ADMIN_SECRET"
# → {"message": "Modèle 'llama-3.1-8b-instruct' chargé et prêt."}

# Vérifier l'état live (le modèle apparaît en state "ready")
curl -s "$GW/admin/models" \
  -H "Authorization: Bearer $ADMIN_SECRET" | python3 -m json.tool
```

### Décharger un modèle spécifique

Utile pour libérer de la VRAM pour un autre modèle, ou forcer le rechargement
d'un modèle dont les paramètres ont changé.

```bash
# Décharger le 70B
curl -s -X POST "$GW/admin/models/llama-3.3-70b-instruct/unload" \
  -H "Authorization: Bearer $ADMIN_SECRET"
# → {"message": "Modèle 'llama-3.3-70b-instruct' déchargé. VRAM libérée."}

# Vérifier la VRAM libérée
nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits
# → ~200  (MiB)
```

### Déchargement et requêtes actives (409)

**Invariant : un modèle qui traite une requête active n'est jamais déchargé en
silence.** Cela vaut pour l'éviction LRU, pour l'arrêt de la gateway, et depuis
COR-004 pour *toutes* les opérations admin qui déchargent un modèle :

| Route | Décharge le modèle |
|-------|--------------------|
| `POST /admin/models/{id}/unload` | toujours |
| `DELETE /admin/models/{id}` | avant la suppression du registre |
| `PATCH /admin/models/{id}` avec `enabled: false` | oui |
| `PATCH /admin/models/{id}` avec `llama_params` | oui (hot-reload) |
| `POST /admin/unload` | tous les modèles chargés |

Déroulé d'une de ces opérations :

1. **Quarantaine** — le modèle n'admet plus aucune *nouvelle* requête (les
   clients reçoivent un `503` temporaire, « déchargement administratif en
   cours »). Sans cela, un flux continu de requêtes empêcherait le drain de
   converger.
2. **Drain borné** — la gateway attend la fin des requêtes déjà en cours, au
   maximum `ADMIN_UNLOAD_DRAIN_TIMEOUT_SECONDS` (défaut **5 s**). Retour immédiat
   si le modèle est inactif : le cas courant ne coûte rien.
3. **Décision** :
   - drain terminé → déchargement normal, `200` ;
   - requêtes encore actives → **`409 Conflict`**, *rien n'est modifié* : le
     modèle reste chargé, le registre reste intact (jamais de `enabled: false`
     persisté sur un modèle qui continue de servir), la quarantaine est levée et
     le modèle redevient immédiatement utilisable.

```bash
# Modèle occupé par un stream en cours
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  "$GW/admin/models/llama-3.3-70b-instruct/unload" \
  -H "Authorization: Bearer $ADMIN_SECRET"
# → 409
# corps : {"detail": "Le modèle 'llama-3.3-70b-instruct' traite encore 2 requête(s)
#           active(s) après 5s de drain — déchargement refusé pour ne pas
#           interrompre les générations en cours. Réessayez plus tard, ou passez
#           force=true pour interrompre explicitement les requêtes actives."}
```

Un `503` sur ces routes garde son sens habituel : échec technique du
déchargement (en cluster, un agent qui n'a pas confirmé), pas un conflit.

#### Forcer (`?force=true`)

Le forçage est **opt-in, jamais la valeur par défaut**. Il existe pour les cas où
une requête ne se termine jamais (client disparu, `llama-server` bloqué) et où le
modèle serait sinon indéchargeable :

```bash
# Interrompt les générations en cours — à n'utiliser qu'en connaissance de cause
curl -s -X POST "$GW/admin/models/llama-3.3-70b-instruct/unload?force=true" \
  -H "Authorization: Bearer $ADMIN_SECRET"
```

`force=true` est accepté par `POST /admin/models/{id}/unload`,
`DELETE /admin/models/{id}` et `PATCH /admin/models/{id}`. Sur un modèle inactif
il n'a aucun effet ; chaque forçage **effectif** (requêtes réellement
interrompues) est tracé par un log `CRITICAL` indiquant leur nombre. Il n'existe
**pas** de forçage global sur `POST /admin/unload` : décharger modèle par modèle.

En **mode cluster**, le forçage n'existe pas : les node-agents refusent tout
modèle avec des requêtes actives, et l'orchestrateur ne tue pas un
`llama-server` qu'il ne possède pas. Sur un modèle occupé, `?force=true` y
répond donc `409` en précisant que le forçage est indisponible — le paramètre
n'est jamais ignoré en silence. Sur un modèle inactif, il n'a aucun effet et le
déchargement réussit normalement.

Réglages associés (`gateway/.env`) :

| Variable | Défaut | Rôle |
|----------|--------|------|
| `ADMIN_UNLOAD_DRAIN_TIMEOUT_SECONDS` | `5` | Attente max des requêtes actives sur une opération admin. `0` = refus immédiat si occupé. |
| `SHUTDOWN_DRAIN_TIMEOUT_SECONDS` | `25` | Attente max au SIGTERM. Le shutdown, lui, **force** après ce délai (la VRAM et les ports doivent être libérés avant que systemd ne tue le processus). |
| `SHUTDOWN_DRAIN_POLL_SECONDS` | `0.2` | Granularité de poll des deux drains. |

### Décharger tous les modèles

```bash
# Décharger tous les modèles chargés sans arrêter la gateway
curl -s -X POST "$GW/admin/unload" \
  -H "Authorization: Bearer $ADMIN_SECRET"
# → {"message": "Tous les modèles déchargés. VRAM entièrement libérée."}
```

Cette route répond `409` si une génération est encore active après le drain — et
dans ce cas **aucun** modèle n'est déchargé (pas de purge partielle). Elle répond
`503` si le déchargement n'a pas pu être confirmé.

En cluster, l'orchestrateur conserve ses clients et son heartbeat après cette
action : il peut recharger un modèle à la requête suivante. Il n'annonce jamais
une libération partielle comme réussie.

### Activer / désactiver un modèle du registre

Désactiver un modèle le rend **invisible aux clients** (`GET /v1/models` ne le liste plus)
et les requêtes vers cet ID reçoivent un `403`. Si le modèle est actuellement chargé,
il est automatiquement déchargé.

> Le PATCH répond `409` et **ne modifie pas le registre** si le modèle traite
> encore des requêtes après le drain — le modèle reste `enabled: true` et
> continue de servir. Voir
> [Déchargement et requêtes actives](#déchargement-et-requêtes-actives-409).

```bash
# Désactiver le modèle 8B (ex: fichier .gguf absent)
curl -s -X PATCH "$GW/admin/models/llama-3.1-8b-instruct" \
  -H "Authorization: Bearer $ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'

# Réactiver
curl -s -X PATCH "$GW/admin/models/llama-3.1-8b-instruct" \
  -H "Authorization: Bearer $ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'
```

### Modifier les paramètres llama-server (hot-reload)

Il est possible de modifier **à chaud** les paramètres de lancement d'un modèle
(`ctx_size`, `parallel`, `cpu_moe`, etc.) sans redémarrer le gateway.

Le PATCH déclenche un **hot-reload** : le modèle est déchargé (sa VRAM est
libérée), le registre est mis à jour, et le prochain appel relancera
llama-server avec les nouveaux paramètres.

> Comme tout déchargement admin, le hot-reload attend la fin des requêtes en
> cours puis répond `409` sans rien modifier si elles n'ont pas terminé. Les
> anciens paramètres restent alors en vigueur. Voir
> [Déchargement et requêtes actives](#déchargement-et-requêtes-actives-409).

> **`llama_params` utilise une sémantique de remplacement complet.** Tous les champs
> doivent être fournis — il n'y a pas de merge partiel. Récupérez les valeurs actuelles
> via `GET /admin/models` (liste) avant de faire le PATCH.

```bash
# Récupérer la config actuelle d'un modèle (repérer l'entrée dans la liste)
curl -s "$GW/admin/models" \
  -H "Authorization: Bearer $ADMIN_SECRET" | python3 -m json.tool

# Corriger un modèle MoE qui saturait la VRAM (ajout de cpu_moe)
curl -s -X PATCH "$GW/admin/models/qwen3.5-9b-q5_k_m" \
  -H "Authorization: Bearer $ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "llama_params": {
      "n_gpu_layers": 999,
      "ctx_size": 32768,
      "parallel": 4,
      "batch_size": 2048,
      "ubatch_size": 512,
      "cache_type_k": "q8_0",
      "cache_type_v": "q8_0",
      "flash_attn": true,
      "threads": 8,
      "threads_http": 4,
      "cpu_moe": true
    }
  }'

# Réduire la fenêtre de contexte pour libérer de la VRAM KV
curl -s -X PATCH "$GW/admin/models/llama-3.3-70b-instruct" \
  -H "Authorization: Bearer $ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "llama_params": {
      "n_gpu_layers": 999,
      "ctx_size": 16384,
      "parallel": 4,
      "batch_size": 4096,
      "ubatch_size": 512,
      "cache_type_k": "q8_0",
      "cache_type_v": "q8_0",
      "flash_attn": true,
      "threads": 8,
      "threads_http": 4,
      "cpu_moe": false
    }
  }'
```

**Champs disponibles dans `llama_params` :**

| Champ | Type | Défaut | Description |
|-------|------|--------|-------------|
| `n_gpu_layers` | int | 999 | Couches GPU (999 = tout en GPU) |
| `ctx_size` | int | 32768 | Contexte total (somme de tous les slots) |
| `parallel` | int | 4 | Slots d'inférence concurrents |
| `batch_size` | int | 4096 | Taille de batch logique |
| `ubatch_size` | int | 512 | Taille de micro-batch physique |
| `cache_type_k` | str | `q8_0` | Quantisation du KV cache (K) |
| `cache_type_v` | str | `q8_0` | Quantisation du KV cache (V) |
| `flash_attn` | bool | true | Flash Attention 2 (Ada Lovelace) |
| `threads` | int | 8 | Threads CPU pour le calcul |
| `threads_http` | int | 4 | Threads HTTP de llama-server |
| `cpu_moe` | bool | false | **MoE uniquement** — déporte les experts FFN sur RAM CPU |

> **`cpu_moe: true`** est indispensable pour les modèles MoE (Mixtral, Qwen-MoE, MiniMax…)
> quand les experts ne tiennent pas en VRAM. Sans ce flag, llama-server alloue tous les
> experts en GPU → CUDA OOM → exit code 1 immédiat. Le `vram_gb` déclaré dans le registre
> doit correspondre à l'utilisation **avec** `cpu_moe` (uniquement les couches attention +
> embeddings restent en GPU).

#### Speculative decoding MTP (`speculative`) — YAML uniquement

Le bloc `speculative` active le **Multi-Token Prediction** (tête intégrée au GGUF,
sans modèle draft séparé). Il se déclare **par édition manuelle de `models.yaml`** —
il n'est pas encore exposé via l'API admin (pas de champ `speculative` dans le corps
POST/PATCH). Après édition, redémarrer le gateway (le registre relit `models.yaml` au
démarrage) ; le modèle sera lancé avec les nouveaux flags `--spec-*` à son prochain
chargement (au besoin `POST /admin/models/{id}/unload` puis `/load`).

```yaml
    speculative:
      type: mtp        # seul type supporté
      draft_max: 16    # --spec-draft-n-max : nb de tokens draftés (défaut 16)
      draft_min: 0     # --spec-draft-n-min (optionnel, défaut 0)
      draft_p_min: 0.0 # --spec-draft-p-min (optionnel, défaut 0.0)
```

| Champ | Type | Défaut | Description |
|-------|------|--------|-------------|
| `type` | str | `mtp` | Type de speculative (seul `mtp` supporté) |
| `draft_max` | int | 16 | Nb de tokens draftés par étape (`--spec-draft-n-max`) |
| `draft_min` | int | 0 | Minimum de draft tokens (`--spec-draft-n-min`) |
| `draft_p_min` | float | 0.0 | Proba min d'acceptation greedy (`--spec-draft-p-min`) |

> **VRAM inchangée** : la tête MTP est dans le même GGUF, donc `vram_gb` reste
> l'empreinte du modèle seul (aucun second modèle à charger).
> **Prérequis** : le binaire `llama-server` doit supporter `--spec-type`
> (vérifier avec `llama-server --help | grep spec`). En cluster, c'est le binaire
> de chaque **node** qui doit le supporter. Le bloc `speculative` est visible dans
> `GET /admin/status` une fois le modèle chargé.

### Ce qu'une mutation admin fait à `models.yaml` (COR-020)

`POST /admin/models`, `PATCH /admin/models/{id}` et `DELETE /admin/models/{id}`
persistent dans `models.yaml`. Depuis COR-020, la persistance **retouche le
texte** du fichier au lieu de le resérialiser : les commentaires d'exploitation
— l'en-tête du fichier livré, les notes de fin de ligne, les procédures de
réactivation — sont de la documentation, pas du bruit.

**Ce qui est préservé**

| Cas | Ce que fait la gateway |
|-----|------------------------|
| Ajout (`POST`) | L'entrée est **ajoutée en fin de document**. Aucun octet de l'existant n'est réécrit. |
| Mise à jour (`PATCH`) | Seules les **lignes de champ qui changent** sont retouchées, y compris à l'intérieur de `llama_params`. Le commentaire de fin de ligne survit (son alignement est normalisé à deux espaces). |
| Suppression (`DELETE`) | Seul le **bloc de l'entrée** est retiré : en-tête du fichier et entrées voisines intacts. |
| Mutation sans effet | Rien n'est écrit, aucune sauvegarde n'est produite. |

**Ce qui n'est pas préservé**

- Les commentaires **internes au bloc supprimé** par un `DELETE` : ils décrivent
  l'entrée qui disparaît. La sauvegarde horodatée en garde une copie.
- Un commentaire placé **après le dernier champ** d'une entrée supprimée est
  conservé dans le fichier et devient orphelin — un commentaire orphelin se
  relit, un commentaire effacé ne se retrouve pas.
- Une entrée qui n'a pas de bloc `llama_params` et dont on modifie un paramètre
  reçoit un bloc **complet** (paramètres effectifs), pas un bloc partiel.

**Ce qui est refusé** — HTTP 422, message explicite, *rien n'est écrit et l'état
mémoire de la gateway est restauré* :

- `models.yaml` n'est plus lisible, ou n'est plus un registre (clé `models`
  absente, valeur qui n'est pas une liste) ;
- une entrée n'est pas identifiable avec certitude dans le texte : registre en
  style « flow » (`models: [{...}]`), identifiant présent plusieurs fois ;
- le texte candidat, reparsé, ne rend **pas** le document attendu — par exemple
  une clé dupliquée dans une entrée, où YAML retient la dernière ;
- le sens de `models.yaml` diverge du snapshot chargé par la gateway, scalaire
  ou non scalaire, entrée ajoutée ou retirée : c'est une édition concurrente et
  l'écraser avec l'ancien état mémoire serait une perte de données. Une
  modification de **commentaire seulement** reste autorisée et préservée.

Ces refus se lisent de la **même façon sur les trois verbes** (COR-029) :
`POST`, `PATCH` et `DELETE` répondent tous `422` avec le message de refus dans
`detail`. C'est ce message qui dit quoi corriger ; jusqu'à COR-029, `DELETE` le
perdait dans un `500` au corps générique. Un identifiant inconnu reste, lui, un
`404` — ce n'est pas la même panne et elle ne se répare pas pareil.

Dans tous ces cas, corrigez le fichier à la main puis redémarrez la gateway (le
registre relit `models.yaml` au démarrage). La gateway ne se rabat **jamais** sur
une réécriture globale.

**Sauvegardes et durabilité**

- Avant chaque écriture, une copie `models.yaml.pre-admin.<horodatage>.bak` est
  produite dans le même répertoire, avec le mode du fichier d'origine.
- Ces sauvegardes sont **bornées à 5** (les plus anciennes sont purgées).
  Contrairement aux `*.pre-migration.*.bak` des migrations SQLite, elles ne
  s'accumulent pas. Le motif est distinct de celui du bootstrap
  (`*.pre-bootstrap.*.bak`) : les deux jeux ne se purgent pas l'un l'autre.
- L'écriture est atomique : fichier temporaire dans le même répertoire, `fsync`,
  validation par le chargeur du registre lui-même, `os.replace`, puis `fsync`
  du **répertoire parent** — sans lequel le renommage n'est pas durable.
- Le **mode** du fichier est réappliqué (un `models.yaml` en 0640 le reste), et
  le **propriétaire et le groupe** sont rétablis après le renommage. Si le
  service n'a pas le droit de le faire, un avertissement est journalisé : vérifiez
  alors `ls -l` sur le fichier.

### Enregistrer un nouveau modèle (sans redémarrage)

```bash
# 1. S'assurer que le fichier .gguf est présent
ls -lh /models/Qwen2.5-32B-Instruct-Q4_K_M.gguf

# 2. Enregistrer via API (persiste dans models.yaml)
curl -s -X POST "$GW/admin/models" \
  -H "Authorization: Bearer $ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "qwen2.5-32b-instruct",
    "path": "/models/Qwen2.5-32B-Instruct-Q4_K_M.gguf",
    "description": "Qwen 2.5 32B Instruct Q4_K_M",
    "vram_gb": 20.0,
    "enabled": true,
    "capabilities": ["text_generation", "streaming"],
    "llama_params": {
      "n_gpu_layers": 999,
      "ctx_size": 32768,
      "parallel": 6,
      "batch_size": 2048,
      "ubatch_size": 512,
      "cache_type_k": "q8_0",
      "cache_type_v": "q8_0",
      "flash_attn": true,
      "threads": 6,
      "threads_http": 3
    }
  }' | python3 -m json.tool

# 3. Le modèle est maintenant visible dans GET /v1/models
#    Il chargera automatiquement à la première requête qui le cible
```

**Validations appliquées lors de l'enregistrement :**
- L'`id` doit correspondre à `^[a-z0-9][a-z0-9._-]{0,62}$` (63 caractères max)
- Le `path` doit être absolu et se terminer par `.gguf`
- Le fichier `.gguf` doit exister sur disque
- Si `ALLOWED_MODEL_DIRS` est configuré, le chemin doit être sous ces répertoires
- `vram_gb` doit être strictement supérieur à 0 et au plus le budget VRAM net

> **Modèles vision (`mmproj_path`) :** l'API `POST /admin/models` n'accepte **pas**
> le champ `mmproj_path` (il est absent du corps de requête et serait ignoré). Pour
> enregistrer un modèle vision avec son projecteur multimodal, il faut définir
> `mmproj_path` directement dans `models.yaml` (édition YAML + reload du registre),
> et **non** via l'API d'enregistrement.

**Pour un modèle vision**, ajouter l'entrée dans `models.yaml` avec `mmproj_path` :

```yaml
  - id: "llava-7b"
    path: "/models/llava-v1.6-mistral-7b-Q4_K_M.gguf"
    mmproj_path: "/models/llava-v1.6-mistral-7b-mmproj-f16.gguf"
    description: "LLaVA 1.6 Mistral 7B — vision + texte"
    vram_gb: 6.0
    enabled: true
    capabilities: ["text_generation", "vision", "streaming"]
    llama_params:
      n_gpu_layers: 999
      ctx_size: 8192
      parallel: 4
      batch_size: 2048
      ubatch_size: 512
      cache_type_k: "q8_0"
      cache_type_v: "q8_0"
      flash_attn: true
      threads: 4
      threads_http: 2
```

> **Important :** `mmproj_path` est **obligatoire** en pratique si `vision` est dans
> `capabilities`. Sans lui, llama-server retourne HTTP 500 sur toute requête avec image.
> La gateway émet un warning dans les logs au démarrage si ce champ est absent.

### Supprimer un modèle du registre

```bash
# Un modèle chargé ne peut pas être supprimé — le décharger d'abord
curl -s -X POST "$GW/admin/models/qwen2.5-32b-instruct/unload" \
  -H "Authorization: Bearer $ADMIN_SECRET"

# Puis supprimer
curl -s -X DELETE "$GW/admin/models/qwen2.5-32b-instruct" \
  -H "Authorization: Bearer $ADMIN_SECRET"
# → {"message": "Modèle 'qwen2.5-32b-instruct' supprimé du registre."}
```

`DELETE` décharge lui-même le modèle si nécessaire : l'étape `unload` ci-dessus
n'est qu'une commodité. En revanche, si le modèle traite encore des requêtes, le
`DELETE` répond `409` et **l'entrée reste dans le registre** — jamais de
suppression partielle. Voir
[Déchargement et requêtes actives](#déchargement-et-requêtes-actives-409).

### Redémarrer le service

```bash
# En local : arrêt propre et déchargement des modèles.
# En cluster : arrêt de l'orchestrateur, modèles distants conservés chauds.
sudo systemctl restart llm-gateway

# Vérifier le redémarrage
sudo journalctl -u llm-gateway -f --since now
```

---

## 7. Référence API REST admin

Toutes les routes nécessitent : `Authorization: Bearer <ADMIN_SECRET>`
Toutes les routes `/admin/*` sont restreintes aux IP campus par nginx.

### Interface web

| Méthode | Route | Description |
|---------|-------|-------------|
| `GET` | `/admin/dashboard` | Dashboard de monitoring (HTML, navigateur) |

### Utilisateurs

| Méthode | Route | Description |
|---------|-------|-------------|
| `POST` | `/admin/users` | Créer un utilisateur |
| `GET` | `/admin/users` | Lister tous les utilisateurs |
| `GET` | `/admin/users/{username}` | Détail d'un utilisateur |
| `PATCH` | `/admin/users/{username}` | Modifier un utilisateur |
| `DELETE` | `/admin/users/{username}` | **Anonymiser** un utilisateur — RGPD, **irréversible**. La ligne et l'historique `usage_log` sont conservés, les données personnelles effacées, les clés révoquées ([détail](#anonymiser-un-utilisateur-droit-à-leffacement-rgpd)) |

### Clés API

| Méthode | Route | Description |
|---------|-------|-------------|
| `POST` | `/admin/users/{username}/keys` | Générer une clé (retourne la clé brute une seule fois) |
| `GET` | `/admin/users/{username}/keys` | Lister les clés (sans valeur brute) |
| `DELETE` | `/admin/keys/{key_prefix}` | Révoquer une clé |

### Registre des modèles

| Méthode | Route | Description |
|---------|-------|-------------|
| `GET` | `/admin/models` | Lister tous les modèles (registre + état live) |
| `POST` | `/admin/models` | Enregistrer un nouveau modèle (persiste dans models.yaml) |
| `PATCH` | `/admin/models/{model_id}` | Modifier un modèle — `enabled`, `vram_gb`, `description`, `llama_params` (hot-reload) — `?force=true` optionnel |
| `DELETE` | `/admin/models/{model_id}` | Supprimer un modèle (déchargé au préalable) — `?force=true` optionnel |
| `POST` | `/admin/models/{model_id}/load` | Pré-charger un modèle en VRAM |
| `POST` | `/admin/models/{model_id}/unload` | Décharger un modèle spécifique — `?force=true` optionnel |

Les routes marquées `?force=true` déchargent le modèle : elles répondent `409` si
une génération est encore en cours. Voir
[Déchargement et requêtes actives](#déchargement-et-requêtes-actives-409).

**Exemple — lister les modèles avec état live :**

```bash
curl -s "$GW/admin/models" \
  -H "Authorization: Bearer $ADMIN_SECRET" | python3 -m json.tool
# [
#   {
#     "id": "llama-3.3-70b-instruct",
#     "description": "Llama 3.3 70B Instruct, Q4_K_M",
#     "enabled": true,
#     "vram_gb": 42.0,
#     "capabilities": ["text_generation", "tool_calls", "streaming"],
#     "state": "ready",
#     "pid": 18432,
#     "uptime_seconds": 3742.1,
#     "idle_seconds": 42.3,
#     "path": "/models/Llama-3.3-70B-Instruct-Q4_K_M.gguf"
#   },
#   {
#     "id": "llama-3.1-8b-instruct",
#     ...
#     "state": "unloaded",
#     "pid": null
#   }
# ]
```

### Système

| Méthode | Route | Description |
|---------|-------|-------------|
| `GET` | `/admin/status` | Budget VRAM + état de tous les modèles |
| `POST` | `/admin/unload` | Décharger tous les modèles chargés (`409` si une génération est active) |

### Endpoints d'inférence exposés aux utilisateurs

Pour référence — ces routes sont accessibles par les utilisateurs avec leur clé API (Bearer token).
Elles sont toutes soumises au rate limiting et à la gestion VRAM automatique.

| Méthode | Route | Description |
|---------|-------|-------------|
| `GET` | `/v1/models` | Liste les modèles activés |
| `GET` | `/v1/capacity` | État minimal de la queue VRAM (auth utilisateur, sans détail infra sensible) |
| `POST` | `/v1/chat/completions` | Chat completion (OpenAI-compatible, streaming supporté) |
| `POST` | `/v1/completions` | Legacy text completion (OpenAI-compatible) |
| `POST` | `/completion` | Completion native llama.cpp — prend un champ `prompt` string |
| `POST` | `/v1/completion` | Alias de `/completion` |
| `POST` | `/v1/tokenize` | Tokenise un texte — retourne les token IDs |
| `POST` | `/v1/detokenize` | Reconstruit du texte depuis des token IDs |
| `GET` | `/health` | Health check (non authentifié) |

> **Paramètres avancés llama.cpp :** `/v1/chat/completions` et `/completion` acceptent tous les
> paramètres de sampling natifs llama.cpp directement dans le body (`mirostat`, `dry_multiplier`,
> `repeat_last_n`, `xtc_*`, etc.). La gateway les transmet sans filtrage vers llama-server.
> Voir le guide utilisateur `docs/api.md` sections 6.2 et 7 pour les détails et exemples.

**Exemple — vue d'ensemble du statut système :**

```bash
curl -s "$GW/admin/status" \
  -H "Authorization: Bearer $ADMIN_SECRET" | python3 -m json.tool
# {
#   "status": "ok",
#   "vram_budget": {
#     "total_gb": 48.0,
#     "overhead_gb": 2.0,
#     "used_gb": 42.0,
#     "available_gb": 1.6
#   },
#   "capacity_queue": { "enabled": true, "waiters": 0, "max_waiters": 100, "timeout_seconds": 120 },
#   "models": [
#     { "id": "llama-3.3-70b-instruct", "state": "ready", "vram_gb": 42.0, "last_load_seconds": 87.4, ... },
#     { "id": "llama-3.1-8b-instruct", "state": "unloaded", ..., "last_load_seconds": null }
#   ]
# }
```

### Usage (données brutes)

| Méthode | Route | Paramètres |
|---------|-------|------------|
| `GET` | `/admin/usage` | `username`, `from_date`, `to_date`, `limit` |
| `GET` | `/admin/usage/summary` | `from_date`, `to_date` |

### Métriques dashboard

| Méthode | Route | Paramètres | Description |
|---------|-------|------------|-------------|
| `GET` | `/admin/metrics/overview` | — | KPIs globaux, latence (P50/P95/P99), état multi-modèles |
| `GET` | `/admin/metrics/timeseries` | `period=24h\|7d\|30d` | Série temporelle (requêtes, tokens, erreurs, latence) |
| `GET` | `/admin/metrics/users` | `period=7d\|30d\|90d` | Statistiques par utilisateur avec quota |
| `GET` | `/admin/metrics/status-codes` | `period=24h\|7d\|30d` | Distribution des codes HTTP |
| `GET` | `/admin/metrics/llama` | — | Métriques llama-server en direct par model_id — retourne `{}` si aucun chargé |

**Exemple — vue d'ensemble KPI :**

```bash
curl -s "$GW/admin/metrics/overview" \
  -H "Authorization: Bearer $ADMIN_SECRET"
# {
#   "requests_today": 142,
#   "tokens_today": 284031,
#   "active_users_7d": 5,
#   "avg_latency_24h_ms": 3821.4,
#   "error_rate_24h": 0.007,
#   "latency_p50_ms": 2940.0,
#   "latency_p95_ms": 8210.0,
#   "models": {
#     "status": "ok",
#     "vram_budget": { "total_gb": 48.0, "used_gb": 42.0, ... },
#     "models": [...]
#   }
# }
```

---

## 8. Diagnostic préflight — `doctor`

`doctor` inspecte l'**hôte** et la **configuration** et dit, avant tout
démarrage, si la gateway pourra servir. Il ne contacte aucun service, ne charge
aucun modèle et fonctionne donc **avant le premier `systemctl start`** comme
pendant un incident.

Quatre usages :

- manuellement, après `install.sh` et avant d'activer le service ;
- depuis `install.sh`, avant activation ;
- depuis `update.sh`, avant et après bascule de version ;
- lors d'un incident, pour distinguer un problème d'hôte d'un problème de code.

À ne pas confondre avec les deux autres sondes :

| Outil | Question à laquelle il répond | Service requis |
|---|---|---|
| `GET /health` | Le process répond-il ? | oui |
| `GET /ready` | La gateway peut-elle servir maintenant ? | oui |
| `doctor` | L'hôte et la configuration sont-ils corrects ? | **non** |

### Usage

```bash
# Depuis le répertoire d'installation, avec le venv du service
cd /opt/llm-gateway

# Diagnostic complet, sortie humaine
sudo venv/bin/python cli.py doctor

# Sortie JSON (schéma stable) pour un script de déploiement
sudo venv/bin/python cli.py doctor --json

# Cibler explicitement des artefacts (utile en staging ou hors installation standard)
sudo venv/bin/python cli.py doctor \
    --env-file /etc/llm-gateway/env \
    --nginx-conf /etc/nginx/sites-available/llm-gateway \
    --systemd-unit /etc/systemd/system/llm-gateway.service

# Vérifier en plus l'intégrité SHA-256 des GGUF — COÛTEUX (lecture intégrale)
sudo venv/bin/python cli.py doctor --verify-hashes

# Traiter les avertissements comme bloquants (recette de mise en production)
sudo venv/bin/python cli.py doctor --strict
```

| Option | Effet | Défaut |
|---|---|---|
| `--json` | Document JSON au lieu du rapport texte | texte |
| `--env-file` | EnvironmentFile à valider | `EnvironmentFile=` lu dans l'unité systemd, sinon `/etc/llm-gateway/env`, sinon `./.env` |
| `--nginx-conf` | Configuration nginx à contrôler | `/etc/nginx/sites-available/llm-gateway` |
| `--systemd-unit` | Unité systemd à contrôler | `/etc/systemd/system/llm-gateway.service` |
| `--verify-hashes` | Calcule le SHA-256 des GGUF déclarés | désactivé |
| `--strict` | Les avertissements deviennent bloquants | désactivé |

`doctor` valide **le fichier d'environnement que systemd donnera au service**, pas
l'environnement du shell appelant : une variable exportée dans votre session ne
peut ni masquer ni compléter le fichier ciblé.

**Sans `sudo`**, les contrôles qui exigent des droits (lecture du fichier de
secrets, de la clé TLS) dégradent en `skip`/`warn` avec la raison, jamais en
faux négatif silencieux.

### Grille des exit codes

| Code | Signification | Conduite à tenir |
|---|---|---|
| `0` | Tous les contrôles passent — aucun échec, aucun avertissement | démarrer / basculer |
| `1` | Au moins un contrôle **critique** en échec | **ne pas** démarrer ni basculer ; corriger d'abord |
| `2` | *Réservé* : erreur d'usage de la CLI (option inconnue) | corriger la ligne de commande |
| `3` | Avertissements seulement, aucun échec bloquant | démarrage possible, dette à traiter |
| `4` | Erreur interne de `doctor` | signaler ; ne pas conclure sur l'état de l'hôte |

`2` n'est pas utilisé pour les avertissements précisément parce que Typer/Click
le renvoie déjà pour une erreur d'usage : un script qui accepterait `2` ne
distinguerait plus « hôte imparfait » de « faute de frappe dans le script ».

Dans un script de déploiement, la lecture correcte est donc :

```bash
set +e
sudo venv/bin/python cli.py doctor --json > /tmp/doctor.json
status=$?
set -e
case "$status" in
    0) echo "Hôte conforme." ;;
    3) echo "Avertissements — voir /tmp/doctor.json." ;;
    *) echo "Diagnostic bloquant (code $status) — arrêt." >&2; exit 1 ;;
esac
```

### Contrôles effectués

Statut par contrôle : `pass`, `warn`, `fail`, `skip`. Seul un `fail` **critique**
bloque (exit 1) ; un `skip` n'est jamais un échec.

Contrôles structurels, partagés avec `GET /ready` (module `readiness.py`) :

| Contrôle | Ce qu'il vérifie | Mode cluster |
|---|---|---|
| `models_config` | `models.yaml` présent et lisible | contrôlé |
| `enabled_models` | au moins un modèle activé | contrôlé |
| `secrets` | secrets non laissés à `CHANGE_ME_*` (avertissement) | contrôlé |
| `llama_server_binary` | binaire présent et exécutable | `skip` (vit sur les nœuds) |
| `model_files` | GGUF et projecteurs présents et lisibles | `skip` (vivent sur les nœuds) |
| `database` | répertoire et fichier SQLite inscriptibles | contrôlé |
| `log_dir` | répertoire de logs inscriptible (avertissement) | contrôlé |
| `vram_budget_fit` | au moins un modèle activé tient dans le budget VRAM | `skip` (budget des nœuds) |
| `cluster_nodes_config` | `nodes.yaml` présent et lisible | contrôlé |
| `cluster_nodes_online` | heartbeat des nœuds | **toujours `skip`** : exige un service vivant |
| `serving_capacity` | capacité de service immédiate | **toujours `skip`** : exige un service vivant |

Contrôles propres à `doctor` :

| Contrôle | Ce qu'il vérifie | Bloquant | Mode cluster |
|---|---|---|---|
| `config_env_file` | permissions du fichier de secrets (attendu 0600/0640), lisibilité par le `User=` de l'unité, chargement de la configuration | oui | contrôlé |
| `models_registry` | `models.yaml` se **parse** réellement : chemins absolus, `.gguf`, allowlist `ALLOWED_MODEL_DIRS`, paramètres `llama.cpp` valides | oui | contrôlé |
| `database_permissions` | base, `-wal`, `-shm` et répertoire non exposés (jugé avec la traversée des parents) | oui si atteignable par tous | contrôlé |
| `disk_space` | espace libre sur les volumes de la base et des logs | oui sous 0,5 Go | contrôlé |
| `llama_server_version` | `llama-server --version` confronté à `LLAMA_SERVER_MIN_BUILD` | oui | `skip` |
| `gpu_inventory` | `nvidia-smi` : modèle, VRAM, driver, compute capability. Distingue « GPU attendu mais absent » (`warn` / `nvidia_smi_unavailable`) de « pas de GPU, assumé » (`skip` / `gpu_absence_declared`, quand `ALLOW_NO_GPU=true` a été posé par `install.sh --allow-no-gpu`) ; une renonciation devenue fausse est signalée en `warn` / `gpu_waiver_stale` | non (avertissement) | `skip` |
| `vram_detected` | budget VRAM net vs VRAM des devices **réellement exposés par `CUDA_VISIBLE_DEVICES`** | oui si le budget net dépasse le matériel ; avertissement si `TOTAL_VRAM_GB` est seulement nominalement supérieur | `skip` |
| `model_artifacts` | taille et plausibilité des GGUF/mmproj ; intégrité SHA-256 **seulement** avec `--verify-hashes` | oui | `skip` |
| `port_pool` | pool `BASE_LLAMA_PORT … +MAX_LOADED_MODELS-1` libre, pas de collision avec `GATEWAY_PORT` | oui pour la collision, avertissement pour un port occupé | `skip` |
| `nginx_timeouts` | `proxy_read_timeout` des blocs proxifiants vs `MODEL_LOAD_TIMEOUT_SECONDS + 10` | non (avertissement) | contrôlé |
| `tls_certificate` | certificat **fourni** : présence, lisibilité, expiration, correspondance au `server_name` ; permissions de la clé | oui | contrôlé |
| `systemd_limits` | politique mémoire déclarée, `TasksMax` dérivé de `MAX_LOADED_MODELS`, working set des modèles `cpu_moe` sous `MemoryHigh`, répertoires de modèles déclarés | oui pour le profil mémoire | contrôlé |
| `cluster_agent_secret` | `AGENT_SECRET` présent et ≥ 32 caractères (sans quoi le service refuse de démarrer) | oui | `skip` en local |
| `cluster_nodes_inventory` | `nodes.yaml` se **parse**, au moins un nœud, `tls_verify` actif | oui | `skip` en local |

Points de conception à connaître :

- **Aucune empreinte SHA-256 n'est calculée par défaut.** Un catalogue de
  production pèse plusieurs centaines de gigaoctets : hacher à chaque diagnostic
  saturerait le stockage. `doctor` se limite à des `stat` ; l'intégrité est
  réservée à `--verify-hashes`.
- **Politique fail-closed de version.** Si `LLAMA_SERVER_MIN_BUILD > 0` mais que
  la version du binaire est illisible, le contrôle **échoue** : on ne peut pas
  prouver que le binaire est patché. Avec `LLAMA_SERVER_MIN_BUILD=0`, `doctor`
  avertit que le garde-fou supply-chain est inerte. Depuis SEC-009, la gateway et
  le node-agent appliquent la **même** politique au démarrage : `doctor` n'est
  plus le seul chemin fail-closed.
- **`nvidia-smi` absent** n'est pas un échec bloquant : en mode cluster c'est
  normal (`skip`), en mode local c'est un avertissement (un hôte de
  développement sans GPU doit rester diagnosticable).
- **VRAM : le critère bloquant est le budget NET**, c'est-à-dire
  `TOTAL_VRAM_GB - VRAM_OVERHEAD_GB - marge`, puisque c'est lui que le contrôle
  d'admission distribue. Un `TOTAL_VRAM_GB` nominal légèrement supérieur à la
  VRAM utilisable (48 « Go » commerciaux contre 46068 MiB exposés sur une L40S)
  n'est qu'un avertissement : la marge absorbe l'écart, mais il la ronge.
- **Un port du pool occupé n'est qu'un avertissement**, car `update.sh` appelle
  `doctor` après bascule, où un port peut être tenu par un modèle légitimement
  chargé.
- **Les incohérences nginx sont signalées, pas corrigées** : `doctor` ne modifie
  aucun fichier.

### Non-divulgation

Aucun secret, token Hugging Face ou valeur sensible n'apparaît dans le rapport,
en sortie humaine comme en JSON :

- les contrôles ne citent que le **nom** d'un secret, jamais sa valeur ;
- tout message passe par une passe de rédaction alimentée par les variables du
  fichier d'environnement dont le nom évoque un secret (`*SECRET*`, `*TOKEN*`,
  `*KEY*`, `*PASSWORD*`) : même une valeur arrivée par un chemin de fichier ou
  par un message d'erreur de validation est remplacée par `***` ;
- aucun secret ne transite par `argv` (donc jamais visible dans `ps`) : les
  seuls sous-processus lancés sont `llama-server --version` et
  `nvidia-smi --query-gpu=…`.

En revanche, les **chemins de fichiers sont conservés** : sans eux un diagnostic
n'est pas actionnable. `doctor` est une commande locale exécutée par un
opérateur qui a déjà accès à ces fichiers — contrairement au corps public de
`GET /ready`, qui n'expose que des codes de contrôle.

### Exemple de sortie humaine

Valeurs et chemins fictifs.

```text
EVARuntime doctor — mode local
  Configuration : /etc/llm-gateway/env
  Généré le     : 2026-07-30T09:15:04+00:00

  [ OK ] config_env_file          Fichier de secrets /etc/llm-gateway/env correctement protégé (mode 0640).
  [ OK ] models_config            Registre des modèles présent et lisible.
  [ OK ] models_registry          Registre chargé et validé : 5 modèle(s) déclaré(s), 3 activé(s).
  [ OK ] enabled_models           3 modèle(s) activé(s) dans le registre.
  [ OK ] secrets                  Aucun secret laissé à sa valeur d'exemple.
  [ OK ] database                 Base de données inscriptible.
  [ OK ] database_permissions     Base et fichiers WAL correctement protégés (2 fichier(s) contrôlé(s)).
  [ OK ] log_dir                  Répertoire de logs inscriptible.
  [ OK ] disk_space               Espace disque suffisant — base (/var/lib/llm-gateway) : 812.4 Go libres.
  [ OK ] llama_server_binary      Binaire llama-server présent et exécutable.
  [WARN] llama_server_version     llama-server build 6120 détecté, mais LLAMA_SERVER_MIN_BUILD=0 : aucun plancher
                                  de version n'est imposé. Fixez LLAMA_SERVER_MIN_BUILD=6120 (ou le premier build
                                  patché connu) pour activer le garde-fou supply-chain.
  [ OK ] gpu_inventory            1 GPU détecté(s) — GPU 0: NVIDIA L40S, 44.4 Go, driver 550.54.15, compute 8.9.
  [ OK ] vram_detected            1/1 GPU exposé(s) par CUDA_VISIBLE_DEVICES=0 → 44.4 Go détectés, TOTAL_VRAM_GB=44.0,
                                  budget net 39.8 Go.
  [ OK ] vram_budget_fit          Au moins un modèle activé tient dans le budget VRAM net (39.8 GB).
  [ OK ] model_files              Artefacts présents et lisibles pour les 3 modèle(s) activé(s).
  [ OK ] model_artifacts          4 artefact(s) mesuré(s) pour 3 modèle(s) activé(s), 77.5 Go au total, tailles plausibles.
  [ OK ] port_pool                Pool de ports 8081–8085 entièrement libre sur 127.0.0.1 (5 modèle(s) simultané(s)).
  [WARN] nginx_timeouts           Timeout nginx trop court pour chargement admin : proxy_read_timeout 30s sur
                                  « /admin/ », alors que la gateway peut légitimement attendre 310s
                                  (MODEL_LOAD_TIMEOUT_SECONDS + 10s, pire modèle activé). Le client recevra 504
                                  alors que le chargement réussit côté serveur. Portez proxy_read_timeout et
                                  proxy_send_timeout au-delà de 310s sur ce bloc (item COR-009/EVA-004).
  [ OK ] tls_certificate          Certificat TLS fourni valide et clé protégée (/etc/ssl/certs/llm-gateway.crt).
  [ OK ] systemd_limits           Limites de llm-gateway.service cohérentes avec la configuration
                                  (MemoryHigh=80% → 102 Go, RAM hôte 128 Go).
  [SKIP] cluster_nodes_config     Mode local : aucun inventaire de nœuds requis.
  [SKIP] cluster_agent_secret     Mode local : aucun secret partagé orchestrateur ↔ nœuds.
  [SKIP] cluster_nodes_inventory  Mode local : aucun inventaire de nœuds requis.
  [SKIP] cluster_nodes_online     Heartbeat des nœuds non évaluable hors process vivant : doctor ne contacte aucun
                                  node-agent. Utilisez GET /ready ou GET /admin/status.
  [SKIP] serving_capacity         Capacité de service non évaluable hors process vivant : doctor n'interroge aucun
                                  service. Utilisez GET /ready (COR-005) ou le smoke test (COR-006).

  Résumé  : 17 conforme(s), 2 avertissement(s), 0 échec(s) dont 0 bloquant(s), 5 ignoré(s)
  Verdict : AVERTISSEMENTS SEULEMENT
  Exit code : 3
```

Les messages longs sont sur une seule ligne dans la sortie réelle (repliés
ci-dessus pour la lisibilité du document).

### Exemple de sortie JSON

Extrait — valeurs fictives. Le document complet contient une entrée par
contrôle, dans le même ordre que la sortie humaine.

```json
{
  "tool": "evaruntime-doctor",
  "schema_version": 1,
  "generated_at": "2026-07-30T09:15:04+00:00",
  "mode": "local",
  "config_source": "/etc/llm-gateway/env",
  "strict": false,
  "status": "fail",
  "exit_code": 1,
  "summary": {
    "pass": 15,
    "warn": 2,
    "fail": 1,
    "skip": 6,
    "blocking": 1
  },
  "checks": [
    {
      "name": "config_env_file",
      "status": "pass",
      "code": "ok",
      "message": "Fichier de secrets /etc/llm-gateway/env correctement protégé (mode 0640).",
      "critical": true
    },
    {
      "name": "vram_detected",
      "status": "fail",
      "code": "vram_budget_exceeds_hardware",
      "message": "1/2 GPU exposé(s) par CUDA_VISIBLE_DEVICES=0 → 44.4 Go détectés, TOTAL_VRAM_GB=88.0, budget net 81.6 Go. Le contrôle d'admission peut distribuer plus de VRAM qu'il n'en existe : les chargements échoueront en cours de route, sans qu'aucune éviction n'y remédie. Abaissez TOTAL_VRAM_GB à 44.4 au plus (ou relevez VRAM_OVERHEAD_GB / VRAM_SAFETY_MARGIN).",
      "critical": true
    },
    {
      "name": "serving_capacity",
      "status": "skip",
      "code": "service_not_running",
      "message": "Capacité de service non évaluable hors process vivant : doctor n'interroge aucun service. Utilisez GET /ready (COR-005) ou le smoke test (COR-006).",
      "critical": false
    }
  ],
  "reason": "total_vram_gb_overcommitted"
}
```

Champs stables, garantis par les tests (`gateway/tests/test_doctor.py`) :
`tool`, `schema_version`, `generated_at`, `mode`, `config_source`, `strict`,
`status` (`ok` / `warn` / `fail`), `exit_code`, `summary`
(`pass`/`warn`/`fail`/`skip`/`blocking`), `checks[]`
(`name`/`status`/`code`/`message`/`critical`) et `reason` (présent uniquement
s'il existe un contrôle bloquant, contenant le `code` du premier d'entre eux
dans l'ordre du rapport). Les `code` sont des identifiants machine stables :
c'est sur eux qu'un script doit s'appuyer, jamais sur le texte du `message`.

Toute évolution non rétro-compatible de ce document incrémente
`schema_version`.

---

## 9. Planificateur d'amorçage — `bootstrap-plan`

`bootstrap-plan` calcule ce qu'il faudrait installer sur un hôte pour atteindre
le premier token — et **n'applique rien**. Aucun téléchargement, aucune
compilation, aucune écriture de registre, aucun `systemctl`. Le seul
sous-processus que la chaîne puisse lancer est `llama-server --version`, et
seulement si `--llama-bin` est fourni. La commande ne demande donc **aucun
privilège**.

À situer parmi les autres outils hors-service :

| Outil | Question à laquelle il répond | Hôte déjà installé |
|---|---|---|
| `bootstrap-plan` | Que faudrait-il installer ici, et pourquoi ? | **non** |
| `doctor` | L'hôte et la configuration sont-ils corrects ? | oui |
| `smoke_test.sh` | La chaîne publique sert-elle réellement un token ? | oui, et démarré |

L'installation elle-même reste le domaine de `install.sh` : la séparation entre
les trois étapes — plan, installation du socle et application relue — sont
détaillées dans l'[architecture](architecture.md#trois-étapes-et-pourquoi-elles-sont-séparées).

### Usage

```bash
# Depuis le répertoire de la gateway, avec le venv du service
cd /opt/llm-gateway

# Plan complet, sortie humaine — aucun privilège requis
./venv/bin/python cli.py bootstrap-plan

# Plan JSON (schéma versionné) pour un script de déploiement
./venv/bin/python cli.py bootstrap-plan --json

# Cas nominal : runtime épinglé, plancher de sécurité, volume des modèles explicite
./venv/bin/python cli.py bootstrap-plan \
    --pin-version b6210 \
    --pin-commit <sha_git_du_tag_b6210> \
    --min-build 6120 \
    --models-dir /models

# Évaluer un llama-server déjà en place (`--version` sera exécuté sur ce binaire)
./venv/bin/python cli.py bootstrap-plan \
    --llama-bin /opt/llama.cpp/current/llama-server

# Restreindre le plan à une entrée du catalogue
./venv/bin/python cli.py bootstrap-plan --model qwen2.5-0.5b-instruct-q4_k_m

# Traiter les avertissements comme bloquants (recette de mise en production)
./venv/bin/python cli.py bootstrap-plan --strict
```

| Option | Effet | Défaut |
|---|---|---|
| `--json` | Document JSON (schéma versionné) au lieu du rapport texte | texte |
| `--mode` | Topologie visée : `local` ou `cluster` | `local` |
| `--catalog` | Catalogue de modèles approuvés à lire | `gateway/bootstrap/catalog.yaml` |
| `--hardware-profile` | Profil matériel **déclaré** (JSON) au lieu de sonder l'hôte | sonde |
| `--models-dir` | Volume où atterriraient les GGUF | `/models` |
| `--model` | Restreindre à ces identifiants de catalogue (option répétable) | toutes les entrées |
| `--max-models` | Nombre maximal de modèles retenus | `1` |
| `--llama-bin` | Binaire `llama-server` déjà en place, à évaluer | aucun |
| `--pin-version` | Version llama.cpp épinglée, au format « bNNNNN » | aucune |
| `--pin-commit` | Commit git correspondant à `--pin-version` | aucun |
| `--min-build` | Premier build patché connu — plancher de sécurité | `0` |
| `--runtime-variants` | Matrice d'artefacts `llama-server` épinglée par l'opérateur (YAML) — **remplace** la matrice livrée | matrice livrée |
| `--allow-container` | Accepter une image conteneur épinglée par digest (étape 2 de §6) | refusé |
| `--allow-local-build` / `--no-local-build` | Accepter le build local reproductible (étape 4 de §6) | accepté |
| `--allow-cpu-fallback` | **Assumer** la dégradation GPU → CPU si aucune variante GPU sûre n'existe | refusé |
| `--llmfit-bin` | Binaire LLMfit à consulter | recherché dans le `PATH` |
| `--llmfit-version` | Version LLMfit attendue — va de pair avec `--llmfit-sha256` | aucune |
| `--llmfit-sha256` | Empreinte attendue du binaire LLMfit (64 hex minuscules) | aucune |
| `--llmfit-timeout` | Délai maximal accordé à LLMfit, en secondes | `20` |
| `--llmfit-profile` | Recommandation écrite à la main, à la place de LLMfit | aucune |
| `--no-llmfit` | Ne pas consulter LLMfit du tout | désactivé |
| `--strict` | Les avertissements deviennent bloquants | désactivé |

`--llmfit-version` et `--llmfit-sha256` vont ensemble, pour la même raison que
`--pin-version` et `--pin-commit` : une version seule se déclare — c'est une
chaîne que le binaire choisit —, une empreinte seule ne dit pas ce qu'on croyait
installer. **Sans les deux, le binaire LLMfit n'est pas exécuté** et la section
sort en `skip` : un conseiller non épinglé n'est pas un conseiller de confiance.
`--llmfit-profile` remplace intégralement LLMfit par un profil écrit à la main,
qui passe par **la même validation** — une entrée d'opérateur n'est pas plus
fiable qu'une sortie d'outil, elle est seulement plus facile à corriger.

#### Le mode `cluster` est refusé au jalon M1

`--mode cluster` sort en code `2` avec un message explicite. Ce n'est pas un
oubli : en cluster, le binaire `llama-server` et les GGUF vivent sur les nœuds,
alors que le planificateur inventorie l'hôte sur lequel il tourne. Accepter
l'option produirait un plan **cohérent et entièrement faux** — il proposerait un
runtime local et des modèles sous le volume de la gateway. En attendant que la
planification des nœuds existe, planifiez chaque nœud séparément avec
`--hardware-profile` et `--models-dir`, ou restez en `--mode local`.

`--pin-version` et `--pin-commit` vont ensemble : l'un sans l'autre n'épingle
rien et la commande refuse (code `2`). `--min-build` est la valeur d'où
`LLAMA_SERVER_MIN_BUILD` est dérivé — c'est le **plancher de sécurité**, pas le
build épinglé, pour que la gateway accepte tout binaire au moins aussi patché
que le premier build corrigé connu (cf.
[section 11 du guide de déploiement](deployment.md#11-mise-à-jour)). Une politique
qui épinglerait un build inférieur à son propre plancher est refusée à la
construction.

### Grille des exit codes

Mêmes conventions que `doctor`, volontairement.

| Code | Signification | Conduite à tenir |
|---|---|---|
| `0` | Plan complet et applicable | appliquer |
| `1` | Au moins un bloqueur | **ne rien appliquer**, même partiellement ; corriger d'abord |
| `2` | Erreur d'usage : la commande désigne quelque chose qui n'existe pas | corriger la ligne de commande |
| `3` | Avertissements seulement — applicable | applicable, dette à traiter |
| `4` | Erreur d'exécution du planificateur | signaler ; ne pas conclure sur l'état de l'hôte |

Les trois codes d'échec disent trois choses différentes, et un script doit
pouvoir les distinguer : `1` = **cet hôte** est bloqué, `2` = **votre commande**
est mal formée, `4` = **l'outil** a cassé. Relèvent du code `2` : une option
inconnue, `--mode cluster` (non planifiable au jalon M1) ou un `--mode` hors
`local`/`cluster`, un `--pin-version` sans son commit, un `--llmfit-version` sans
son empreinte, un `--model` absent du catalogue, un `--hardware-profile`
illisible ou invalide.

**Tout plan qui sort en code `1` ne décrit aucune étape**, quelle que soit la
cause — runtime non résolu, catalogue illisible, aucun modèle ne tenant sur
l'hôte, **ou `--strict` sur un plan qui ne portait que des avertissements**. Le
critère est le statut, pas la seule présence de bloqueurs : `--strict` promeut
les avertissements en blocage, et cette promotion vaut pour `applicable` et pour
les étapes exactement comme elle vaut pour le code de sortie.

Les étapes ne disparaissent pas en silence : le rendu humain indique combien
avaient été calculées et invite à lever les bloqueurs. La règle est portée par le
contrat lui-même — un document qui porterait à la fois un statut non applicable
et des étapes est rejeté par la validation. Sans quoi un applicateur pourrait
n'exécuter que la moitié du plan, et c'est justement la moitié qui consomme du
disque et du réseau.

`--strict` change donc le **document**, pas seulement son affichage : le JSON
produit avec `--strict` porte `strict: true`, `applicable: false` et
`steps: []`. Un plan enregistré reste ainsi cohérent avec le code de sortie qui
l'accompagnait.

### Sans épinglage, le plan sort bloqué — et c'est voulu

Lancée sans `--pin-version`/`--pin-commit`, la commande ne propose **aucune
étape** et sort en code `1`. Ce n'est pas un défaut d'ergonomie :

- le planificateur refuse d'inventer un numéro de build. Un numéro inventé se
  propagerait dans tous les manifestes de provenance produits, où il aurait
  l'apparence d'un fait vérifié ;
- une séquence de téléchargements sans binaire capable de servir les modèles
  inviterait à n'exécuter que la moitié du plan — et cette moitié-là est
  justement celle qui consomme du disque et du réseau.

Ce qui *serait* retenu reste visible dans la section « Modèles retenus » : rien
n'est caché, seule la séquence est retenue. Pour débloquer, fournir la version
épinglée, le commit correspondant et le plancher de sécurité :

```bash
./venv/bin/python cli.py bootstrap-plan \
    --pin-version b6210 --pin-commit <sha_git> --min-build 6120
```

### Épingler son runtime — `--runtime-variants`

`--pin-version`/`--pin-commit` disent **quelle version** installer. Ils ne disent
pas **quel artefact** : c'est le rôle de la matrice d'artefacts, qui associe un
couple (backend, plateforme) à une archive, une image ou un build local.

La matrice **livrée** avec EVARuntime ne porte **aucune empreinte**. C'est
délibéré : aucune n'a été vérifiée dans ce dépôt, et en inventer une
transformerait le contrôle d'intégrité en théâtre. Deux conséquences à connaître :

- seules ses variantes `local-build` sont éligibles — les autres apparaissent
  dans les motifs de rejet du plan avec la mention « non épinglé » ;
- aucune de ses variantes ne porte d'URL d'archive exploitable. Un plan produit
  sans `--runtime-variants` est lisible et honnête, mais `bootstrap-apply` ne
  peut pas en installer de runtime.

Fournir sa propre matrice se fait avec un fichier YAML :

```bash
./venv/bin/python cli.py bootstrap-plan --json \
    --pin-version b6210 --pin-commit <sha_git> --min-build 6120 \
    --runtime-variants /etc/evaruntime/runtime-variants.yaml
```

Le modèle commenté est livré dans **`gateway/deploy/runtime-variants.yaml.example`**.
Il ne se charge pas tel quel, et c'est voulu : les valeurs que le dépôt ne peut
pas connaître y portent le marqueur `REMPLACER`, que le chargeur reconnaît et
nomme dans son refus. Copiez-le, relevez les vraies valeurs, chargez-le.

**Le fichier remplace la matrice livrée, il ne s'y ajoute pas.** Ce que vous y
écrivez est l'intégralité de ce que le résolveur examinera. Si vous voulez
conserver la voie du build local, redéclarez-la explicitement. Le remplacement
est délibéré : en union, une faute de frappe dans `platform` rendrait votre
entrée épinglée invisible et une variante `local-build` livrée l'emporterait en
silence — vous liriez un plan réussi qui ignore intégralement votre épinglage.

Champs, par source :

| Source | `reference` | `artifact_sha256` | `container_digest` | `approx_bytes` |
|---|---|---|---|---|
| `official-release`, `evaruntime-build` | URL HTTPS d'**archive** | obligatoire | interdit | obligatoire |
| `official-container` | `hôte/chemin:tag` | interdit | obligatoire | facultatif |
| `local-build` | interdit | interdit | interdit | interdit |

`evidence` et `recorded_on` sont obligatoires dans **tous** les cas. Ils ne sont
pas décoratifs : une variante fournie ici est un **constat opérateur**, et le
plan la présentera comme tel — jamais comme une affirmation de la spécification.
Écrivez comment vous avez relevé l'empreinte, pas ce que vous supposez, et n'y
mettez aucun nom de personne : le champ est recopié dans le plan et les journaux.

Relever les valeurs :

```bash
curl -fL -o llama.zip \
  https://github.com/ggml-org/llama.cpp/releases/download/<version>/<archive>
sha256sum llama.zip   # -> artifact_sha256
stat -c%s llama.zip   # -> approx_bytes

docker buildx imagetools inspect ghcr.io/ggml-org/llama.cpp:server-cuda
                      # -> container_digest, sous la forme sha256:<64 hex>
```

Recoupez la somme avec celle publiée par la release amont. Une somme calculée sur
un fichier que vous venez de télécharger ne prouve que la cohérence du transfert ;
c'est le recoupement avec la source qui prouve la provenance.

La validation est **fail-closed et sans repli** : un fichier malformé fait sortir
la commande en code `2` (erreur d'usage) sans produire de plan. Il ne retombe
**jamais** sur la matrice livrée — un opérateur qui fournit un fichier attend que
ce fichier soit employé, pas qu'il soit ignoré. Sont refusés, entre autres : une
URL non HTTPS, une URL portant des identifiants ou une chaîne de requête (elle
transporterait un secret jusque dans le plan et les journaux), une URL qui ne
désigne pas une archive — une page de releases en est une —, une empreinte qui
n'est pas 64 hexadécimaux minuscules, un champ hors schéma, deux entrées pour le
même couple (source, backend, plateforme).

`--runtime-variants` exige `--pin-version` et `--pin-commit` : la matrice dit
quel artefact installer, l'épinglage dit quelle version il porte, et le manifeste
de provenance a besoin des deux.

L'option n'existe que sur `bootstrap-plan`. `bootstrap-apply` reconstruit la
décision depuis le plan relu : c'est le plan qui porte la variante retenue, son
empreinte et son URL, et rien ne se redécide au moment d'appliquer.

Enfin, ce que fournir une matrice **ne fait pas** : cela n'autorise aucun repli
CPU tacite (§6 reste appliqué à l'identique), et cela ne prouve pas que l'archive
désignée contient un `llama-server` compilé pour le backend annoncé —
`--version` ne le dit pas. Le plan porte ce constat.

### Ouvrir une branche de §6 — les trois drapeaux de politique

L'ordre de résolution de §6 comporte des branches que l'opérateur doit accepter
explicitement. Elles étaient jusqu'ici inatteignables autrement que depuis du code
Python : on pouvait épingler une image conteneur par digest et la voir
**systématiquement** écartée — « mode conteneur non accepté par la politique ».

| Option | Ce qu'elle ouvre ou ferme | Défaut |
|---|---|---|
| `--allow-container` | L'étape 2 de §6 : les images officielles épinglées **par digest** entrent dans l'ordre de résolution | refusé |
| `--no-local-build` | Ferme l'étape 4. Sur la matrice livrée, qui ne porte aucune empreinte, il ne reste alors **aucune** variante éligible et la résolution échoue | l'étape est ouverte |
| `--allow-cpu-fallback` | Assume la dégradation GPU → CPU quand aucune variante GPU sûre n'existe | refusé |

Les trois exigent `--pin-version` et `--pin-commit`, pour la même raison que
`--runtime-variants` : autoriser une branche sans épingler de version ne résout
rien, et laisser passer l'option ferait croire qu'elle a été prise en compte.

`--allow-container` a une limite qui n'est pas dans la politique mais dans le
code : `server_manager` ne sait lancer que des sous-processus natifs. Le plan est
descriptible, son application ne l'est pas encore — le plan porte ce constat.

#### `--allow-cpu-fallback` n'est pas un détail d'invocation

Le défaut qui compte est que ce drapeau soit **faux**. Une installation CPU
démarre, répond et passe le smoke test : elle a toutes les apparences d'une
installation réussie, et son TTFT ne se verra qu'en production, des semaines plus
tard. §6 exige donc que cette dégradation soit demandée, jamais subie.

La politique retenue est inscrite dans le plan, sous `sections.runtime.data.policy`,
et pas seulement dans la ligne de commande : un plan relu six mois après doit dire
sous quelles règles il a été calculé, sans quoi on le compare à un autre sans
savoir que les règles différaient. Deux constats accompagnent le drapeau :

- `cpu_fallback_authorized` (`info`) dès que l'autorisation est debout, **même si
  le repli n'est pas emprunté** ;
- `cpu_fallback_degraded` (`warn`) quand il l'est — avec ce que l'opérateur perd :
  GPU inutilisé, TTFT et débit d'un ordre de grandeur inférieurs, `vram_gb` de
  `models.yaml` privé de sens.

Le rendu humain, lui, porte l'annonce en toutes lettres dans les notes de la
section : `Repli CPU EMPRUNTÉ` et `CE PLAN EST DÉGRADÉ`. `data` n'est pas imprimé,
`notes` l'est — et un repli CPU qui ne se lirait que dans le JSON serait un repli
silencieux. Avec `--strict`, l'avertissement devient bloquant.

### Exemple de sortie humaine

Exécution **sans aucune option**, sur un poste de développement macOS sans GPU
et sans `/models` — ce n'est donc pas le cas nominal, mais c'est exactement la
sortie qu'un opérateur obtient quand rien n'est encore en place, et elle montre
les quatre familles de constats. Sortie tronquée après les sections :

```text
PLAN DE BOOTSTRAP EVARUNTIME
  Généré le    : 2026-07-31T09:11:48+00:00
  Mode         : local
  Schéma       : v1
  Verdict      : BLOQUÉ

Ce document décrit ce qui SERAIT fait. Rien n'a été installé, téléchargé
ni modifié pour le produire.

SECTIONS
  [XX] Inventaire matériel — macos 25.5.0 arm64 (mesuré) — 24.0 Go de RAM, 0.0 Go libres sur /models, aucun GPU ; backends : metal, cpu
       · warn [cpu_model_unknown] Modèle de CPU non lisible (`/proc/cpuinfo` absent). Le profil reste exploitable, mais aucune variante CPU optimisée ne pourra être justifiée.
       · warn [cpu_flags_unavailable] Jeux d'instructions du CPU non détectables : impossible de confirmer AVX2/AVX-512. La liste vide signifie « non mesuré », pas « absent » — ne l'interprétez pas comme un CPU sans SIMD.
       · warn [ram_available_unknown] RAM disponible non mesurable ; seule la RAM totale sera utilisée pour dimensionner, ce qui surestime la marge réelle.
       · fail [disk_unreadable] Espace libre illisible sur « /models » (FileNotFoundError sur /models). Aucun téléchargement ne peut être planifié sans connaître la place disponible : créez le répertoire ou pointez --models-dir sur le bon volume.
       · warn [gpu_probe_unavailable] Sonde GPU impossible : nvidia-smi introuvable dans le PATH. L'hôte est traité comme dépourvu de GPU NVIDIA — légitime sur un poste de développement ou un orchestrateur de cluster, anormal sur un nœud d'inférence, où seul un backend CPU pourra être proposé.
  [XX] Runtime llama-server — aucune politique de release fournie
       · fail [politique_de_release_absente] Aucune politique de release n'a été fournie : le planificateur ne peut pas décider quel llama-server installer, et il refuse d'en inventer une. Fournissez une version épinglée (« bNNNNN »), le commit correspondant et le premier build patché connu (plancher de sécurité, cf. GHSA-8947-pfff-2f3c) — c'est de là que LLAMA_SERVER_MIN_BUILD est généré (§6).
       Un numéro de build inventé se propagerait dans tous les manifestes de
       provenance produits, où il aurait l'apparence d'un fait vérifié.
       Tant que le runtime n'est pas résolu, AUCUNE étape n'est proposée : ce qui
       serait retenu reste visible dans la section « Modèles retenus », mais une
       séquence de téléchargements sans binaire capable de servir les modèles
       inviterait à n'exécuter que la moitié du plan.
  [--] Recommandation — conseil consultatif — LLMfit absent, aucune recommandation
       LLMfit est un conseiller, pas une autorité. Ses estimations ignorent :
         · tous les paramètres EVARuntime
         · le coût exact de ctx_size × parallel
         · les caches K/V sélectionnés
         · le comportement exact de cpu_moe
         · l'empreinte des projecteurs multimodaux
         · la fragmentation VRAM
         · les autres modèles chargés simultanément
         · les contraintes systemd de l'hôte

       Règle d'activation : recommandation LLMfit + modèle approuvé par le catalogue EVARuntime + estimation conservatrice + chargement réel de calibration = modèle activable.
  [ok] Catalogue approuvé — 2 modèle(s) approuvé(s), épinglé(s) et sous licence identifiée (apache-2.0).
  [XX] Modèles retenus — aucun modèle retenu
       · fail [aucun_modele_retenu] Aucun modèle du catalogue ne peut être retenu sur cet hôte : qwen2.5-0.5b-instruct-q4_k_m → disque insuffisant sur le volume des modèles : 0.0 Go libres pour 0.6 Go requis (marge ×1.25); smollm2-360m-instruct-q8_0 → disque insuffisant sur le volume des modèles : 0.0 Go libres pour 0.4 Go requis (marge ×1.25). Sans modèle, le parcours jusqu'au premier token n'a pas d'objet.
       Les valeurs de ressources sont des ESTIMATIONS conservatrices, jamais des
       mesures. L'étape `calibrate_model` du plan est ce qui les remplace par des
       pics observés ; tant qu'elle n'a pas eu lieu, l'entrée de registre reste
       désactivée (AUT-007).

DÉCISIONS
  · runtime llama-server → aucun
      parce que aucune politique de release n'a été fournie au planificateur
  · modèle par défaut → aucun
      parce que aucune entrée approuvée du catalogue ne tient dans les ressources de cet hôte
      écarté : qwen2.5-0.5b-instruct-q4_k_m (disque insuffisant sur le volume des modèles : 0.0 Go libres pour 0.6 Go requis (marge ×1.25)), smollm2-360m-instruct-q8_0 (disque insuffisant sur le volume des modèles : 0.0 Go libres pour 0.4 Go requis (marge ×1.25))

ÉTAPES QUE L'APPLICATION EXÉCUTERAIT
  (aucune)
```

Le rapport se poursuit par les blocs `BLOQUEURS`, `AVERTISSEMENTS` et la ligne
`Sortie : 1`, tronqués ici.

Quatre marques de statut se lisent en tête de section : `[ok]` complète, `[!!]`
dégradée mais utilisable, `[XX]` inexploitable — bloquante — et `[--]` non
applicable ici, qui n'est **jamais** un échec (LLMfit absent est un `[--]`).

### Séquence d'étapes proposée

Extrait d'une exécution où une politique de release est fournie et où le volume
des modèles est lisible. Le plan de cette exécution portait par ailleurs d'autres
constats ; seule la séquence est reproduite ici, et le chemin du volume de test a
été remplacé par `/models` :

```text
ÉTAPES QUE L'APPLICATION EXÉCUTERAIT
   1. [accept_license] qwen2.5-0.5b-instruct-q4_k_m — apache-2.0
      Acceptation explicite par l'opérateur avant tout téléchargement. Modèle de base : apache-2.0 ; fine-tune : apache-2.0 ; redistribution du GGUF autorisée.
   2. [download_model] Qwen/Qwen2.5-0.5B-Instruct-GGUF@9217f5db79a29953eb74d5343926648285ec7e67  (root, 468.6 Mio)
      Télécharger l'ensemble indivisible (qwen2.5-0.5b-instruct-q4_k_m.gguf) vers /models, à révision figée, par fichier temporaire puis renommage atomique.
   3. [verify_artifact] qwen2.5-0.5b-instruct-q4_k_m
      Vérifier le SHA-256 de chaque fichier de l'ensemble contre le catalogue, avant toute mise en service. Un écart annule l'installation du modèle.
   4. [write_registry] models.yaml → qwen2.5-0.5b-instruct-q4_k_m  (root)
      Écrire l'entrée de registre avec `enabled: false` et les paramètres du catalogue. Elle reste désactivée tant que la calibration et la recette n'ont pas réussi (AUT-007).
   5. [calibrate_model] qwen2.5-0.5b-instruct-q4_k_m
      Chargement réel de calibration : relever les pics RAM/VRAM, la durée de chargement et le TTFT, puis PROPOSER un `vram_gb` — sans l'appliquer silencieusement (§9).
   6. [enable_model] qwen2.5-0.5b-instruct-q4_k_m  (root)
      Publier `enabled: true` uniquement dans le registre vivant, avec la capacité issue de la calibration, à titre PROVISOIRE. Le fichier reste `enabled: false` jusqu'au succès de la recette.
   7. [smoke_test] qwen2.5-0.5b-instruct-q4_k_m
      Recette du premier token pour ce modèle à travers nginx → gateway → llama-server. Succès : activation confirmée ; échec : retour immédiat à `enabled: false`.
   8. [warmup_model] qwen2.5-0.5b-instruct-q4_k_m
      Préchauffer le modèle pour que le premier utilisateur ne paie pas le chargement après un déploiement réussi (AUT-010).
```

Chaque étape annonce si elle exige `root` et si elle est réversible : ce sont les
deux questions que se pose l'opérateur avant de valider, et les cacher
reviendrait à lui demander une signature à l'aveugle. L'ordre n'est pas
négociable — le
[détail des trois inversions interdites](architecture.md#ordre-des-étapes) est
dans le document d'architecture.

> **Aucune de ces étapes n'est exécutée par `bootstrap-plan`.** Le document
> décrit ce qui *serait* fait ; `bootstrap-apply` l'exécute seulement après
> relecture et avec `--apply`. `install.sh` reste chargé de poser le socle
> système (utilisateur, venv, systemd, nginx et secrets).

### Artefacts déjà présents sur l'hôte

Sur une réinstallation, une bascule de branche ou un hôte de test, les GGUF du
catalogue sont souvent **déjà là**. Le plan le constate et adapte sa séquence —
il ne propose jamais de retélécharger ce qu'il sait déjà en place et vérifié.

Trois situations, trois conduites :

| État constaté au chemin cible | Ce que le plan propose |
|---|---|
| Ensemble complet, tailles conformes, manifeste de provenance cohérent | L'étape `download_model` **disparaît** ; son volume est décompté du total annoncé ; la `verify_artifact` reste seule et porte le motif |
| Un fichier présent dont la **taille diffère** de celle épinglée | **Bloqueur** `artefact_local_divergent` : le plan sort en code 1, sans aucune étape. Le fichier n'est ni réutilisé ni écrasé — déplacez-le ou supprimez-le vous-même, puis régénérez le plan |
| Tout autre cas — ensemble incomplet, taille non épinglée, manifeste absent ou périmé | Le téléchargement reste proposé **en entier**. L'ensemble est indivisible (§8) : une moitié présente n'est jamais créditée |

La séquence devient alors, pour ce modèle :

```text
   2. [verify_artifact] qwen2.5-0.5b-instruct-q4_k_m
      Aucun téléchargement proposé : les 1 fichier(s) de l'ensemble sont déjà au chemin cible, à la taille épinglée, et le manifeste de provenance atteste qu'ils ont été confrontés octet à octet aux empreintes du catalogue — aucun octet à retélécharger. Cette étape reste la seule preuve d'intégrité : elle relit les octets et confronte le SHA-256 de chaque fichier de l'ensemble au catalogue, avant toute mise en service. Un écart annule l'installation du modèle.
```

> **`bootstrap-plan` ne hache aucun octet.** Relire un GGUF de 40 Gio à chaque
> planification transformerait une commande rapide et rejouable — appelée aussi
> par `doctor` — en opération de plusieurs minutes. La conformité est donc
> **attestée**, pas recalculée : le plan s'appuie sur le manifeste de provenance
> écrit par un téléchargement vérifié antérieur, et c'est l'étape
> `verify_artifact`, à l'application, qui relit réellement les octets et
> confronte les empreintes. La **divergence**, elle, se prouve pour rien : une
> taille différente interdit mathématiquement au SHA-256 de correspondre, et le
> plan la traite lui-même.
>
> Conséquence à connaître : si les fichiers sont bien là mais qu'aucun manifeste
> ne les atteste — GGUF recopiés à la main, par exemple —, le plan continue de
> proposer le téléchargement. Ce n'est pas un gaspillage : le téléchargeur
> revérifie et ne transfère rien s'il n'y a rien à transférer, puis écrit le
> manifeste manquant.

Le détail complet est lisible dans la sortie JSON, sous
`sections[].data.retained[].local_artifact` : fichiers attendus, présents,
manquants, divergents, chemin de l'attestation et motif en clair.

### Sortie JSON

```bash
./venv/bin/python cli.py bootstrap-plan --json > /tmp/plan.json
```

```json
{
  "tool": "eva-bootstrap-plan",
  "schema_version": 1,
  "generated_at": "2026-07-31T09:14:38+00:00",
  "mode": "local",
  "strict": false,
  "status": "blocked",
  "applicable": false,
  "exit_code": 1,
  "counts": {
    "ok": 1,
    "warn": 0,
    "fail": 3,
    "skip": 1,
    "steps": 0,
    "decisions": 2
  },
  "estimated_download_bytes": 0,
  "sections": [ ... ],
  "steps": [ ... ],
  "decisions": [ ... ],
  "blockers": [ ... ],
  "warnings": [ ... ]
}
```

Les `code` des constats (`disk_unreadable`, `politique_de_release_absente`,
`catalogue_entree_non_epinglee`…) sont des identifiants machine stables : c'est
sur eux qu'un script doit s'appuyer, jamais sur le texte du `message`. Toute
évolution non rétro-compatible du document incrémente `schema_version` ; un plan
dont le `schema_version` dépasse celui de l'outil est signalé comme tel par le
validateur du schéma, plutôt que lu de travers.

Les six valeurs de `counts` sont des entiers positifs ou nuls. Les booléens et
les flottants sont refusés, même lorsqu'ils sont numériquement égaux à l'entier
attendu (`true == 1`, par exemple), ainsi que toute clé absente ou inconnue.

Le plan est destiné à être collé dans un ticket : le rendu — JSON **comme**
texte — refuse de publier un document contenant une valeur ressemblant à un
secret, et la commande échoue alors en code `4` plutôt que d'émettre le
document. Détail des deux filets :
[non-divulgation](architecture.md#non-divulgation).

### Profil matériel déclaré (`--hardware-profile`)

Sur une VM, en passthrough, ou sur un hôte où les outils constructeur échouent,
sonder rend une réponse *fausse* plutôt qu'aucune réponse. `--hardware-profile`
remplace alors intégralement la sonde par un document JSON décrivant l'hôte :

```json
{
  "os": "linux",
  "os_version": "22.04",
  "arch": "x86_64",
  "cpu_model": "Intel(R) Xeon(R) Gold 6338",
  "cpu_flags": ["avx2", "avx512f"],
  "ram_total_bytes": 137438953472,
  "ram_available_bytes": 128849018880,
  "disk_available_bytes": 2199023255552,
  "disk_path": "/models",
  "gpus": [
    {
      "index": 0,
      "uuid": "GPU-00000000-0000-0000-0000-000000000000",
      "vendor": "nvidia",
      "model": "NVIDIA L40S",
      "vram_total_bytes": 48305504256,
      "driver_version": "535.183.01",
      "compute_capability": "8.9"
    }
  ],
  "backend_candidates": ["cuda12", "vulkan", "cpu"]
}
```

Le document est traité comme une **entrée non fiable**, au même titre qu'un corps
de requête. Sont refusés, avec le chemin du champ fautif : un champ obligatoire
absent ou vide, une taille qui n'est pas un entier d'octets, `ram_total_bytes`
à zéro, une `ram_available_bytes` supérieure au total, un `uuid` de GPU en double
(qui doublerait le budget VRAM), une `vram_total_bytes` nulle ou négative, un
backend inconnu, un backend GPU annoncé alors que `gpus` est vide, et toute
valeur ressemblant à un secret. `CUDA_VISIBLE_DEVICES` s'applique au profil
déclaré comme à un profil sondé.

Un profil déclaré porte **toujours** un avertissement
`hardware_profile_declared` : les capacités n'ont été confrontées à aucune sonde,
et un chiffre trop généreux ne se verra qu'au chargement du premier modèle. Le
silence serait ici le vrai défaut — un plan bâti sur des chiffres affirmés par un
humain n'a pas la même valeur de preuve qu'un plan bâti sur une mesure.

### Lecture depuis un script

```bash
set +e
./venv/bin/python cli.py bootstrap-plan --json > /tmp/plan.json
status=$?
set -e
case "$status" in
    0) echo "Plan applicable." ;;
    3) echo "Avertissements — voir /tmp/plan.json." ;;
    *) echo "Plan bloqué ou en erreur (code $status) — arrêt." >&2; exit 1 ;;
esac
```

Un plan bloqué ne doit être appliqué par personne, **jamais partiellement** : le
champ `applicable` du document le dit aussi explicitement que l'exit code.

---

## 10. Applicateur d'amorçage — `bootstrap-apply`

`bootstrap-plan` décrit ce qu'il faudrait installer. `bootstrap-apply` **exécute
ce plan** : installation du runtime, téléchargement des modèles, écriture du
registre, calibration, activation, pré-chauffage, recette du premier token, puis
rapport d'installation.

> **État au 2026-08-03 — lisez ceci avant d'essayer.** Les neuf actions ont un
> câblage de production depuis la CLI : décision runtime reconstruite depuis le
> plan relu, téléchargement, acceptation explicite de licence, sondes réelles
> RAM/VRAM et `llama-server` isolé sur loopback, client HTTP asynchrone pour la
> recette et le pré-chauffage. COR-022 est fermé par DEC-010 : chaque modèle suit
> `calibrate → enable` provisoire `→ smoke_test → warmup`, avec retour immédiat à
> l'état désactivé si la recette ou sa preuve échoue. La fenêtre provisoire est
> uniquement en mémoire : `models.yaml` ne passe à `enabled: true` qu'après le
> premier token prouvé.
>
> Cette livraison reste une **capacité codée et testée contre des doubles**. Le
> parcours `bootstrap-apply --apply` n'a encore été exécuté ni sur un GPU réel,
> ni à travers un nginx réel ; le jalon M2 n'est donc pas prononcé. En outre, les
> variantes runtime par défaut sans SHA-256 restent volontairement
> ininstallables : fournissez une décision épinglée dans le plan.

Les deux téléchargements sortants — archive du runtime et fichiers GGUF/mmproj —
emploient la même politique HTTPS publique. L'endpoint initial et chaque
redirection sont validés avant émission ; identifiants dans l'URL, loopback,
link-local, réseaux privés, adresses non globales et réponses DNS mixtes sont
refusés. La connexion TCP est épinglée sur la résolution contrôlée tout en
gardant le hostname d'origine pour SNI et le certificat TLS. Le transport ne
consulte pas les variables de proxy de l'environnement. Une empreinte SHA-256
reste obligatoire en aval : le contrôle réseau et l'intégrité de l'artefact
protègent deux risques distincts.

> **Précondition d'exploitation : un seul worker gateway.** L'activation
> provisoire, son verrou et son bail sont locaux au processus FastAPI. L'unité
> systemd officielle lance bien `--workers 1`. N'exécutez pas cette commande
> contre une gateway lancée manuellement avec plusieurs workers ; certaines
> variables usuelles (`WEB_CONCURRENCY`, `UVICORN_WORKERS`) sont refusées si
> elles annoncent une valeur supérieure à 1, mais elles ne peuvent pas détecter
> toutes les topologies improvisées.

### La simulation est le défaut

```bash
# Simule — n'écrit rien, ne télécharge rien, ne charge aucun modèle
./venv/bin/python cli.py bootstrap-apply /tmp/plan.json \
    --allowed-root /models --allowed-root /opt/llama.cpp \
    --allowed-root /var/lib/llm-gateway --allowed-root /etc/llm-gateway \
    --models-dir /models --registry /var/lib/llm-gateway/models.yaml \
    --runtime-root /opt/llama.cpp \
    --calibration-report-dir /var/lib/llm-gateway/calibration \
    --env-file /etc/llm-gateway/env \
    --base-url https://eva.example.edu --admin-url http://127.0.0.1:8000 \
    --vram-budget-gb 43.6 \
    --accept-license qwen2.5-0.5b-instruct-q4_k_m \
    --license-reference CHG-2026-081

# Applique réellement — le drapeau est obligatoire et il n'a pas d'équivalent court
./venv/bin/python cli.py bootstrap-apply /tmp/plan.json --apply \
    --allowed-root /models --allowed-root /opt/llama.cpp \
    --allowed-root /var/lib/llm-gateway --allowed-root /etc/llm-gateway \
    --models-dir /models --registry /var/lib/llm-gateway/models.yaml \
    --runtime-root /opt/llama.cpp \
    --calibration-report-dir /var/lib/llm-gateway/calibration \
    --env-file /etc/llm-gateway/env \
    --base-url https://eva.example.edu --admin-url http://127.0.0.1:8000 \
    --vram-budget-gb 43.6 \
    --accept-license qwen2.5-0.5b-instruct-q4_k_m \
    --license-reference CHG-2026-081
```

La simulation n'exige aucun secret : ses raccords HTTP sont décrits mais jamais
appelés. Sur le parcours de production, `--env-file /etc/llm-gateway/env` charge
la configuration exacte du service et fournit son `ADMIN_SECRET` sans créer un
second fichier secret. `--admin-secret-file` reste une surcharge possible pour
un déploiement autonome ; il doit être régulier, non symlink, appartenir à root
ou à l'utilisateur courant et être en mode 0600. Un secret n'est jamais accepté
en argument de CLI.

Une installation réelle de runtime exige `--env-file` et un `--min-build`
strictement positif dans le plan. La commande vérifie que `--registry` correspond
à `MODELS_CONFIG_PATH` et que le binaire publié correspond à
`LLAMA_SERVER_BIN`. Après succès complet, elle met atomiquement à jour ce binaire
et `LLAMA_SERVER_MIN_BUILD` dans l'EnvironmentFile ; un échec ou un résultat
partiel ne durcit jamais prématurément le service.

Un mode d'exécution ne s'obtient **jamais** par omission d'argument. Une
simulation complète sort en **3**, jamais en 0 : rien n'a été appliqué, et un
script d'exploitation ne doit pas pouvoir confondre « j'ai simulé sans
problème » et « la machine est installée ».

### Options

| Option | Rôle |
|---|---|
| `--apply` | Appliquer réellement. Sans lui, la commande simule. |
| `--json` | Rapport d'installation JSON au lieu du rendu français. |
| `--allowed-root` | Répertoire que l'application a le droit de toucher. **Répétable et obligatoire** : une liste vide n'autorise rien. |
| `--catalog` | Catalogue de modèles approuvés (défaut : celui du dépôt). |
| `--models-dir` | Volume où atterrissent les GGUF. |
| `--registry` | `models.yaml` à écrire. |
| `--runtime-root` | Racine versionnée où installer les releases de `llama-server`. |
| `--llama-server-bin` | Binaire à employer pour la calibration ; sinon release installée ou configuration de la gateway. |
| `--env-file` | EnvironmentFile systemd à relire et durcir. Obligatoire pour poser réellement un runtime ; en production : `/etc/llm-gateway/env`. |
| `--calibration-report-dir` | Répertoire des preuves JSON de calibration. |
| `--calibration-port` | Port loopback du `llama-server` isolé de calibration (défaut : `19091`). |
| `--calibration-load-timeout` | Borne de chargement ; sinon `MODEL_LOAD_TIMEOUT_SECONDS`. |
| `--base-url` | **Origin** publique de recette, nginx compris (sans chemin, query ni fragment). HTTPS est exigé hors loopback. |
| `--admin-url` | Origin directe de la gateway pour `/ready` et `/admin`, sans chemin/query/fragment et limitée à loopback afin que `ADMIN_SECRET` ne quitte pas l'hôte. |
| `--admin-secret-file` | Surcharge par fichier régulier privé, non symlink, mode 0600 et propriétaire attendu ; sinon `ADMIN_SECRET` vient de l'EnvironmentFile relu ou de l'environnement. La valeur n'est jamais acceptée en argv. |
| `--accept-license` | ID dont l'opérateur accepte explicitement la licence. Répétable. |
| `--license-reference` | Référence technique de changement/ticket associée aux acceptations ; n'y placez ni nom ni email. |
| `--ttft-threshold-ms` | Seuil de TTFT en millisecondes ; `0` mesure sans seuil. |
| `--ttft-gate` | Transforme le dépassement du seuil TTFT en échec. |
| `--runtime-version` | Build servi (ex. `b6042`). S'il est aussi déductible du plan, toute divergence est refusée. |
| `--hardware-fingerprint` | Empreinte matérielle (§9). Si l'inventaire du plan permet de la recalculer, toute divergence est refusée. |
| `--vram-budget-gb` | Budget VRAM net de l'hôte, en Go. |

La version runtime et l'empreinte matérielle servent à décider si une preuve de
calibration est réutilisable. Quand le plan les porte, la CLI les reconstruit
depuis le document validé ; une valeur explicitement fournie qui diverge est un
refus, jamais un remplacement silencieux. Avant de réutiliser une preuve — et
avant toute nouvelle mesure — l'applicateur sonde à nouveau le binaire courant,
les UUID GPU visibles, leur modèle, VRAM, pilote et compute capability. Un plan
ancien ne peut donc pas étiqueter le matériel ou le runtime courant avec son
ancien état.

### Grille des exit codes

| Code | Signification | Conduite à tenir |
|---:|---|---|
| 0 | Installation complète et prouvée | Rien — le rapport est archivable en l'état. |
| 1 | Échec, ou plan inapplicable | Lire les échecs du rapport. Le plan a pu être refusé à la relecture. |
| 2 | Commande mal formée, ou câblage incomplet | Corriger les options. **Rien n'a été entamé.** |
| 3 | Partiel — dont **toute simulation** | Normal après une simulation. Après un `--apply`, lire ce qui n'a pas été tenté. |
| 4 | L'applicateur lui-même a cassé | Ce n'est pas un diagnostic sur l'hôte. À remonter comme un défaut. |

La séparation 1 / 2 / 4 est la même que pour `bootstrap-plan` : « l'hôte est
bloqué », « la commande est mal formée », « l'outil a cassé » sont trois
conséquences différentes, et un script d'exploitation doit pouvoir les
distinguer.

### Ce que l'applicateur garantit

- **Les impossibilités prévisibles sont refusées avant toute mutation.** Une
  action sans exécuteur ou un triplet d'activation non compensable arrête la
  commande au pré-vol.
- **Un échec métier peut laisser les étapes antérieures appliquées.** Runtime,
  téléchargements et registre sont idempotents et restent en place pour une
  reprise ; le journal les distingue des étapes non tentées. Ce n'est pas une
  transaction globale, et la documentation ne prétend plus le contraire.
- **L'activation provisoire, elle, est compensée et résistante au crash.** Le
  fichier reste `enabled: false` pendant la recette ; seule la gateway
  mono-worker voit temporairement le modèle, sous un bail borné que la gateway
  annule automatiquement si l'applicateur disparaît. Un échec ferme d'abord l'admission
  en mémoire, laisse terminer les requêtes déjà actives, puis décharge sans
  forçage. Un redémarrage avant confirmation relit donc l'état désactivé. Un
  modèle actif avant le run n'est jamais désactivé sous couvert de rollback.
  La durée du bail additionne les pires bornes séquentielles de readiness,
  identité, chargement, stream, log d'usage, nettoyage et confirmation, plus
  une marge ; si le total dépasse 3600 s, la commande refuse au lieu de le
  tronquer et d'expirer pendant une recette encore valide.
- **La synchronisation live recoupe chaque snapshot.** Le client loopback
  appelle `POST /admin/models/{id}/bootstrap-sync` sous `ADMIN_SECRET` pour
  `activate`, `confirm` ou `rollback`. Chaque transition porte le SHA-256 exact
  de `models.yaml`; la gateway refuse un fichier ou un autre modèle modifié
  concurremment. Une réponse de confirmation perdue reste compensable et un
  rollback arrivé après l'expiration du bail est idempotent.
- **Une annulation n'abandonne pas un thread d'écriture.** La persistance du
  registre doit terminer avant que l'annulation soit propagée ; le rollback lit
  ainsi l'état final réel, jamais un `enabled: false` qui serait remplacé juste
  après par un thread encore actif.
- **Une preuve n'est jamais présumée.** La calibration autorise seulement la
  fenêtre provisoire ; seule la calibration ET la recette du premier token
  réussies confirment l'activation. Aucune valeur par défaut ni équivalent
  approchant n'est accepté.
- **Le journal distingue « sauté » de « non tenté ».** Les étapes qui suivent un
  échec n'ont pas été atteintes ; ce n'est pas la même information pour qui
  diagnostique.
- **Le plan relu est revalidé intégralement.** Version de schéma, cohérence des
  champs dérivés, bloqueurs recalculés depuis les sections, numérotation des
  étapes, absence de secret : un document retouché à la main est refusé.
- **Le même instantané de plan est câblé puis exécuté.** La CLI ne relit pas le
  chemin après avoir dérivé runtime, modèles et matériel ; remplacer le fichier
  pendant l'exécution ne peut pas substituer un second plan.
- **Le serveur de calibration est identifié.** Un port déjà occupé est refusé
  avant lancement ; le processus est encore vivant après `/health` et doit
  annoncer l'alias exact du modèle via `/v1/models`.
- **La mise en page de `models.yaml` ne décide pas de l'issue** (COR-028).
  L'écriture et l'activation localisent une entrée quel que soit l'ordre de ses
  clés — un fichier déjà passé par une mutation admin a `capabilities` avant
  `id`, et l'étape `enable_model` le refusait auparavant. Ce localisateur est
  celui de `model_registry`, pas un second : il n'y a qu'une politique
  d'écriture de `models.yaml` dans le dépôt. Une entrée réellement introuvable
  ou présente deux fois reste **refusée**, sans rien écrire.
- **Aucun secret dans la sortie**, y compris dans un message d'erreur, y compris
  le chemin du fichier de plan.

### Le rapport d'installation

Chaque exécution produit le document qu'un opérateur archive : versions,
empreintes, licences, matériel, modèle, performances et contrôles, plus l'état
des **sept conditions du jalon M2** et la preuve de chacune.

Deux propriétés à connaître :

- il **ne prétend jamais plus que ce qui a été fait**. Une installation
  partielle se lit comme telle au premier coup d'œil ;
- il **distingue le constat de l'hypothèse**. Certaines variantes d'artefact
  `llama-server` sont retenues sur hypothèse et non sur constat ; le rapport le
  dit, parce que c'est exactement ce qu'un lecteur pressé prendrait pour un fait
  vérifié six mois plus tard.

#### Ce qui prouve quoi (COR-026)

L'action `verify_artifact` sert **deux domaines** : l'archive de `llama-server`
et les ensembles de GGUF. Une étape ne prouve donc une condition que si sa
**cible** appartient au domaine de cette condition — la vérification du GGUF
d'un modèle ne prouve rien du runtime, et réciproquement. Une vérification dont
la cible n'est rattachable à aucun domaine ne prouve rien du tout, et le rapport
écrit pourquoi.

Conséquence pratique, sur un hôte où les GGUF étaient **déjà présents et
attestés** : `bootstrap-plan` n'émet plus d'étape `download_model` (AUT-014), et
la condition « Modèle en place à révision figée » est alors tenue par la seule
`verify_artifact` du modèle. Le rapport écrit littéralement *« aucune étape
"download_model" n'était au programme »* — à ne pas confondre avec *« rien
n'atteste cette condition »*, qui reste le verdict quand aucune vérification
n'a eu lieu. Les deux se lisent différemment parce qu'ils ne se réparent pas de
la même façon.

Élargir ce que le rapport sait voir ne l'a pas rendu plus complaisant : une
vérification en échec, sautée, simulée ou jamais tentée laisse sa condition
**non satisfaite**, exactement comme avant.

`bootstrap-apply` ne remplace pas `doctor` (§8). `doctor` répond à « cet hôte
peut-il démarrer **maintenant** ? » en sondant le système vivant ; le rapport
d'installation répond à « qu'a produit **cette** installation, et qu'est-ce qui
reste à faire ? » et ne périme pas. Les deux sont nécessaires.

---

## Bonnes pratiques

### Politique de gestion des accès

- Créer **une clé par projet** (pas une clé globale par utilisateur), pour pouvoir
  révoquer un accès spécifique sans impacter les autres travaux
- Fixer une **date d'expiration** pour les accès temporaires (stagiaires, visiteurs)
- Revoir les accès inactifs chaque début de semestre (`llmgw list-users`)

### Politique de gestion des modèles

- Maintenir `vram_gb` à jour dans `models.yaml` si vous modifiez `ctx_size` ou `parallel`
  (la VRAM réelle change avec le KV cache — voir formule dans `docs/architecture.md`)
- Désactiver (`enabled: false`) les modèles dont le fichier `.gguf` n'est pas encore
  téléchargé, plutôt que de les supprimer
- Surveiller `idle_seconds` dans `/admin/status` pour identifier les modèles rarement
  utilisés qui pourraient être désactivés pour libérer du budget VRAM
- Surveiller `capacity_queue.waiters` dans `/admin/status` : une valeur non nulle
  récurrente indique une saturation VRAM ou des générations trop longues. Ajuster
  `CAPACITY_QUEUE_TIMEOUT_SECONDS` et `CAPACITY_QUEUE_MAX_WAITERS` avec prudence.
- Pour tout modèle vision : s'assurer que `mmproj_path` est renseigné **avant**
  d'activer le modèle — un modèle vision sans `mmproj_path` provoque des HTTP 500
  silencieux (llama-server démarre, mais échoue à chaque requête avec image)
- Pour les **modèles MoE** (Mixtral, Qwen-MoE, MiniMax, Gemma-MoE…) : toujours activer
  `cpu_moe: true` dans `llama_params` si les experts FFN ne tiennent pas en VRAM. Le
  `vram_gb` doit refléter la consommation **avec** `cpu_moe` (attention + embeddings
  seulement). Sans ce flag, llama-server crashe avec exit code 1 dès qu'un autre modèle
  est chargé simultanément. Corriger à chaud via `PATCH /admin/models/{id}`.
- Si un modèle crashe au chargement (exit code 1), les **dernières lignes de stderr**
  sont désormais incluses dans le message d'erreur retourné au client et dans les logs
  gateway — chercher `Stderr (dernières N lignes)` dans `journalctl -u llm-gateway`.

### Sécurité de l'`ADMIN_SECRET`

- Minimum 32 caractères, non-placeholder : un secret plus faible désactive les
  routes `/admin` (fail-closed 503) et est signalé bloquant par `doctor`
- Ne jamais transmettre l'`ADMIN_SECRET` par email ou messagerie non chiffrée
- Si compromis : générer un nouveau secret, mettre à jour `/etc/llm-gateway/env`,
  et redémarrer le service

```bash
# Régénérer l'ADMIN_SECRET
NEW_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
sudo sed -i "s/^ADMIN_SECRET=.*/ADMIN_SECRET=$NEW_SECRET/" /etc/llm-gateway/env
sudo systemctl restart llm-gateway
echo "Nouveau ADMIN_SECRET : $NEW_SECRET"
```

### Sauvegarde de la base de données

```bash
# Sauvegarde manuelle (SQLite WAL — utiliser sqlite3 pour une copie cohérente)
sqlite3 /var/lib/llm-gateway/gateway.db ".backup '/backup/gateway-$(date +%Y%m%d).db'"

# Sauvegarder aussi le registre des modèles
cp /var/lib/llm-gateway/models.yaml "/backup/models-$(date +%Y%m%d).yaml"

# Sauvegarde automatique quotidienne (cron)
echo "0 3 * * * llmservice sqlite3 /var/lib/llm-gateway/gateway.db \
  \".backup '/backup/gateway-\$(date +\%Y\%m\%d).db'\"" \
  | sudo tee /etc/cron.d/llm-gateway-backup
```
