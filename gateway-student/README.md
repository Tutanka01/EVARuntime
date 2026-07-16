# Gateway Étudiante — EVARuntime

Edge proxy durci qui expose l'inférence LLM EVA au réseau étudiant UPPA, sans toucher
à la gateway admin existante et sans ouvrir le GPU directement.

---

## En bref

```
Étudiants (VLAN étudiant)
    │  HTTPS TLS 1.3
    ▼
[gw-étudiante]  ← CE PROJET
    │  HTTPS + mTLS  (réseau admin uniquement)
    ▼
[gw-admin]  →  llama-server  →  L40S
```

La gateway étudiante :
- authentifie via `Bearer llmstu-…` (clés dédiées, base SQLite séparée)
- applique **quatre couches de rate limiting** (burst court, RPM, tokens/heure, tokens/jour) **plus une limite de concurrence** par étudiant
- valide et **normalise** chaque requête (allowlist-first, strip des paramètres dangereux)
- relaie exclusivement vers l'URL admin configurée en dur — pas de SSRF possible
- n'expose aucune route `/admin/*`, aucun endpoint de gestion GPU

---

## Démarrage local (développement)

```bash
cd gateway-student
python -m venv .venv
source .venv/bin/activate       # Windows : .venv\Scripts\activate
pip install -r requirements.txt

cp deploy/env.example .env
# Éditer .env : remplacer les CHANGE_ME_* par de vraies valeurs

python cli.py init-db
python cli.py add-student alice --email alice@univ-pau.fr
python cli.py create-key alice --expires-at 2026-08-31T23:59:59+00:00

uvicorn main:app --host 127.0.0.1 --port 8001 --reload
```

En local, pointer `UPSTREAM_BASE_URL` vers un mock ou une gateway de test.
En production, c'est le vhost interne mTLS de la gateway admin.

---

## Variables d'environnement essentielles

| Variable | Défaut | Description |
|---|---|---|
| `UPSTREAM_BASE_URL` | `https://llm-internal.eva.univ-pau.fr` | URL du vhost interne mTLS admin |
| `UPSTREAM_API_KEY` | *(obligatoire)* | Bearer envoyé vers la gw-admin |
| `UPSTREAM_CA_PATH` | `/etc/…/ca-uppa.pem` | CA pour vérifier le cert serveur admin |
| `UPSTREAM_CLIENT_CERT_PATH` | `/etc/…/gw-student.crt` | Cert client mTLS |
| `UPSTREAM_CLIENT_KEY_PATH` | `/etc/…/gw-student.key` | Clé privée cert client |
| `ALLOWED_MODELS` | `llama-3.1-8b-instruct,qwen-9b` | Modèles accessibles aux étudiants |
| `AUDIT_HMAC_SECRET` | *(obligatoire)* | Secret HMAC pour pseudonymisation des IP |
| `AUDIT_LOG_PATH` | `/var/log/…/audit.jsonl` | Fichier de logs d'audit |
| `DB_PATH` | `/var/lib/…/students.db` | Base SQLite étudiante |

> **Important :** l'application refuse de démarrer si `UPSTREAM_API_KEY` ou
> `AUDIT_HMAC_SECRET` correspondent à une valeur par défaut connue **ou** commencent
> par le préfixe `CHANGE_ME`, ou font moins de 32 caractères.

Voir `deploy/env.example` pour la liste complète.

---

## Rate limiting — comportement

Chaque requête POST `/v1/chat/completions` passe par **4 vérifications successives**,
du plus court au plus long :

| Couche | Fenêtre | Limite | Configurable |
|---|---|---|---|
| **Burst** | 10 s (configurable) | 3 req | global (tous les étudiants) |
| **RPM** | 60 s glissantes | 10 req | par étudiant en base |
| **Tokens/heure** | 60 min glissantes | 20 000 tokens | par étudiant en base |
| **Tokens/jour** | depuis minuit UTC | 100 000 tokens | par étudiant en base |
| **Concurrence** | instantané | 1 requête concurrente | par étudiant en base |

Les 429 incluent les headers `X-RateLimit-Limit`, `X-RateLimit-Remaining`,
`X-RateLimit-Reset` et `Retry-After` pour un retry intelligent côté client.

---

## CLI admin — référence rapide

```
python cli.py --help
```

### Tableau de bord

```bash
python cli.py stats                      # dashboard : req/tokens du jour + 7j + modèles
python cli.py usage-report --days 7      # classement par tokens sur 7 jours
python cli.py usage-report --days 30 --user alice  # zoom sur un étudiant
python cli.py expiring-keys --days 30    # clés qui expirent dans < 30 jours
```

### Gestion des étudiants

```bash
# Créer
python cli.py add-student alice --email alice@univ-pau.fr
python cli.py add-student alice --rpm 20 --daily-tokens 200000 --notes "TER 2026"

# Lister / modifier
python cli.py list-students
python cli.py set-quota alice --rpm 20 --daily-tokens 200000 --hourly-tokens 30000

# Suspendre / réactiver
python cli.py deactivate-student alice   # suspension immédiate
python cli.py activate-student alice

# Supprimer (RGPD)
python cli.py delete-student alice --yes
```

### Gestion des clés API

```bash
# Créer (expires-at OBLIGATOIRE, avec timezone)
python cli.py create-key alice --expires-at 2026-08-31T23:59:59+00:00 --name TP-S2

# Lister
python cli.py list-keys                  # toutes les clés
python cli.py list-keys --user alice     # clés d'un étudiant

# Révoquer
python cli.py revoke-key llmstu-abc12ef
```

---

## Arborescence

```
gateway-student/
├── main.py           — FastAPI : 3 routes + handlers globaux
├── config.py         — Settings (pydantic-settings, validation au démarrage)
├── database.py       — SQLite WAL : users, api_keys, usage_log + fonctions admin
├── auth.py           — Vérification Bearer + lookup clé SHA-256
├── rate_limiter.py   — Burst, RPM, tokens/h, tokens/j, concurrence
├── policy.py         — Normalisation allowlist-first du body
├── upstream.py       — Client httpx partagé, mTLS, streaming SSE
├── audit.py          — Log structuré JSON (sans contenu, IP hashée HMAC)
├── schemas.py        — Modèles Pydantic (ChatMessage, NormalizedRequest)
├── cli.py            — CLI admin rich (stats, usage, clés, quotas…)
├── requirements.txt
├── docs/
│   ├── api.md        — Référence API pour les étudiants / intégrateurs
│   ├── deployment.md — Déploiement prod étape par étape
│   ├── operations.md — Runbook admin (incidents, fin de semestre…)
│   ├── security.md   — Modèle de menace et couches de défense
│   └── architecture.md
├── deploy/
│   ├── env.example                     — Template de configuration
│   ├── llm-gateway-student.service     — Unité systemd durcie
│   ├── nginx.conf                      — TLS 1.3, limits, WAF L7
│   ├── nftables.conf                   — Pare-feu deny-by-default
│   └── sysctl.conf                     — Hardening kernel
└── tests/
    ├── conftest.py
    ├── test_policy.py             — 45 tests sur la normalisation des requêtes
    ├── test_rate_limiter.py       — 13 tests async sur burst/RPM/concurrence
    ├── test_upstream.py           — tests du relais upstream (mTLS, erreurs, SSE)
    ├── test_database_hardening.py — tests de durcissement de la base SQLite
    └── test_config.py             — tests de validation de la configuration
```

---

## Tests

```bash
python -m pytest tests/ -v
```

---

## Décisions à valider avant mise en production

1. **Modèles exposés** — sous-ensemble de la registry admin (`ALLOWED_MODELS`)
2. **Quotas par défaut** — valeurs dans `deploy/env.example`, ajustables par étudiant
3. **mTLS côté admin** — la DSI doit émettre `gw-student.crt` et configurer le vhost interne
4. **Rétention des logs d'audit** — 90 jours par défaut, à valider avec le DPO
5. **Provisioning** — CLI MVP puis portail SSO Shibboleth en cible

Voir `docs/deployment.md` pour la procédure complète.
