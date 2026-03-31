# Guide administrateur — Cluster EVA Inference Gateway

Ce document s'adresse à l'administrateur du gateway : gestion des utilisateurs,
des clés API, surveillance du système et reporting d'usage.

Deux interfaces sont disponibles :
- **CLI** (`cli.py`) : à utiliser directement sur le serveur, idéal pour le quotidien
- **API REST** (`/admin/...`) : accessible depuis le réseau campus uniquement, utile pour l'automatisation

---

## Table des matières

1. [Accès administrateur](#1-accès-administrateur)
2. [Gestion des utilisateurs](#2-gestion-des-utilisateurs)
3. [Gestion des clés API](#3-gestion-des-clés-api)
4. [Surveillance du système](#4-surveillance-du-système)
5. [Rapports d'usage](#5-rapports-dusage)
6. [Contrôle du modèle](#6-contrôle-du-modèle)
7. [Référence API REST admin](#7-référence-api-rest-admin)

---

## 1. Accès administrateur

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

```bash
# Récupérer l'ADMIN_SECRET
sudo grep ADMIN_SECRET /etc/llm-gateway/env

# L'exporter pour les exemples suivants
export ADMIN_SECRET="votre_secret_ici"
export GW="https://llm.univ-pau.fr"
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
| `monthly_token_limit` | 0 | Quota tokens/mois (0 = illimité) |
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

### État en temps réel

```bash
# CLI
llmgw status

# Sortie :
#   État du modèle  : ready
#   Modèle          : llama-3.3-70b-instruct
#   Chemin          : /models/Llama-3.3-70B-Instruct-Q4_K_M.gguf
#   PID             : 18432
#   Uptime          : 3742s
#   Idle depuis     : 42.3s
#   Idle timeout    : 300s
#
#   GPU layers      : 999
#   Context size    : 32768
#   Parallel slots  : 4
#   Flash Attention : True
#   KV cache type   : K=q8_0 / V=q8_0

# API REST
curl -s "$GW/admin/status" \
  -H "Authorization: Bearer $ADMIN_SECRET" | python3 -m json.tool
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
# Modèle chargé, inactif  : ~40500 MiB utilisés, ~130W
# Inférence active (70B)  : ~41000 MiB utilisés, ~320W
# Modèle déchargé (idle)  : ~200 MiB,            ~28W  ← GPU libéré ✓
```

### Logs en temps réel

```bash
# Gateway (démarrages, requêtes, erreurs)
sudo journalctl -u llm-gateway -f

# llama-server (chargement, inférence)
tail -f /var/log/llm-gateway/llama-server.log

# Filtrer les erreurs uniquement
sudo journalctl -u llm-gateway -p err -f

# Dernières 24h
sudo journalctl -u llm-gateway --since "24 hours ago" | less
```

### Métriques Prometheus (intégrées à llama-server)

Lorsque le modèle est chargé, les métriques sont disponibles directement
depuis le serveur :

```bash
# Métriques brutes (format Prometheus)
curl http://127.0.0.1:8081/metrics

# Métriques intéressantes :
# llama_prompt_tokens_total        — tokens en entrée traités
# llama_completion_tokens_total    — tokens générés
# llama_tokens_per_second          — débit en génération
# llama_kv_cache_usage_ratio       — taux d'utilisation du KV cache
# llama_requests_processing        — requêtes en cours
# llama_requests_deferred          — requêtes en attente de slot
```

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

---

## 6. Contrôle du modèle

### Forcer le déchargement du modèle

Utile pour libérer le GPU pour d'autres tâches (entraînement, etc.)
ou pour vérifier que la VRAM se libère correctement.

```bash
# API REST
curl -s -X POST "$GW/admin/unload" \
  -H "Authorization: Bearer $ADMIN_SECRET"
# → {"message": "Modèle déchargé. GPU libéré."}

# Vérifier dans les 3s
nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits
# → ~200  (MiB)
```

### Redémarrer le service

```bash
# Arrêt propre : décharge le modèle avant d'arrêter
sudo systemctl restart llm-gateway

# Vérifier le redémarrage
sudo journalctl -u llm-gateway -f --since now
```

---

## 7. Référence API REST admin

Toutes les routes nécessitent : `Authorization: Bearer <ADMIN_SECRET>`

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

### Usage

| Méthode | Route | Paramètres |
|---------|-------|------------|
| `GET` | `/admin/usage` | `username`, `from_date`, `to_date`, `limit` |
| `GET` | `/admin/usage/summary` | `from_date`, `to_date` |

### Système

| Méthode | Route | Description |
|---------|-------|-------------|
| `GET` | `/admin/status` | État du modèle, PID, uptime, params GPU |
| `POST` | `/admin/unload` | Forcer le déchargement du modèle |

---

## Bonnes pratiques

### Politique de gestion des accès

- Créer **une clé par projet** (pas une clé globale par utilisateur), pour pouvoir
  révoquer un accès spécifique sans impacter les autres travaux
- Fixer une **date d'expiration** pour les accès temporaires (stagiaires, visiteurs)
- Revoir les accès inactifs chaque début de semestre (`llmgw list-users`)

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

# Sauvegarde automatique quotidienne (cron)
echo "0 3 * * * llmservice sqlite3 /var/lib/llm-gateway/gateway.db \
  \".backup '/backup/gateway-\$(date +\%Y\%m\%d).db'\"" \
  | sudo tee /etc/cron.d/llm-gateway-backup
```
