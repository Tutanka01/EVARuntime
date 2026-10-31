# Compiler llama.cpp sur macOS (Apple Silicon — Metal)

Ce document couvre uniquement la compilation et l'installation du binaire
`llama-server` sur un Mac à puce Apple Silicon. Le déploiement EVARuntime est
décrit dans [`deployment.md`](deployment.md), section « Déploiement macOS ».

Deux voies existent :

| Voie | À qui elle convient | Limite principale |
|---|---|---|
| [Homebrew](#voie-homebrew-brew-install-llamacpp) | Test rapide, station de développement | Version imposée par la formule, backend Metal parfois absent du bottle, plancher supply-chain non maîtrisé |
| [Depuis les sources](#voie-recommandée--build-depuis-les-sources) | Production (**recommandée**) | Temps de compilation (~5–15 min) |

La version Homebrew est un compromis pratique : la formule suit les tags
officiels `bNNNN` de llama.cpp mais avec un décalage volontaire, et le bottle
précompilé a déjà été livré **sans le backend Metal** (inférence CPU silencieuse,
cas rapporté en juillet 2026). Pour une gateway en production, compilez depuis
les sources : c'est la seule façon de connaître et de contrôler le numéro de
build servi aux clients, que `LLAMA_SERVER_MIN_BUILD` plafonne par sécurité
(voir [§ Plancher supply-chain](#plancher-supply-chain-et-version-homebrew)).

Le checkout utilisé partout ici est `$HOME/src/llama.cpp`. Le binaire est publié
dans `$HOME/opt/llama.cpp/releases/<release>/llama-server`, puis exposé par le
lien symbolique `$HOME/opt/llama.cpp/current/llama-server`. Ne lancez pas un
serveur permanent à la main : EVARuntime gère le cycle de vie de ses processus.

## Vérifier la machine et le compilateur

```bash
uname -m                    # résultat attendu : arm64
sw_vers -productVersion     # version de macOS
xcode-select -p             # chemin des Command Line Tools, ex. /Library/Developer/CommandLineTools
```

Le résultat attendu est `arm64`. Si `uname -m` répond `x86_64`, votre shell
tourne sous Rosetta : quittez ce terminal et rouvrez-en un normal, sinon vous
compileriez un binaire x86_64 lent et inutile (voir
[dépannage](#problèmes-de-compilation)).

## Dépendances

```bash
# Command Line Tools : compilateur clang, git, et la toolchain Metal
# (xcrun metal / metallib) utilisée pendant la compilation.
xcode-select --install

# Outils de build.
brew install cmake ninja
```

## Voie Homebrew (`brew install llama.cpp`)

```bash
brew install llama.cpp
which llama-server        # /opt/homebrew/bin/llama-server sur Apple Silicon
/opt/homebrew/bin/llama-server --version
```

Homebrew installe `llama-server` dans `/opt/homebrew/bin/` (Apple Silicon) ou
`/usr/local/bin/` (Intel). C'est ce chemin que le template macOS d'EVARuntime
pose par défaut dans `LLAMA_SERVER_BIN`.

Limites à connaître avant de choisir cette voie en production :

- **Version décalée** : la formule ne suit pas chaque tag `bNNNN` ; elle est
  mise à jour par lots (environ une fois tous les deux jours). Le build servi
  peut donc être antérieur à celui que votre politique exige.
- **Backend Metal parfois absent du bottle** : un bottle précompilé sans
  `libggml-metal.dylib` fait tourner le modèle sur CPU sans aucune erreur —
  seulement un débit effondré. Vérifiez toujours que Metal est réellement
  utilisé ([§ Vérifier que Metal est utilisé](#vérifier-que-metal-est-réellement-utilisé)).
- **Plancher supply-chain** : voir ci-dessous.

### Plancher supply-chain et version Homebrew

EVARuntime refuse de démarrer sur un `llama-server` dont le numéro de build est
inférieur à `LLAMA_SERVER_MIN_BUILD` (mitigation GHSA-8947-pfff-2f3c ;
comportement détaillé dans
[deployment.md §11](deployment.md#mettre-à-jour-llamacpp)). Deux conséquences
pour la voie Homebrew :

1. `brew upgrade llama.cpp` ne garantit pas d'atteindre le build exigé : tant
   que la formule n'est pas bumpée, `doctor` continue de signaler le plancher
   non satisfait.
2. La formule construit llama.cpp contre la bibliothèque `ggml` séparée de
   Homebrew (`-DLLAMA_USE_SYSTEM_GGML=ON`) et en liaison dynamique
   (`BUILD_SHARED_LIBS=ON`) : le binaire dépend des dylibs installées par brew
   et non de celles que produirait un build local. Un `brew upgrade ggml`
   peut donc modifier le comportement du runtime sans changer `llama-server`.

Si l'un de ces points est rédhibitoire, passez à la voie sources.

## Voie recommandée : build depuis les sources

### Récupérer les sources

```bash
mkdir -p "$HOME/src"
git clone https://github.com/ggml-org/llama.cpp.git "$HOME/src/llama.cpp"
export LLAMA_SRC="$HOME/src/llama.cpp"

cd "$LLAMA_SRC"
git status --short && git log -1 --oneline
```

Utilisez un **clone complet**, pas `--depth 1`. Un clone superficiel produit un
binaire qui se déclare `version: 1` au lieu du numéro de build réel : EVARuntime
traite ce binaire comme non traçable et le refuse dès qu'un plancher
(`LLAMA_SERVER_MIN_BUILD`) ou une attestation runtime est exigée (cf.
`gateway/bootstrap/runtime_resolver.py`). Le dépôt doit être cloné en dehors de
`/tmp` : le chemin est réutilisé par les mises à jour suivantes.

Avant une mise à jour, le checkout doit être propre.

### Build Metal du serveur

```bash
export LLAMA_SRC="$HOME/src/llama.cpp"
cd "$LLAMA_SRC"

cmake -S . -B build -G Ninja \
  -DGGML_METAL=ON \
  -DGGML_METAL_EMBED_LIBRARY=ON \
  -DBUILD_SHARED_LIBS=OFF \
  -DLLAMA_CURL=OFF \
  -DLLAMA_BUILD_TESTS=OFF \
  -DLLAMA_BUILD_UI=OFF \
  -DLLAMA_USE_PREBUILT_UI=OFF \
  -DCMAKE_BUILD_TYPE=Release

cmake --build build --target llama-server --parallel "$(sysctl -n hw.ncpu)"
./build/bin/llama-server --version
```

Ce que fait chaque option, et pourquoi elle est là :

- `GGML_METAL=ON` est **déjà le défaut sur Apple** (CMake active Metal quand
  `APPLE`) ; il est écrit ici explicitement pour qu'un lecteur n'ait pas à le
  deviner. Ne mettez jamais `-DGGML_METAL=OFF`.
- `GGML_METAL_EMBED_LIBRARY=ON` embarque le shader compilé
  (`default.metallib`) **dans** le binaire au lieu de le poser à côté. Sans
  cela, `llama-server` doit trouver `ggml-metal.metal`/`default.metallib` dans
  son répertoire au moment du chargement du modèle : publier le seul binaire
  dans `releases/<tag>/` casserait Metal. Avec l'embed, le fichier publié est
  autonome. C'est aussi le défaut sur Apple, écrit ici explicitement pour la
  même raison.
- `BUILD_SHARED_LIBS=OFF` produit un binaire qui ne dépend pas des bibliothèques
  générées dans `build/` lorsqu'il est publié ailleurs.
- `LLAMA_CURL=OFF` désactive le téléchargement de modèles intégré à llama.cpp.
  EVARuntime télécharge ses GGUF avec la CLI Hugging Face
  ([deployment.md](deployment.md)); sans cette option, CMake exige des headers
  libcurl que les Command Line Tools ne fournissent pas et la configuration
  échoue (`Could NOT find CURL`).
- `LLAMA_BUILD_TESTS=OFF`, `LLAMA_BUILD_UI=OFF`, `LLAMA_USE_PREBUILT_UI=OFF` :
  ne construire ni tests ni interface web, et ne pas télécharger d'assets UI
  préconstruits depuis Hugging Face. Le target explicite `llama-server` limite
  la compilation au strict nécessaire.

La sortie attendue est `build/bin/llama-server`. La compilation prend 5 à
15 minutes selon la machine. Si elle manque de mémoire, relancez avec moins de
jobs : `cmake --build build --target llama-server --parallel 4`.

### Relever le numéro de build

```bash
./build/bin/llama-server --version
# version: 4559 (ec44a1c0)
# built with Apple clang version ...
```

La première ligne affiche `version: <numéro> (<sha>)`. Le numéro correspond au
tag Git officiel `b<numéro>` (ici `b4559`) : c'est lui que compare
`LLAMA_SERVER_MIN_BUILD`, et celui à utiliser comme nom de release ci-dessous.

> **Note :** un binaire qui se déclare `version: 1` provient d'un clone
> superficiel ou d'une arborescence sans métadonnées Git. Il est refusé par la
> gateway : reprenez le checkout complet décrit plus haut.

### Publier une release versionnée

```bash
export LLAMA_SRC="$HOME/src/llama.cpp"
cd "$LLAMA_SRC"

# Numéro de build lu dans --version (ex. 4559) ; le tag officiel est b4559.
BUILD="$(./build/bin/llama-server --version | sed -n 's/^version: \([0-9][0-9]*\) .*/\1/p')"
test -n "$BUILD" || { echo 'numéro de build illisible — binaire sans métadonnées Git ?' >&2; exit 1; }

install -d -m 0755 "$HOME/opt/llama.cpp/releases/b${BUILD}"
install -m 0755 build/bin/llama-server "$HOME/opt/llama.cpp/releases/b${BUILD}/llama-server"

# Bascule atomique du lien current vers la nouvelle release.
# ln -sfn suffit sous macOS (BSD) : contrairement à GNU mv -T, il remplace le
# lien symbolique sans le déréférencer, même s'il pointe vers un répertoire.
ln -sfn "releases/b${BUILD}" "$HOME/opt/llama.cpp/current"

"$HOME/opt/llama.cpp/current/llama-server" --version
```

L'ancienne release reste en place : revenir en arrière est un seul
`ln -sfn releases/b<ancien> .../current` suivi d'un redémarrage du service.

### Pointer EVARuntime dessus

Dans `~/.config/evaruntime/env` :

```ini
LLAMA_SERVER_BIN=/Users/<user>/opt/llama.cpp/current/llama-server
```

Puis validez et redémarrez le service launchd (voir
[deployment.md §macOS](deployment.md#déploiement-macos-apple-silicon)). Le
plancher peut maintenant être relevé explicitement :

```ini
LLAMA_SERVER_MIN_BUILD=<premier_build_corrige>
```

## Vérifier que Metal est réellement utilisé

Metal peut être absent du binaire (voie Homebrew), refuser de compiler son
shader, ou simplement ne pas recevoir les couches du modèle — sans erreur
fatale dans les deux derniers cas, le modèle tournant alors sur CPU. Après un
chargement de modèle, trois indices se lisent dans les logs de `llama-server`
(`~/Library/Application Support/evaruntime/logs/gateway.log` côté gateway, ou
un lancement manuel pour tester) :

```text
load_tensors: offloading output layer to GPU
load_tensors: offloaded 33/33 layers to GPU      ← TOUTES les couches sur GPU
load_tensors: Metal_Mapped model buffer size =  ...
ggml_metal_init: picking default device: Apple M2 Ultra
ggml_metal_init: using embedded metal library    ← build avec GGML_METAL_EMBED_LIBRARY=ON
ggml_metal_init: GPU name:   Apple M2 Ultra
ggml_metal_init: hasUnifiedMemory              = true
ggml_metal_init: recommendedMaxWorkingSetSize  = 98304.00 MB
```

Selon la version de llama.cpp, le préfixe peut être `ggml_metal_init`,
`ggml_metal_device_init` ou `ggml_metal_library_init` : les noms de fonctions
ont évolué, le contenu reste identique. Ce qui compte :

- **`offloaded N/N layers to GPU`** avec N/N égal au total : le paramètre
  `-ngl` (alias `--n-gpu-layers`) du registre `models.yaml`
  (`llama_params.n_gpu_layers: 999` = tout offloader, plafonné automatiquement)
  est bien appliqué. Si la ligne montre `0/N` ou si `Metal_Mapped` est absent,
  rien ne tourne sur GPU.
- **`recommendedMaxWorkingSetSize`** : le plafond mémoire que macOS accorde au
  GPU (voir [§ OOM mémoire unifiée](deployment.md#dépannage-macos) dans
  deployment.md).

Comparaison CPU/GPU honnête : mesurez le débit de génération (champ
`timings.predicted_per_second` de la réponse `/v1/chat/completions`, ou
`llama-bench`) puis relancez avec `--n-gpu-layers 0`. Sur Apple Silicon,
l'écart typique entre CPU pur et Metal complet se compte en facteur multiple
(pour un modèle 7–8B Q4 : quelques tok/s contre plusieurs dizaines). Si vos
deux mesures sont proches et lentes, Metal n'est pas utilisé.

## Problèmes de compilation

| Message | Cause probable | Action |
|---|---|---|
| `xcrun: error: invalid active developer path` | Command Line Tools absentes ou invalidées par une mise à jour macOS | `xcode-select --install`, ou `sudo rm -rf /Library/Developer/CommandLineTools && xcode-select --install` si déjà installées |
| `CMake Error: Could not find ninja` / `cmake: command not found` | Outils de build absents | `brew install cmake ninja` |
| `Could NOT find CURL. Hint: to disable this feature, set -DLLAMA_CURL=OFF` | Headers libcurl absents des CLT (défaut ON de `LLAMA_CURL` depuis avril 2025) | Ajouter `-DLLAMA_CURL=OFF` à la configuration (choix retenu par ce guide) |
| Échec au chargement du modèle : `unknown type name 'block_q4_0'` dans le shader Metal | Bug d'embed de la metallib connu sur certaines combinaisons macOS/Xcode/CMake (chemins contenant des espaces notamment, corrigé en amont mi-2025 ; régressions possibles selon versions) | Recompiler avec `-DGGML_METAL_EMBED_LIBRARY=OFF` : Metal reste actif, mais publiez alors le binaire **avec** `default.metallib` dans le même répertoire de release |
| Le binaire tourne mais tout est lent, aucun log `ggml_metal_*` | Backend Metal absent (typique d'un bottle Homebrew incomplet) ou `GGML_METAL=OFF` | Reconstruire depuis les sources avec la commande de ce guide |
| `Killed: 9` ou freeze pendant la compilation | Trop de jobs parallèles pour la RAM | `cmake --build build --target llama-server --parallel 4`, puis `--parallel 2` |
| `uname -m` répond `x86_64` | Terminal lancé sous Rosetta | Rouvrir un terminal natif arm64 ; vérifier `arch`. Ne pas publier un binaire x86_64 |
| Build très récent + Mac très récent, crash au premier chargement | Kernels Metal d'une famille GPU trop récente absents de votre version | Mettre à jour le checkout (`git pull`) et rebâtir : les nouvelles familles GPU exigent un llama.cpp récent |

Si CMake a été configuré avec de mauvaises options, repartez d'un cache propre
sans supprimer les sources :

```bash
cd "$HOME/src/llama.cpp"
rm -f build/CMakeCache.txt
rm -rf build/CMakeFiles
# puis relancer la commande cmake de configuration complète ci-dessus
```

## Mise à jour du binaire

```bash
export LLAMA_SRC="$HOME/src/llama.cpp"
cd "$LLAMA_SRC"
git pull --ff-only

cmake --build build --target llama-server --parallel "$(sysctl -n hw.ncpu)"
./build/bin/llama-server --version    # relever le nouveau numéro

BUILD="$(./build/bin/llama-server --version | sed -n 's/^version: \([0-9][0-9]*\) .*/\1/p')"
install -d -m 0755 "$HOME/opt/llama.cpp/releases/b${BUILD}"
install -m 0755 build/bin/llama-server "$HOME/opt/llama.cpp/releases/b${BUILD}/llama-server"
ln -sfn "releases/b${BUILD}" "$HOME/opt/llama.cpp/current"
sudo launchctl kickstart -k system/com.evaruntime.gateway
```

Après toute mise à jour, exécutez la recette du premier token
([deployment.md §smoke test](deployment.md#recette-du-premier-token-smoke_testsh))
et relevez `LLAMA_SERVER_MIN_BUILD` si le nouveau build corrige une vulnérabilité
connue. Les anciennes releases restent dans `releases/` : gardez-en au moins
une connue bonne pour le retour arrière.

## Périmètre de validation

Ce guide a été rédigé et validé sur **Mac Studio M2 Ultra sous macOS 26**. Les
Mac Studio M5 Ultra annoncés le 25 août 2026 n'étaient pas disponibles à la
rédaction : rien de ce qui précède ne prétend avoir été exercé sur eux. Pour
toute nouvelle famille de puces, appliquez la règle du tableau précédent —
checkout récent, rebuild, preuve par les logs.
