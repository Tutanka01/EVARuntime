"""
COR-020 — une mutation admin ne doit pas détruire le `models.yaml` de l'exploitant.

Avant le correctif, `ModelRegistry._save()` sérialisait la mémoire par
`yaml.dump` : les 55 lignes d'en-tête opérationnel du fichier livré — budget
VRAM, table RAM hôte, procédure de réactivation de `minimax-m2.7` — et tous les
commentaires d'entrée disparaissaient au premier clic du dashboard.

Le matériau de test est le `models.yaml` RÉELLEMENT livré par le dépôt, pas un
fichier reconstruit pour l'occasion : c'est lui qui porte la mise en page, les
commentaires de fin de ligne et les blocs de commentaires que le correctif doit
traverser sans les abîmer.

Couvre :
  - commentaires préservés sur ajout / mise à jour / suppression ;
  - sauvegarde produite, et BORNÉE ;
  - mode d'origine conservé (y compris 0640), groupe rétabli ;
  - `fsync` du fichier ET du répertoire parent ;
  - refus explicite quand le texte candidat ne rend pas le document attendu,
    avec restauration de l'état mémoire et fichier laissé intact.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml

import model_registry
from model_registry import ModelRegistry, RegistryWriteRefused

# Le registre livré par le dépôt : gateway/models.yaml.
_SHIPPED = Path(__file__).resolve().parent.parent / "models.yaml"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _shipped_registry(tmp_path) -> tuple[ModelRegistry, Path]:
    """Copie le `models.yaml` livré dans `tmp_path` et l'ouvre."""
    cible = tmp_path / "models.yaml"
    cible.write_text(_SHIPPED.read_text(encoding="utf-8"), encoding="utf-8")
    return ModelRegistry(config_path=cible), cible


def _comment_lines(texte: str) -> list[str]:
    """Toutes les lignes de commentaire, normalisées (indentation ignorée)."""
    return [ligne.strip() for ligne in texte.splitlines() if ligne.strip().startswith("#")]


def _backups(dossier: Path) -> list[Path]:
    return sorted(p for p in dossier.iterdir() if ".pre-admin." in p.name and p.suffix == ".bak")


def _simple_registry(tmp_path, models: list[dict]) -> tuple[ModelRegistry, Path]:
    cible = tmp_path / "models.yaml"
    cible.write_text(yaml.safe_dump({"models": models}), encoding="utf-8")
    return ModelRegistry(config_path=cible), cible


def _entry(**overrides) -> dict:
    entree = {"id": "m1", "path": "/models/m1.gguf", "vram_gb": 5.0}
    entree.update(overrides)
    return entree


# ── 1. Le matériau de test est bien celui qu'on croit ────────────────────────

def test_le_fichier_livre_porte_bien_des_commentaires_d_exploitation(tmp_path):
    """
    Contrôle positif des tests de préservation qui suivent.

    Sans lui, un `models.yaml` un jour dépouillé de ses commentaires rendrait
    toutes les assertions de préservation vraies sans rien prouver.
    """
    texte = _SHIPPED.read_text(encoding="utf-8")
    commentaires = _comment_lines(texte)
    assert len(commentaires) > 40, "le fichier livré doit porter son en-tête opérationnel"
    assert any("Budget VRAM" in c for c in commentaires)
    assert any("RAM HÔTE" in c for c in commentaires)
    assert any("Pour l'activer" in c for c in commentaires)
    # Commentaire de fin de ligne sur le champ que l'admin mute le plus souvent.
    assert "enabled: false             # Activer quand le fichier .gguf est disponible" in texte


# ── 2. Ajout ─────────────────────────────────────────────────────────────────

def test_ajout_conserve_tous_les_commentaires_du_fichier_livre(tmp_path):
    registre, chemin = _shipped_registry(tmp_path)
    avant = _comment_lines(chemin.read_text(encoding="utf-8"))

    registre.add({
        "id": "nouveau-modele",
        "path": "/models/nouveau.gguf",
        "vram_gb": 8.0,
        "enabled": False,
    })

    apres_texte = chemin.read_text(encoding="utf-8")
    apres = _comment_lines(apres_texte)
    assert set(avant).issubset(set(apres)), "un ajout ne doit effacer aucun commentaire"

    relu = ModelRegistry(config_path=chemin)
    assert relu.get("nouveau-modele") is not None
    assert relu.get("llama-3.3-70b-instruct") is not None
    assert len(relu.list_all()) == len(registre.list_all())


# ── 3. Mise à jour ───────────────────────────────────────────────────────────

def test_set_enabled_conserve_les_commentaires_et_celui_de_fin_de_ligne(tmp_path):
    registre, chemin = _shipped_registry(tmp_path)
    avant = _comment_lines(chemin.read_text(encoding="utf-8"))

    registre.set_enabled("llama-3.1-8b-instruct", True)

    texte = chemin.read_text(encoding="utf-8")
    assert set(avant) == set(_comment_lines(texte))
    # La valeur a changé, le commentaire de fin de ligne a survécu (son alignement
    # est normalisé à deux espaces, son texte est intact).
    assert "enabled: true  # Activer quand le fichier .gguf est disponible sur /models/" in texte
    assert "enabled: false             # Activer quand" not in texte
    assert ModelRegistry(config_path=chemin).get("llama-3.1-8b-instruct").enabled is True


def test_update_de_llama_params_ne_touche_que_la_ligne_concernee(tmp_path):
    registre, chemin = _shipped_registry(tmp_path)
    avant = chemin.read_text(encoding="utf-8")

    params = registre.get("gemma-4-26b-a4b").llama_params.__dict__ | {"ctx_size": 8192}
    registre.update("gemma-4-26b-a4b", llama_params=params)

    texte = chemin.read_text(encoding="utf-8")
    assert set(_comment_lines(avant)) == set(_comment_lines(texte))
    assert "ctx_size: 8192" in texte
    # Le commentaire de fin de ligne de CETTE ligne survit lui aussi.
    assert "# Réduit pour coexister avec qwen3.5-9b" in texte
    # Les paramètres voisins ne bougent pas.
    assert "parallel: 2" in texte
    assert ModelRegistry(config_path=chemin).get("gemma-4-26b-a4b").llama_params.ctx_size == 8192

    # Les autres entrées gardent leur ctx_size : on n'a pas réécrit le fichier.
    relu = ModelRegistry(config_path=chemin)
    assert relu.get("llama-3.3-70b-instruct").llama_params.ctx_size == 32768


def test_update_insere_le_bloc_llama_params_quand_il_est_absent(tmp_path):
    """Entrée écrite à la main qui s'appuie sur les défauts : il n'y a rien à retoucher."""
    registre, chemin = _simple_registry(tmp_path, [_entry()])
    chemin.write_text(
        "# en-tête à préserver\nmodels:\n  - id: m1\n    path: /models/m1.gguf\n"
        "    vram_gb: 5.0  # à affiner\n",
        encoding="utf-8",
    )
    registre = ModelRegistry(config_path=chemin)

    params = registre.get("m1").llama_params.__dict__ | {"ctx_size": 4096}
    registre.update("m1", llama_params=params)

    texte = chemin.read_text(encoding="utf-8")
    assert "# en-tête à préserver" in texte
    assert "# à affiner" in texte
    assert "llama_params:" in texte
    assert ModelRegistry(config_path=chemin).get("m1").llama_params.ctx_size == 4096


# ── 3 bis. Valeurs longues, `#`, guillemets, valeurs repliées ───────────────
# Régressions : `_render_scalar` repliait à 80 colonnes, l'expression régulière
# prenait tout `#` pour un commentaire, et une seule ligne physique était
# remplacée. Une description de 150 caractères — le schéma en autorise 200 —
# était refusée, et `Model #1 for chat` puis `Plain` laissait un résidu.

def _phrase(longueur: int) -> str:
    return ("lorem ipsum dolor sit amet " * 10)[: longueur - 1] + "z"


def _ligne_du_champ(texte: str, champ: str, model_id: str) -> list[str]:
    """Lignes physiques de l'entrée `model_id` qui portent `champ:` ou sa suite."""
    lignes = texte.splitlines()
    debut, fin, indent = model_registry._entry_bounds(lignes, model_id)
    sortie: list[str] = []
    for ligne in lignes[debut:fin]:
        if ligne.strip().startswith(f"{champ}:") or ligne.lstrip("- ").startswith(f"{champ}:"):
            sortie.append(ligne)
        elif sortie and len(ligne) - len(ligne.lstrip()) > indent and ligne.strip():
            sortie.append(ligne)  # suite d'une valeur repliée
        elif sortie:
            break
    return sortie


@pytest.mark.parametrize("longueur", [81, 150, 200])
def test_patch_d_une_longue_description_tient_sur_une_ligne(tmp_path, longueur):
    registre, chemin = _shipped_registry(tmp_path)
    avant = set(_comment_lines(chemin.read_text(encoding="utf-8")))
    valeur = _phrase(longueur)
    assert len(valeur) == longueur and " " in valeur

    registre.update("llama-3.1-8b-instruct", description=valeur)
    registre.update("llama-3.1-8b-instruct", vram_gb=7.0)  # nouvelle retouche du même bloc

    texte = chemin.read_text(encoding="utf-8")
    assert len(_ligne_du_champ(texte, "description", "llama-3.1-8b-instruct")) == 1
    assert ModelRegistry(config_path=chemin).get("llama-3.1-8b-instruct").description == valeur
    assert set(_comment_lines(texte)) == avant


def test_une_longue_description_ajoutee_reste_modifiable(tmp_path):
    registre, chemin = _shipped_registry(tmp_path)
    valeur = _phrase(150)
    registre.add({
        "id": "nouveau-modele", "path": "/models/nouveau.gguf", "vram_gb": 8.0,
        "enabled": False, "description": valeur,
    })
    assert len(_ligne_du_champ(chemin.read_text(encoding="utf-8"), "description", "nouveau-modele")) == 1

    registre.update("nouveau-modele", description="courte")
    registre.update("nouveau-modele", vram_gb=9.0)
    relu = ModelRegistry(config_path=chemin).get("nouveau-modele")
    assert (relu.description, relu.vram_gb) == ("courte", 9.0)


@pytest.mark.parametrize(
    "valeur",
    [
        "Model #1 for chat",
        "a#b sans espace",
        "Model: fast, cheap",
        "it's \"quoted\" # not a comment",
        "'entre apostrophes'",
        "fin en points...",
        "- tiret en tête",
        "ligne un\nligne deux",
    ],
)
def test_valeurs_speciales_aller_retour_puis_remplacement_sans_residu(tmp_path, valeur):
    registre, chemin = _shipped_registry(tmp_path)
    avant = set(_comment_lines(chemin.read_text(encoding="utf-8")))

    registre.update("gemma-4-26b-a4b", description=valeur)
    assert ModelRegistry(config_path=chemin).get("gemma-4-26b-a4b").description == valeur

    registre.update("gemma-4-26b-a4b", description="Plain")
    texte = chemin.read_text(encoding="utf-8")
    assert _ligne_du_champ(texte, "description", "gemma-4-26b-a4b") == ["    description: Plain"]
    assert ModelRegistry(config_path=chemin).get("gemma-4-26b-a4b").description == "Plain"
    # Commentaire de fin de ligne de `path:` du même bloc : intact.
    assert "# À ajuster selon le chemin réel" in texte
    assert set(_comment_lines(texte)) == avant


def test_une_valeur_deja_repliee_est_remplacee_en_entier(tmp_path):
    """Fichier écrit avant le correctif (ou à la main) : valeur pliée sur trois lignes."""
    chemin = tmp_path / "models.yaml"
    chemin.write_text(
        "models:\n"
        "  - id: m1\n"
        "    path: /models/m1.gguf\n"
        "    description: lorem ipsum dolor sit amet lorem ipsum dolor sit amet lorem\n"
        "      ipsum dolor sit amet lorem ipsum dolor sit amet lorem ipsum dolor sit\n"
        "      amet fin  # commentaire d'exploitation\n"
        "    vram_gb: 5.0  # à affiner\n"
        "  - id: m2\n"
        "    path: /models/m2.gguf\n"
        "    description: 'Modèle #2'  # garder\n"
        "    vram_gb: 4.0\n",
        encoding="utf-8",
    )
    registre = ModelRegistry(config_path=chemin)
    assert registre.get("m1").description.endswith("amet fin")  # contrôle positif

    registre.update("m1", description="Nouvelle description")
    registre.update("m2", description="Plain")

    texte = chemin.read_text(encoding="utf-8")
    assert "    description: Nouvelle description  # commentaire d'exploitation\n" in texte
    assert "    description: Plain  # garder\n" in texte
    assert "ipsum dolor sit amet lorem ipsum" not in texte
    assert "# à affiner" in texte
    relu = ModelRegistry(config_path=chemin)
    assert relu.get("m1").description == "Nouvelle description"
    assert relu.get("m2").description == "Plain"
    assert relu.get("m1").vram_gb == 5.0


@pytest.mark.parametrize("style", [">", "|", "|-", ">+"])
@pytest.mark.parametrize("indent_commentaire", [0, 2, 4])
def test_un_commentaire_apres_un_scalaire_bloc_survit_a_la_retouche(
    tmp_path, style, indent_commentaire
):
    """
    Régression : un commentaire moins indenté que les champs, juste après un
    scalaire bloc, devenait une ligne vide de l'extrait ; PyYAML la comptait
    dans le bloc et la retouche l'effaçait. Le document restant identique, le
    contrôle du candidat ne voyait rien : perte silencieuse.
    """
    commentaire = " " * indent_commentaire + "# OPERATOR: never raise vram above 40"
    chemin = tmp_path / "models.yaml"
    chemin.write_text(
        "models:\n"
        "  - id: m\n"
        "    path: /models/m.gguf\n"
        f"    description: {style}\n"
        "      Long folded\n"
        "      description\n"
        f"{commentaire}\n"
        "    vram_gb: 4.0\n",
        encoding="utf-8",
    )
    registre = ModelRegistry(config_path=chemin)

    registre.update("m", description="New")

    texte = chemin.read_text(encoding="utf-8")
    assert f"\n{commentaire}\n" in texte
    assert "    description: New\n" in texte
    relu = ModelRegistry(config_path=chemin).get("m")
    assert (relu.description, relu.vram_gb) == ("New", 4.0)


def test_une_retouche_qui_perdrait_un_commentaire_est_refusee(tmp_path, monkeypatch):
    """
    Garde-fou général, indépendant du calcul d'étendue : si ce calcul se
    trompe et englobe une ligne de commentaire, l'écriture est REFUSÉE (422),
    le fichier et la mémoire restent intacts.
    """
    from bootstrap import registry_writer as rw

    chemin = tmp_path / "models.yaml"
    chemin.write_text(
        "models:\n  - id: m\n    path: /models/m.gguf\n    description: a\n"
        "  # note d'exploitation\n    vram_gb: 4.0\n",
        encoding="utf-8",
    )
    registre = ModelRegistry(config_path=chemin)
    avant = chemin.read_bytes()
    vraie_etendue = rw._scalar_value_span

    def etendue_trop_large(lines, debut, fin, champ_indent, champ):
        trouve = vraie_etendue(lines, debut, fin, champ_indent, champ)
        assert trouve is not None  # contrôle positif : le champ est bien isolé
        ligne_debut, prefixe, ligne_fin, commentaire = trouve
        return ligne_debut, prefixe, ligne_fin + 1, commentaire  # avale le commentaire

    monkeypatch.setattr(rw, "_scalar_value_span", etendue_trop_large)

    with pytest.raises(RegistryWriteRefused, match="commentaire"):
        registre.update("m", description="b")
    assert chemin.read_bytes() == avant
    assert registre.get("m").description == "a"
    assert _backups(tmp_path) == []


def test_une_valeur_sur_la_ligne_suivant_sa_cle_est_retouchee(tmp_path):
    """Valeur écrite sous sa clé : la clé reçoit la valeur, les commentaires restent."""
    chemin = tmp_path / "models.yaml"
    chemin.write_text(
        "models:\n  - id: m\n    path: /models/m.gguf\n"
        "    vram_gb:   # clé\n      4.0   # valeur\n    enabled: false\n",
        encoding="utf-8",
    )
    registre = ModelRegistry(config_path=chemin)

    registre.update("m", vram_gb=8.0)

    texte = chemin.read_text(encoding="utf-8")
    assert "    vram_gb: 8.0  # clé # valeur\n    enabled: false\n" in texte
    assert ModelRegistry(config_path=chemin).get("m").vram_gb == 8.0


# ── 4. Suppression ───────────────────────────────────────────────────────────

def test_remove_supprime_le_bloc_et_conserve_l_en_tete_et_les_voisins(tmp_path):
    registre, chemin = _shipped_registry(tmp_path)

    registre.remove("minimax-m2.7")

    texte = chemin.read_text(encoding="utf-8")
    # L'en-tête opérationnel du fichier est intact.
    assert "# Budget VRAM L40S 48 GB :" in texte
    assert "#   Budget net disponible   : ~43.6 GB" in texte
    assert "# IMPORTANT — RAM HÔTE (pas seulement la VRAM) :" in texte
    # Les commentaires PROPRES à l'entrée supprimée s'en vont avec elle.
    assert "DÉSACTIVÉ PAR DÉFAUT — contrainte de RAM HÔTE" not in texte
    assert "minimax-m2.7" not in texte
    # Les entrées voisines et leurs commentaires sont intacts.
    assert "# OBLIGATOIRE — sans ce flag les experts FFN saturent la VRAM" in texte
    assert "# Activer quand le fichier .gguf est disponible" in texte

    relu = ModelRegistry(config_path=chemin)
    assert relu.get("minimax-m2.7") is None
    assert len(relu.list_all()) == 4


def test_remove_de_la_derniere_entree_laisse_un_registre_rechargeable(tmp_path):
    """
    `models:` seul se relit comme `None` et casse le chargement : la clé doit
    passer à `models: []`.
    """
    registre, chemin = _simple_registry(tmp_path, [_entry()])
    registre.remove("m1")

    texte = chemin.read_text(encoding="utf-8")
    assert "models: []" in texte
    assert ModelRegistry(config_path=chemin).list_all() == []


# ── 5. Sauvegarde ────────────────────────────────────────────────────────────

def test_une_mutation_produit_une_sauvegarde_du_contenu_precedent(tmp_path):
    registre, chemin = _shipped_registry(tmp_path)
    avant = chemin.read_text(encoding="utf-8")

    registre.set_enabled("llama-3.1-8b-instruct", True)

    sauvegardes = _backups(tmp_path)
    assert len(sauvegardes) == 1
    assert sauvegardes[0].read_text(encoding="utf-8") == avant
    assert sauvegardes[0].name.startswith("models.yaml.pre-admin.")


def test_les_sauvegardes_sont_bornees(tmp_path):
    """
    OPS-002 constate que les `*.pre-migration.*.bak` s'accumulent sans purge. Le
    dashboard peut écrire à chaque clic : ne pas borner serait pire.
    """
    registre, chemin = _simple_registry(tmp_path, [_entry()])
    for tour in range(model_registry.ADMIN_BACKUP_RETENTION + 4):
        registre.update("m1", vram_gb=float(tour + 1))

    assert len(_backups(tmp_path)) == model_registry.ADMIN_BACKUP_RETENTION


def test_la_purge_garde_les_pre_etats_les_plus_recents_dans_la_meme_seconde(
    tmp_path, monkeypatch
):
    """
    Régression : la purge triait par nom, et `…Z-1.bak` se trie avant `…Z.bak`.
    Au-delà de `ADMIN_BACKUP_RETENTION` mutations dans la même seconde, les
    pré-états les plus récents étaient effacés aussitôt créés.
    """
    import datetime as _dt

    class _Fige(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=tz)

    monkeypatch.setattr(model_registry, "datetime", _Fige)
    registre, chemin = _simple_registry(tmp_path, [_entry()])
    mutations = model_registry.ADMIN_BACKUP_RETENTION + 3
    for tour in range(1, mutations + 1):
        registre.update("m1", vram_gb=5.0 + tour)  # pré-état : 5.0 + tour - 1

    def _essai(p: Path) -> int:
        suffixe = p.name.removesuffix(".bak").rpartition("Z")[2]
        return int(suffixe.lstrip("-") or 0)

    # essai N = sauvegarde de la mutation N+1 = pré-état vram_gb 5.0 + N.
    conservees = {
        _essai(p): next(
            m for m in yaml.safe_load(p.read_text())["models"] if m["id"] == "m1"
        )["vram_gb"]
        for p in _backups(tmp_path)
    }
    premiere = mutations - model_registry.ADMIN_BACKUP_RETENTION
    assert conservees == {n: 5.0 + n for n in range(premiere, mutations)}, (
        "les plus récents pré-états doivent survivre, sous des numéros jamais réemployés"
    )


def test_la_purge_departage_une_mtime_identique_par_numero_d_essai(tmp_path):
    """Système de fichiers à mtime grossière : l'ordre retombe sur `-N`, pas sur le nom."""
    cible = tmp_path / "models.yaml"
    cible.write_text("models: []\n", encoding="utf-8")
    noms = [f"models.yaml.pre-admin.20260101T000000Z{s}.bak" for s in ("", "-1", "-2", "-10")]
    for nom in noms:
        (tmp_path / nom).write_text(nom, encoding="utf-8")
        os.utime(tmp_path / nom, ns=(10**18, 10**18))

    supprimees = model_registry.purge_registry_backups(cible, ".pre-admin.", ".bak", 2)

    assert sorted(supprimees) == sorted(noms[:2])
    assert sorted(p.name for p in _backups(tmp_path)) == sorted(noms[2:])


def test_une_mutation_sans_effet_n_ecrit_ni_ne_sauvegarde(tmp_path):
    registre, chemin = _simple_registry(tmp_path, [_entry()])
    avant = chemin.read_text(encoding="utf-8")

    registre.update("m1", vram_gb=5.0)  # valeur identique

    assert chemin.read_text(encoding="utf-8") == avant
    assert _backups(tmp_path) == []

    # Contrôle positif : le même helper VOIT bien une sauvegarde après une
    # mutation réelle. Sans lui, l'assertion d'absence ci-dessus serait inerte.
    registre.update("m1", vram_gb=6.0)
    assert len(_backups(tmp_path)) == 1


# ── 6. Permissions et propriété ──────────────────────────────────────────────

@pytest.mark.parametrize("mode", [0o600, 0o640, 0o644])
def test_le_mode_d_origine_est_conserve(tmp_path, mode):
    registre, chemin = _shipped_registry(tmp_path)
    os.chmod(chemin, mode)

    registre.set_enabled("llama-3.1-8b-instruct", True)

    assert stat.S_IMODE(os.stat(chemin).st_mode) == mode


def test_la_sauvegarde_n_est_pas_plus_permissive_que_l_original(tmp_path):
    registre, chemin = _simple_registry(tmp_path, [_entry()])
    os.chmod(chemin, 0o640)

    registre.update("m1", vram_gb=6.0)

    sauvegarde = _backups(tmp_path)[0]
    assert stat.S_IMODE(os.stat(sauvegarde).st_mode) == 0o640


def test_le_groupe_d_origine_est_retabli(tmp_path):
    """
    `os.replace` publie l'inode du temporaire : il porte le groupe du processus
    (ou du répertoire, selon le système), pas celui du fichier remplacé.
    """
    groupes = [g for g in os.getgroups() if g != os.stat(tmp_path).st_gid]
    if not groupes:
        pytest.skip("aucun groupe secondaire disponible pour éprouver la restauration")

    registre, chemin = _simple_registry(tmp_path, [_entry()])
    os.chown(chemin, -1, groupes[0])
    assert os.stat(chemin).st_gid == groupes[0]

    registre.update("m1", vram_gb=6.0)

    assert os.stat(chemin).st_gid == groupes[0]


# ── 7. Durabilité ────────────────────────────────────────────────────────────

def test_le_fichier_et_le_repertoire_parent_sont_fsynces(tmp_path, monkeypatch):
    """
    Sans `fsync` du répertoire, le renommage lui-même n'est pas durable : un
    arrêt brutal peut laisser le répertoire pointant sur l'ancien inode.
    """
    registre, chemin = _simple_registry(tmp_path, [_entry()])

    vus: list[str] = []
    vrai_fsync = os.fsync

    def _espion(fd):
        try:
            vus.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        except OSError:  # pragma: no cover - descripteur exotique
            pass
        return vrai_fsync(fd)

    monkeypatch.setattr(os, "fsync", _espion)
    registre.update("m1", vram_gb=6.0)

    assert "file" in vus, "le contenu écrit doit être fsyncé"
    assert "dir" in vus, "le répertoire parent doit être fsyncé après os.replace"


# ── 8. Refus ─────────────────────────────────────────────────────────────────

def test_refus_quand_le_texte_candidat_ne_rend_pas_le_document_attendu(tmp_path):
    """
    Le reparse comparatif est le garde-fou de la retouche textuelle : si le texte
    produit ne signifie pas exactement ce qui était prévu, on refuse. Jamais de
    repli sur une réécriture globale — c'est elle, le défaut COR-020.
    """
    # Clé dupliquée dans une entrée : YAML retient la DERNIÈRE. La retouche
    # change la première ligne, le fichier relu ne bouge donc pas de valeur.
    chemin = tmp_path / "models.yaml"
    chemin.write_text(
        "# en-tête à préserver\n"
        "models:\n"
        "  - id: m1\n"
        "    path: /models/m1.gguf\n"
        "    vram_gb: 5.0\n"
        "    vram_gb: 7.0\n",
        encoding="utf-8",
    )
    registre = ModelRegistry(config_path=chemin)
    avant = chemin.read_text(encoding="utf-8")
    assert registre.get("m1").vram_gb == 7.0  # contrôle : c'est bien la dernière qui vaut

    with pytest.raises(RegistryWriteRefused, match="ne signifie pas"):
        registre.update("m1", vram_gb=9.0)

    assert chemin.read_text(encoding="utf-8") == avant, "aucun octet ne doit avoir bougé"
    assert _backups(tmp_path) == [], "un refus ne produit pas de sauvegarde"
    # L'état mémoire est revenu en arrière : il ne reste pas en avance sur le disque.
    assert registre.get("m1").vram_gb == 7.0


def test_refus_quand_l_entree_n_est_pas_identifiable_dans_le_texte(tmp_path):
    """
    Registre en style « flow » : chargeable, mais sans ligne de champ à retoucher.

    La gateway ne devine pas — elle refuse et le dit, plutôt que de se rabattre
    sur une réécriture qui remettrait le fichier à plat.
    """
    chemin = tmp_path / "models.yaml"
    chemin.write_text(
        "# en-tête à préserver\n"
        "models: [{id: m1, path: /models/m1.gguf, vram_gb: 5.0}]\n",
        encoding="utf-8",
    )
    registre = ModelRegistry(config_path=chemin)
    avant = chemin.read_text(encoding="utf-8")

    with pytest.raises(RegistryWriteRefused, match="identifi"):
        registre.update("m1", vram_gb=9.0)

    assert chemin.read_text(encoding="utf-8") == avant
    assert registre.get("m1").vram_gb == 5.0


def test_refus_quand_le_fichier_n_est_plus_un_registre(tmp_path):
    registre, chemin = _simple_registry(tmp_path, [_entry()])
    chemin.write_text("autre_cle:\n  - 1\n", encoding="utf-8")

    with pytest.raises(RegistryWriteRefused):
        registre.update("m1", vram_gb=9.0)

    assert chemin.read_text(encoding="utf-8") == "autre_cle:\n  - 1\n"
    assert registre.get("m1").vram_gb == 5.0


def test_refus_quand_un_champ_non_scalaire_diverge_du_disque(tmp_path):
    """
    `capabilities`, `speculative`, `path` : aucune mutation admin ne les change.
    Une divergence vient d'une édition manuelle — l'écraser serait une perte de
    données, donc on refuse plutôt que de réécrire le bloc.
    """
    registre, chemin = _simple_registry(tmp_path, [_entry(capabilities=["text_generation"])])
    # L'exploitant a ajouté une capability à la main pendant que la gateway tournait.
    chemin.write_text(
        yaml.safe_dump({"models": [_entry(capabilities=["text_generation", "streaming"])]}),
        encoding="utf-8",
    )
    avant = chemin.read_text(encoding="utf-8")

    with pytest.raises(RegistryWriteRefused, match="capabilities"):
        registre.update("m1", vram_gb=9.0)

    assert chemin.read_text(encoding="utf-8") == avant


def test_refus_quand_un_scalaire_diverge_du_disque(tmp_path):
    """
    Une édition manuelle de description ne doit pas être annulée par une mise à
    jour admin de vram_gb construite depuis l'ancien snapshot mémoire.
    """
    registre, chemin = _simple_registry(
        tmp_path, [_entry(description="description chargée", enabled=False)]
    )
    chemin.write_text(
        yaml.safe_dump({
            "models": [_entry(description="édition opérateur", enabled=False)]
        }),
        encoding="utf-8",
    )
    avant = chemin.read_text(encoding="utf-8")

    with pytest.raises(RegistryWriteRefused, match="édition concurrente"):
        registre.update("m1", vram_gb=9.0)

    assert chemin.read_text(encoding="utf-8") == avant
    assert registre.get("m1").description == "description chargée"
    assert registre.get("m1").vram_gb == 5.0

    # Après rechargement explicite, l'intention opérateur fait partie du nouveau
    # snapshot et une mutation indépendante la préserve.
    registre.reload()
    registre.update("m1", vram_gb=9.0)
    relu = ModelRegistry(config_path=chemin).get("m1")
    assert relu.description == "édition opérateur"
    assert relu.vram_gb == 9.0


def test_une_edition_concurrente_de_commentaire_reste_autorisee(tmp_path):
    """Le verrou optimiste porte sur le sens YAML, pas sur la mise en page."""
    registre, chemin = _simple_registry(tmp_path, [_entry()])
    chemin.write_text(
        "# commentaire ajouté pendant l'exécution\n" + chemin.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    registre.update("m1", vram_gb=9.0)

    texte = chemin.read_text(encoding="utf-8")
    assert "# commentaire ajouté pendant l'exécution" in texte
    assert ModelRegistry(config_path=chemin).get("m1").vram_gb == 9.0
