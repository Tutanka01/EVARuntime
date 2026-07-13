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

> **Fail-closed :** si `ADMIN_SECRET` est vide ou laissé à sa valeur d'exemple
> (`CHANGE_ME_*`), toutes les routes `/admin/` répondent 503 tant qu'un secret
> fort n'est pas configuré. Générer avec :
> `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`

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
#       "path": "/models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
#     }
#   ]
# }
```

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
| `eva_vram_used_gb` / `eva_vram_total_gb` / `eva_vram_available_gb` | gauge | — | Budget VRAM comptabilisé |
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
# Pré-charger le modèle 8B
curl -s -X POST "$GW/admin/models/llama-3.1-8b-instruct/load" \
  -H "Authorization: Bearer $ADMIN_SECRET"
# → {"message": "Modèle 'llama-3.1-8b-instruct' en cours de chargement."}

# Vérifier le statut après quelques secondes
curl -s "$GW/admin/models/llama-3.1-8b-instruct" \
  -H "Authorization: Bearer $ADMIN_SECRET" | python3 -m json.tool
```

### Décharger un modèle spécifique

Utile pour libérer de la VRAM pour un autre modèle, ou forcer le rechargement
d'un modèle dont les paramètres ont changé.

```bash
# Décharger le 70B
curl -s -X POST "$GW/admin/models/llama-3.3-70b-instruct/unload" \
  -H "Authorization: Bearer $ADMIN_SECRET"
# → {"message": "Modèle 'llama-3.3-70b-instruct' déchargé."}

# Vérifier la VRAM libérée
nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits
# → ~200  (MiB)
```

### Décharger tous les modèles

```bash
# Décharger tous les modèles chargés sans arrêter la gateway
curl -s -X POST "$GW/admin/unload" \
  -H "Authorization: Bearer $ADMIN_SECRET"
# → {"message": "Tous les modèles déchargés. VRAM entièrement libérée."}
```

En cluster, l'orchestrateur conserve ses clients et son heartbeat après cette
action : il peut recharger un modèle à la requête suivante. La route répond 409
si une génération est encore active, ou 503 si un agent n'a pas confirmé le
déchargement; elle n'annonce jamais une libération partielle comme réussie.

### Activer / désactiver un modèle du registre

Désactiver un modèle le rend **invisible aux clients** (`GET /v1/models` ne le liste plus)
et les requêtes vers cet ID reçoivent un `403`. Si le modèle est actuellement chargé,
il est automatiquement déchargé.

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

Le PATCH déclenche un **hot-reload** : le modèle est déchargé immédiatement
(sa VRAM est libérée), le registre est mis à jour, et le prochain appel relancera
llama-server avec les nouveaux paramètres.

> **`llama_params` utilise une sémantique de remplacement complet.** Tous les champs
> doivent être fournis — il n'y a pas de merge partiel. Récupérez les valeurs actuelles
> via `GET /admin/models/{id}` avant de faire le PATCH.

```bash
# Récupérer la config actuelle d'un modèle
curl -s "$GW/admin/models/qwen3.5-9b-q5_k_m" \
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
- L'`id` doit correspondre à `^[a-z0-9][a-z0-9._-]*$`
- Le `path` doit être absolu et se terminer par `.gguf`
- Le fichier `.gguf` doit exister sur disque
- Si `ALLOWED_MODEL_DIRS` est configuré, le chemin doit être sous ces répertoires
- `vram_gb` doit être entre 0.5 et le budget VRAM net
- Si `mmproj_path` est fourni, il est validé de la même façon que `path` (absolu, `.gguf`)

**Pour un modèle vision**, inclure `mmproj_path` dans le corps de la requête :

```bash
curl -s -X POST "$GW/admin/models" \
  -H "Authorization: Bearer $ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "llava-7b",
    "path": "/models/llava-v1.6-mistral-7b-Q4_K_M.gguf",
    "mmproj_path": "/models/llava-v1.6-mistral-7b-mmproj-f16.gguf",
    "description": "LLaVA 1.6 Mistral 7B — vision + texte",
    "vram_gb": 6.0,
    "enabled": true,
    "capabilities": ["text_generation", "vision", "streaming"],
    "llama_params": {
      "n_gpu_layers": 999,
      "ctx_size": 8192,
      "parallel": 4,
      "batch_size": 2048,
      "ubatch_size": 512,
      "cache_type_k": "q8_0",
      "cache_type_v": "q8_0",
      "flash_attn": true,
      "threads": 4,
      "threads_http": 2
    }
  }' | python3 -m json.tool
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
| `GET` | `/admin/models/{model_id}` | Détail d'un modèle (registre + état live) |
| `PATCH` | `/admin/models/{model_id}` | Modifier un modèle — `enabled`, `vram_gb`, `description`, `llama_params` (hot-reload) |
| `DELETE` | `/admin/models/{model_id}` | Supprimer un modèle (seulement si non chargé) |
| `POST` | `/admin/models/{model_id}/load` | Pré-charger un modèle en VRAM |
| `POST` | `/admin/models/{model_id}/unload` | Décharger un modèle spécifique |

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
| `POST` | `/admin/unload` | Décharger tous les modèles chargés |

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
#     { "id": "llama-3.3-70b-instruct", "state": "ready", "vram_gb": 42.0, ... },
#     { "id": "llama-3.1-8b-instruct", "state": "unloaded", ... }
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
| `GET` | `/admin/metrics/status-codes` | `period=24h\|7d` | Distribution des codes HTTP |
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
