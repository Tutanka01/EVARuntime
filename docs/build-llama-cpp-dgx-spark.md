# Compiler llama.cpp pour DGX Spark (GB10, sm_121, ARM64)

> Ce guide s'adresse aux DGX Spark équipés du SoC **NVIDIA Grace-Blackwell GB10**.
> Ne pas confondre avec les GPU Blackwell discrets (RTX 5090, H200 NVL) qui utilisent
> `sm_100` — les binaires ne sont **pas interchangeables**.

---

## Caractéristiques pertinentes du GB10

| Aspect | Détail |
|--------|--------|
| Compute capability | **sm_121** (Grace-Blackwell SoC, différent de sm_100 Blackwell discret) |
| Mémoire | 128 GB LPDDR5X **unifiée CPU+GPU** via NVLink-C2C (600 GB/s) |
| CPU | 72 cœurs ARM Neoverse-V2 (Cortex-X925/A725) |
| OS | DGX OS basé sur Ubuntu 24.04 **aarch64** |
| CUDA requis | **13.0+** (CUDA 12.x ne connaît pas sm_121) |
| Driver requis | **≥ 580.x** |

La mémoire unifiée signifie que CPU et GPU partagent le même pool physique —
aucune copie PCIe, débit natif 600 GB/s. Du point de vue du budget VRAM dans
EVARuntime, configurer `TOTAL_VRAM_GB=120` (sur 128 GB physiques, l'OS et CUDA
réservent ~6-8 GB).

---

## Pré-requis

```bash
# Vérifier CUDA 13+
nvcc --version        # doit afficher "release 13.x"
nvidia-smi            # doit afficher Driver >= 580.x

# Vérifier l'architecture
uname -m              # → aarch64

# Outils de build
sudo apt install -y cmake ninja-build gcc g++ git libcurl4-openssl-dev
# GCC ≥ 12.4 est présent par défaut sur Ubuntu 24.04
gcc --version
```

---

## Build recommandé — sm_121 portable

Compile pour GB10 tout en conservant un PTX virtuel de repli. Binaire portable
si jamais les drivers évoluent.

```bash
git clone https://github.com/ggml-org/llama.cpp.git
cd llama.cpp

cmake -S . -B build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_CUDA=ON \
  -DCMAKE_CUDA_ARCHITECTURES="121" \
  -DGGML_CUDA_F16=ON \
  -DGGML_CUDA_GRAPHS=ON \
  -DGGML_NATIVE=ON \
  -DLLAMA_CURL=ON

cmake --build build -j$(nproc)
sudo install -m 0755 build/bin/llama-server /usr/local/bin/llama-server

# Vérification — doit afficher "compute capability 12.1"
llama-server --version
```

**Explications des flags clés :**

| Flag | Rôle |
|------|------|
| `-DCMAKE_CUDA_ARCHITECTURES="121"` | Cible exclusivement GB10 — génère SASS sm_121 + PTX de repli |
| `-DGGML_CUDA_F16=ON` | Calculs FP16 natifs sur Tensor Cores Blackwell |
| `-DGGML_CUDA_GRAPHS=ON` | CUDA Graphs — réduit la latence CPU sur les tokens répétés |
| `-DGGML_NATIVE=ON` | Active NEON/SVE2 sur les cœurs ARM (tokenizer, sampling) |
| `-DLLAMA_CURL=ON` | Téléchargement de modèles HuggingFace depuis llama-server |

---

## Build avancé — sm_121a-real (performance maximale)

Génère uniquement du SASS sm_121a (instructions GB10 spécifiques : mxfp4,
Tensor Memory Accelerator). Gain attendu : ~3-5 %. **Non portable** hors GB10.

```bash
cmake -S . -B build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_CUDA=ON \
  -DCMAKE_CUDA_ARCHITECTURES="121a-real" \
  -DGGML_CUDA_F16=ON \
  -DGGML_CUDA_GRAPHS=ON \
  -DGGML_NATIVE=ON \
  -DLLAMA_CURL=ON

cmake --build build -j$(nproc)
sudo install -m 0755 build/bin/llama-server /usr/local/bin/llama-server
```

À utiliser uniquement si les deux DGX Spark exécutent exactement le même
binaire et que vous ne redistribuez pas le build.

---

## Erreurs de build connues

| Message | Cause | Solution |
|---------|-------|----------|
| `unsupported gpu architecture 'compute_121'` | CUDA < 13 | Installer CUDA 13.0.1 |
| `ptxas error: mma.m16n8k16 with block scale` | Driver < 580 | Mettre à jour driver ≥ 580.126.09 |
| Build OOM en compilation | Chaque traduction CUDA ≈ 3 GB RAM pic | Réduire `-j` à 4 ou 2 |
| `libcuda.so not found` à l'exécution | LD_LIBRARY_PATH manquant | `export LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH` |

---

## Configuration EVARuntime pour GB10

### Sur chaque nœud (agent) — `/etc/llm-gateway-agent/env`

```ini
# Mémoire unifiée GB10 : 128 GB physiques - 8 GB OS/CUDA = 120 GB net
TOTAL_VRAM_GB=120.0
VRAM_OVERHEAD_GB=4.0      # CUDA 13 + Grace OS + frameworks
VRAM_SAFETY_MARGIN=0.03   # 3% de marge supplémentaire

# Pool de ports — adapter selon le nombre de modèles simultanés voulus
MAX_LOADED_MODELS=5
BASE_LLAMA_PORT=8081
```

### Dans `models.yaml` — profil GB10

```yaml
models:
  - id: llama-3.3-70b-instruct
    path: /models/Llama-3.3-70B-Instruct-Q4_K_M.gguf
    description: "Llama 3.3 70B Instruct — Q4_K_M, optimisé GB10"
    vram_gb: 42.0
    enabled: true
    capabilities: [text_generation, tool_calls, streaming]
    llama_params:
      n_gpu_layers: 999   # tout sur GPU (mémoire unifiée)
      ctx_size: 65536     # 64k tokens — GB10 peut gérer
      parallel: 8         # 8 streams simultanés (128 GB le permettent)
      batch_size: 4096
      ubatch_size: 512
      cache_type_k: q8_0
      cache_type_v: q8_0
      flash_attn: true
      threads: 16
      cpu_moe: false      # inutile sur GB10 (mémoire unifiée — pas de bénéfice)

  - id: deepseek-r1-70b
    path: /models/DeepSeek-R1-0528-Qwen3-8B-Q4_K_M.gguf
    description: "Modèle de raisonnement léger — Q4_K_M"
    vram_gb: 6.0
    enabled: true
    capabilities: [text_generation, streaming]
    llama_params:
      n_gpu_layers: 999
      ctx_size: 32768
      parallel: 4
      batch_size: 2048
      ubatch_size: 512
      cache_type_k: q8_0
      cache_type_v: q8_0
      flash_attn: true
      threads: 8
      cpu_moe: false
```

---

## Note sur `--cpu-moe`

Sur les GPU classiques (L40S, A100…), le flag `--cpu-moe` déporte les experts
FFN des modèles MoE (Mixtral, DeepSeek…) sur le CPU pour économiser de la VRAM.

**Sur GB10, ce flag est inutile** : CPU et GPU partagent la même mémoire physique.
Déporter sur CPU ne libère rien — pire, cela ajoute une indirection logicielle.
Laisser `cpu_moe: false` sur tous les modèles GB10.

---

## Performance attendue sur GB10

| Modèle | Quant | Vitesse génération | Prefill |
|--------|-------|-------------------|---------|
| Llama 3.3 70B | Q4_K_M | ~14 t/s | ~600 t/s |
| Llama 3.3 70B | Q8_0 | ~9 t/s | ~550 t/s |
| Qwen 2.5 72B | Q4_K_M | ~13 t/s | ~580 t/s |

Bande passante mémoire GB10 : 273 GB/s (goulot d'étranglement en génération).
Cold start d'un modèle 70B depuis NVMe local : ~30-45 s.

---

## Multi-GPU / dual-node llama.cpp natif — pourquoi ne pas l'utiliser

llama.cpp propose `--rpc` pour répartir un modèle sur deux GPU via le réseau.
**Ce mode n'est PAS utilisé dans EVARuntime** pour deux raisons :

1. **Latence** : chaque token traverse le réseau Ethernet (~5-10× plus lent qu'un
   modèle qui tient sur un seul GB10 — et 128 GB suffisent pour les 70B en Q8_0).
2. **Complexité** : nécessite un serveur RPC dédié sur le second nœud.

L'approche EVARuntime est préférable : chaque DGX Spark charge ses propres
modèles indépendamment, l'orchestrateur les distribue selon la disponibilité VRAM.
Cela donne la résilience (si un nœud tombe, l'autre continue) sans la latence RPC.
