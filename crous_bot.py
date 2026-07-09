#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crous_bot.py — Surveillance des logements CROUS (trouverunlogement.lescrous.fr)
================================================================================
Vérifie régulièrement la disponibilité des logements CROUS dans une zone
géographique (par défaut : Limoges) et envoie une notification Telegram
dès qu'un NOUVEAU logement apparaît, avec le lien direct pour réserver.

Stratégie de détection (double moteur, bascule automatique) :
  1. API JSON interne du site : POST /api/fr/search/<idTool>
     (celle qu'utilise le site lui-même — légère et fiable)
  2. Repli automatique : lecture de la page HTML de recherche
     (GET /tools/<id>/search?bounds=...) si l'API change de format.

Le bot est volontairement RESPECTUEUX du serveur :
  - une seule vérification toutes les ~5 minutes (jamais moins de 3 min),
  - détection de la page officielle de surcharge ("Vous êtes trop nombreux")
    traitée comme une pause, jamais contournée,
  - aucun accès aux parties authentifiées du site.

Usage :
  python crous_bot.py --test-telegram   # envoie un message de test et s'arrête
  python crous_bot.py --once            # une seule vérification (GitHub Actions)
  python crous_bot.py                   # boucle infinie (serveur / VM / Raspberry)
  python crous_bot.py --selftest        # test hors-ligne de la logique (aucune requête)
"""

import html as html_mod
import json
import logging
import logging.handlers
import os
import random
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# --------------------------------------------------------------------------
# Constantes générales
# --------------------------------------------------------------------------
BASE = "https://trouverunlogement.lescrous.fr"
TELEGRAM_API = "https://api.telegram.org"

# Marqueur exact de la page officielle de surcharge du CROUS.
MARQUEUR_SURCHARGE = "vous êtes trop nombreux"

# Marqueurs prouvant qu'on a bien reçu une vraie page de recherche CROUS
# (avec ou sans résultat) et non une page d'erreur/blocage inconnue.
MARQUEURS_PAGE_AUTHENTIQUE = (
    "aucun logement trouvé",   # cas légitime : 0 résultat
    "fr-card",                 # cas avec résultats : cartes de logement
    "logement pour l'année",   # titre générique de la page de recherche
)

LOG = logging.getLogger("crous_bot")


class SurchargeSite(Exception):
    """Le site affiche sa page de surcharge officielle — on attend le prochain cycle."""


class PageInattendue(Exception):
    """La réponse ne ressemble ni à des résultats ni à la page de surcharge."""


# --------------------------------------------------------------------------
# Chargement de la configuration (.env + variables d'environnement)
# --------------------------------------------------------------------------
def charger_fichier_env(chemin: str = ".env") -> None:
    """Charge un fichier .env très simple (CLE=valeur) dans os.environ.

    Les variables déjà présentes dans l'environnement gardent la priorité
    (utile sur GitHub Actions où les secrets sont injectés directement).
    """
    p = Path(chemin)
    if not p.exists():
        return
    for ligne in p.read_text(encoding="utf-8").splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#") or "=" not in ligne:
            continue
        cle, valeur = ligne.split("=", 1)
        valeur = valeur.strip().strip('"').strip("'")
        os.environ.setdefault(cle.strip(), valeur)


def _env(nom: str, defaut: str = "") -> str:
    return os.environ.get(nom, defaut).strip()


class Config:
    """Toute la configuration du bot, lue depuis l'environnement."""

    def __init__(self) -> None:
        # --- Telegram (obligatoire, sauf en DRY_RUN/selftest) ---
        self.token = _env("TELEGRAM_BOT_TOKEN")
        self.chat_id = _env("TELEGRAM_CHAT_ID")

        # --- Zone de recherche ---
        self.ville = _env("VILLE", "Limoges")
        # Rectangle ouest_nord_est_sud ; accepte aussi des virgules.
        brut = _env("BOUNDS", "1.15_45.93_1.35_45.77").replace(",", "_")
        morceaux = [m for m in brut.split("_") if m]
        if len(morceaux) != 4:
            raise SystemExit("BOUNDS invalide : attendu 4 nombres 'ouest_nord_est_sud'")
        self.ouest, self.nord, self.est, self.sud = (float(m) for m in morceaux)
        self.bounds_str = f"{self.ouest}_{self.nord}_{self.est}_{self.sud}"

        # --- Campagne ciblée ---
        self.annee_cible = _env("TARGET_YEAR", "2026-2027")
        self.tool_id_force = int(_env("TOOL_ID")) if _env("TOOL_ID") else None

        # --- Résidences prioritaires (alerte 🚨) ---
        self.prioritaires = [
            normaliser(x) for x in _env("PRIORITY_RESIDENCES", "").split(",") if x.strip()
        ]
        self.repetitions_prioritaire = max(1, int(_env("PRIORITY_REPEAT", "3")))

        # --- Rythme ---
        self.intervalle_min = max(4, int(_env("CHECK_INTERVAL_MINUTES", "5")))
        self.jitter_s = int(_env("JITTER_SECONDS", "60"))
        self.heure_heartbeat = int(_env("HEARTBEAT_HOUR", "9"))
        self.tz = ZoneInfo(_env("TIMEZONE", "Africa/Algiers"))
        self.tz_nom = _env("TIMEZONE", "Africa/Algiers")

        # --- Divers ---
        self.fichier_etat = Path(_env("STATE_FILE", "data/etat.json"))
        self.fichier_log = _env("LOG_FILE", "crous_bot.log")  # vide = console seule
        self.max_alertes_detaillees = int(_env("MAX_DETAILED_ALERTS", "8"))
        self.dry_run = _env("DRY_RUN") in ("1", "true", "True", "yes")

    def verifier_telegram(self) -> None:
        if self.dry_run:
            return
        if not self.token or not self.chat_id:
            raise SystemExit(
                "Configuration incomplète : TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID "
                "sont obligatoires (voir README, section « Créer le bot Telegram »)."
            )


CFG: Config  # renseigné dans main()


def libelle_fuseau() -> str:
    return {
        "Africa/Algiers": "heure d'Algérie",
        "Europe/Paris": "heure de Paris",
    }.get(CFG.tz_nom, CFG.tz_nom)


def maintenant() -> datetime:
    return datetime.now(CFG.tz)


def normaliser(texte: str) -> str:
    """Minuscule + suppression des accents, pour comparer des noms de résidences."""
    sans_accents = unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode()
    return " ".join(sans_accents.lower().split())


# --------------------------------------------------------------------------
# Journalisation (console + fichier), horodatée dans le fuseau configuré
# --------------------------------------------------------------------------
class FormateurFuseau(logging.Formatter):
    def formatTime(self, record, datefmt=None):  # noqa: N802 (API logging)
        dt = datetime.fromtimestamp(record.created, CFG.tz)
        return dt.strftime(datefmt or "%d/%m/%Y %H:%M:%S")


def configurer_logs() -> None:
    fmt = FormateurFuseau("%(asctime)s [%(levelname)s] %(message)s")
    LOG.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    LOG.addHandler(console)
    if CFG.fichier_log:
        fichier = logging.handlers.RotatingFileHandler(
            CFG.fichier_log, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
        fichier.setFormatter(fmt)
        LOG.addHandler(fichier)


# --------------------------------------------------------------------------
# Couche HTTP : session, User-Agent réaliste, retries avec backoff exponentiel
# --------------------------------------------------------------------------
SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "fr-FR,fr;q=0.9",
    }
)


def requete(methode: str, url: str, *, essais: int = 3, **kwargs) -> requests.Response:
    """Requête HTTP avec retries (backoff exponentiel) qui ne fait jamais crasher le bot.

    - La page de surcharge officielle lève SurchargeSite immédiatement
      (inutile de réessayer tout de suite : on attend le prochain cycle).
    - Les erreurs réseau / 5xx / 429 sont réessayées avec des pauses croissantes.
    """
    delai = 5.0
    derniere_erreur: Exception = RuntimeError("aucune tentative effectuée")
    for tentative in range(1, essais + 1):
        try:
            r = SESSION.request(methode, url, timeout=(10, 30), **kwargs)
            corps = r.text if r.content else ""
            if MARQUEUR_SURCHARGE in corps.lower():
                raise SurchargeSite("page officielle « Vous êtes trop nombreux »")
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            return r
        except SurchargeSite:
            raise
        except requests.RequestException as exc:
            derniere_erreur = exc
            LOG.warning(
                "Requête %s %s en échec (tentative %d/%d) : %s",
                methode, url, tentative, essais, exc,
            )
            if tentative < essais:
                time.sleep(delai + random.uniform(0, 2))
                delai *= 3  # 5 s → 15 s → 45 s
    raise derniere_erreur


# --------------------------------------------------------------------------
# Détection automatique de l'identifiant d'« outil » (change à chaque campagne)
# --------------------------------------------------------------------------
def detecter_tool_id() -> int:
    """Trouve sur la page d'accueil l'outil correspondant à l'année ciblée.

    La page d'accueil contient des liens du type /tools/<id>/search pour
    chaque campagne (année en cours + année suivante). On choisit celui dont
    le contexte mentionne TARGET_YEAR ; à défaut, le plus grand id (campagne
    la plus récente).
    """
    r = requete("GET", BASE + "/")
    page = r.text
    correspondances = list(re.finditer(r"/tools/(\d+)/search", page))
    if not correspondances:
        raise PageInattendue("aucun lien /tools/<id>/search sur la page d'accueil")

    annee = re.sub(r"[\s\u2010-\u2015]", "-", CFG.annee_cible)
    candidats = []
    for m in correspondances:
        contexte = page[max(0, m.start() - 400): m.end() + 400]
        contexte = re.sub(r"[\u2010-\u2015]", "-", contexte)  # tirets typographiques
        if annee in contexte:
            candidats.append(int(m.group(1)))
    if candidats:
        return max(candidats)
    tous = sorted({int(m.group(1)) for m in correspondances})
    LOG.warning(
        "Année %s introuvable sur l'accueil ; repli sur l'outil le plus récent (%s parmi %s)",
        CFG.annee_cible, tous[-1], tous,
    )
    return tous[-1]


def obtenir_tool_id(etat: dict) -> int:
    """Renvoie l'outil à utiliser, avec cache 24 h pour limiter les requêtes."""
    if CFG.tool_id_force:
        return CFG.tool_id_force
    aujourd_hui = maintenant().date().isoformat()
    if etat.get("tool_id") and etat.get("tool_id_verifie_le") == aujourd_hui:
        return etat["tool_id"]
    try:
        tool_id = detecter_tool_id()
    except SurchargeSite:
        raise
    except Exception as exc:  # accueil KO mais on a un cache : on continue avec
        if etat.get("tool_id"):
            LOG.warning("Détection de l'outil impossible (%s) ; on garde l'outil %s en cache",
                        exc, etat["tool_id"])
            return etat["tool_id"]
        raise
    ancien = etat.get("tool_id")
    if ancien and ancien != tool_id:
        LOG.info("Changement de campagne détecté : outil %s → %s", ancien, tool_id)
        envoyer_telegram(
            "ℹ️ <b>Nouvelle campagne CROUS détectée</b>\n"
            f"Je bascule la surveillance de l'outil {ancien} vers l'outil {tool_id}."
        )
        etat["disponibles"] = {}  # les identifiants changent d'une campagne à l'autre
    etat["tool_id"] = tool_id
    etat["tool_id_verifie_le"] = aujourd_hui
    return tool_id


# --------------------------------------------------------------------------
# Moteur 1 : API JSON interne (celle qu'appelle le site lui-même)
# --------------------------------------------------------------------------
def recherche_via_api(tool_id: int) -> dict:
    """Interroge POST /api/fr/search/<idTool> et renvoie {id: infos_logement}."""
    url = f"{BASE}/api/fr/search/{tool_id}"
    entetes = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": BASE,
        "Referer": f"{BASE}/tools/{tool_id}/search?bounds={CFG.bounds_str}",
    }
    logements: dict = {}
    page, total = 1, None
    while page <= 4:  # garde-fou : jusqu'à 200 logements, largement assez pour une ville
        charge = {
            "idTool": tool_id,
            "need_aggregation": False,
            "page": page,
            "pageSize": 50,
            "sector": None,
            "occupationModes": [],
            "location": [
                {"lon": CFG.ouest, "lat": CFG.nord},  # coin nord-ouest
                {"lon": CFG.est, "lat": CFG.sud},     # coin sud-est
            ],
            "residence": None,
            "precision": 3,
            "equipment": [],
            "price": {"max": 10_000_000},  # centimes → aucun filtre de prix
            "accommodationTypes": [],
        }
        r = requete("POST", url, json=charge, headers=entetes)
        try:
            donnees = r.json()
        except ValueError as exc:
            raise PageInattendue(f"réponse non-JSON de l'API (HTTP {r.status_code})") from exc
        resultats = donnees.get("results") or {}
        items = resultats.get("items")
        if items is None:
            raise PageInattendue("champ results.items absent de la réponse API")
        for it in items:
            info = _logement_depuis_api(it, tool_id)
            if info:
                logements[info["id"]] = info
        if total is None:
            total = int((resultats.get("total") or {}).get("value") or len(items))
        if len(logements) >= total or not items:
            break
        page += 1
        time.sleep(2)  # petite pause entre pages, par politesse
    return logements


def _logement_depuis_api(it: dict, tool_id: int) -> dict | None:
    """Extraction défensive d'un logement depuis un item JSON de l'API."""
    ident = it.get("id")
    if ident is None:
        return None
    ident = str(ident)
    residence = it.get("residence") or {}

    # Prix : rentMin est historiquement exprimé en centimes.
    prix_txt = ""
    modes = it.get("occupationModes") or it.get("occupationMods") or []
    valeurs = []
    for mode in modes:
        if isinstance(mode, dict):
            for cle in ("rentMin", "rent", "rentMax"):
                v = mode.get(cle)
                if isinstance(v, (int, float)) and v > 0:
                    valeurs.append(float(v))
    if valeurs:
        prix = min(valeurs)
        if prix >= 1000:  # heuristique centimes → euros (un loyer CROUS < 1000 €)
            prix /= 100
        prix_txt = f"{prix:.2f} €/mois".replace(".", ",")

    # Surface, si présente sous une forme ou une autre.
    surface = it.get("area") or it.get("areaMin") or it.get("areaMax")
    details = f"{surface} m²" if surface else ""

    return {
        "id": ident,
        "titre": it.get("label") or "Logement CROUS",
        "residence": residence.get("label") or "",
        "adresse": residence.get("address") or "",
        "prix": prix_txt,
        "details": details,
        "lien": f"{BASE}/tools/{tool_id}/accommodations/{ident}",
    }


# --------------------------------------------------------------------------
# Moteur 2 (repli) : lecture de la page HTML de recherche
# --------------------------------------------------------------------------
def recherche_via_html(tool_id: int) -> dict:
    """Analyse GET /tools/<id>/search (cartes « fr-card » du design système de l'État)."""
    from bs4 import BeautifulSoup  # import paresseux : seulement si le repli sert

    logements: dict = {}
    page, pages_total = 1, 1
    while page <= min(pages_total, 5):
        r = requete(
            "GET",
            f"{BASE}/tools/{tool_id}/search",
            params={"bounds": CFG.bounds_str, "page": page},
        )
        r.encoding = "utf-8"  # le serveur oublie parfois le charset → accents cassés sinon
        texte_bas = r.text.lower()
        if not any(m in texte_bas for m in MARQUEURS_PAGE_AUTHENTIQUE):
            raise PageInattendue("page HTML sans marqueur CROUS reconnu (site remanié ou bloqué ?)")

        soup = BeautifulSoup(r.text, "html.parser")

        # Nombre total de pages, indiqué dans le <title> : « ... page 1 sur 3 »
        titre = soup.find("title")
        m = re.search(r"page \d+ sur (\d+)", titre.get_text()) if titre else None
        if m:
            pages_total = int(m.group(1))

        cartes = soup.select("li.fr-col-lg-4") or soup.select("div.fr-card")
        for carte in cartes:
            lien_el = carte.select_one('a[href*="/accommodations/"]') or carte.find("a", href=True)
            if not lien_el:
                continue
            href = lien_el["href"]
            lien = href if href.startswith("http") else BASE + href
            ident = lien.rstrip("/").split("/")[-1].split("?")[0]

            titre_el = carte.select_one("h3.fr-card__title a") or carte.find(["h3", "h2"])
            titre_txt = titre_el.get_text(strip=True) if titre_el else "Logement CROUS"

            prix_el = carte.select_one(".fr-badges-group .fr-badge") or carte.select_one("p.fr-badge")
            adresse_el = carte.select_one("p.fr-card__desc")
            details = " · ".join(
                p.get_text(strip=True) for p in carte.select("p.fr-card__detail")
            )

            logements[ident] = {
                "id": ident,
                "titre": titre_txt,
                "residence": titre_txt,  # sur les cartes, le titre contient la résidence
                "adresse": adresse_el.get_text(strip=True) if adresse_el else "",
                "prix": prix_el.get_text(strip=True) if prix_el else "",
                "details": details,
                "lien": lien,
            }
        page += 1
        if page <= pages_total:
            time.sleep(2)
    return logements


# --------------------------------------------------------------------------
# Récupération unifiée : API d'abord, HTML en secours (et mémorisation du mode)
# --------------------------------------------------------------------------
def recuperer_logements(tool_id: int, etat: dict) -> dict:
    prefere = etat.get("mode", "api")
    ordre = ["api", "html"] if prefere == "api" else ["html", "api"]
    derniere: Exception | None = None
    for mode in ordre:
        try:
            resultat = recherche_via_api(tool_id) if mode == "api" else recherche_via_html(tool_id)
            if etat.get("mode") != mode:
                LOG.info("Moteur de détection utilisé : %s", mode.upper())
            etat["mode"] = mode
            return resultat
        except SurchargeSite:
            raise  # tout le site est saturé : inutile d'essayer l'autre moteur
        except Exception as exc:
            LOG.warning("Moteur %s indisponible : %s", mode.upper(), exc)
            derniere = exc
    raise PageInattendue(f"les deux moteurs ont échoué (dernier : {derniere})")


# --------------------------------------------------------------------------
# État persistant (fichier JSON) — écriture atomique
# --------------------------------------------------------------------------
def charger_etat() -> dict:
    if CFG.fichier_etat.exists():
        try:
            return json.loads(CFG.fichier_etat.read_text(encoding="utf-8"))
        except Exception as exc:
            LOG.warning("Fichier d'état illisible (%s) : on repart de zéro", exc)
    return {"disponibles": {}, "echecs_consecutifs": 0}


def sauvegarder_etat(etat: dict) -> None:
    CFG.fichier_etat.parent.mkdir(parents=True, exist_ok=True)
    tmp = CFG.fichier_etat.with_suffix(".tmp")
    tmp.write_text(json.dumps(etat, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CFG.fichier_etat)  # remplacement atomique : jamais d'état corrompu


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
def envoyer_telegram(texte: str, silencieux: bool = False) -> bool:
    """Envoie un message via l'API Bot officielle (simple requête HTTP)."""
    if CFG.dry_run:
        LOG.info("[DRY-RUN Telegram] %s", texte.replace("\n", " | "))
        return True
    try:
        r = requete(
            "POST",
            f"{TELEGRAM_API}/bot{CFG.token}/sendMessage",
            json={
                "chat_id": CFG.chat_id,
                "text": texte,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": silencieux,
            },
        )
        ok = bool(r.json().get("ok"))
        if not ok:
            LOG.error("Telegram a refusé le message : %s", r.text[:300])
        return ok
    except Exception as exc:
        LOG.error("Envoi Telegram impossible : %s", exc)
        return False


def esc(texte: str) -> str:
    return html_mod.escape(texte or "")


def est_prioritaire(logement: dict) -> bool:
    if not CFG.prioritaires:
        return False
    cible = normaliser(f"{logement.get('residence', '')} {logement.get('titre', '')}")
    return any(p in cible for p in CFG.prioritaires)


def notifier_logement(logement: dict, prioritaire: bool) -> None:
    horodatage = maintenant().strftime("%d/%m/%Y à %H:%M")
    lignes = []
    if prioritaire:
        lignes.append("🚨🚨🚨 <b>RÉSIDENCE PRIORITAIRE</b> 🚨🚨🚨")
    lignes += [
        f"🏠 <b>Nouveau logement CROUS — {esc(CFG.ville)}</b>",
        "",
        f"<b>{esc(logement['titre'])}</b>",
    ]
    if logement.get("residence") and logement["residence"] != logement["titre"]:
        lignes.append(f"🏢 Résidence : {esc(logement['residence'])}")
    if logement.get("prix"):
        lignes.append(f"💶 {esc(logement['prix'])}")
    if logement.get("details"):
        lignes.append(f"📐 {esc(logement['details'])}")
    if logement.get("adresse"):
        lignes.append(f"📍 {esc(logement['adresse'])}")
    lignes += [
        "",
        f"🕒 Détecté le {horodatage} ({libelle_fuseau()})",
        f"👉 <a href=\"{esc(logement['lien'])}\">OUVRIR L'ANNONCE ET RÉSERVER</a>",
    ]
    envoyer_telegram("\n".join(lignes))
    if prioritaire:
        # Plusieurs rappels sonores pour être sûr d'être vu.
        for _ in range(CFG.repetitions_prioritaire - 1):
            time.sleep(1.2)
            envoyer_telegram("🚨🚨 <b>LOGEMENT PRIORITAIRE DISPONIBLE</b> — ouvre le message ci-dessus, vite !")


def notifier_resume(nb_restants: int, tool_id: int) -> None:
    lien = f"{BASE}/tools/{tool_id}/search?bounds={CFG.bounds_str}"
    envoyer_telegram(
        f"➕ <b>{nb_restants} autre(s) nouveau(x) logement(s)</b> à {esc(CFG.ville)} — "
        f"trop nombreux pour tout détailler.\n"
        f"👉 <a href=\"{esc(lien)}\">Voir toute la liste sur le site</a>"
    )


# --------------------------------------------------------------------------
# Heartbeat quotidien + alerte de panne prolongée
# --------------------------------------------------------------------------
def gerer_heartbeat(etat: dict, total_visible: int | None) -> None:
    """Une fois par jour, après l'heure configurée : « je suis toujours vivant »."""
    ici = maintenant()
    aujourd_hui = ici.date().isoformat()
    if etat.get("dernier_heartbeat") == aujourd_hui or ici.hour < CFG.heure_heartbeat:
        return
    if total_visible is not None:
        situation = f"📊 {total_visible} logement(s) actuellement visible(s) à {esc(CFG.ville)}."
    else:
        situation = "⚠️ Dernière vérification en échec (site probablement saturé) — je continue d'essayer."
    derniere = etat.get("derniere_reussite")
    ligne_ok = f"\n🔎 Dernière lecture réussie : {derniere}" if derniere else ""
    envoyer_telegram(
        f"✅ <b>Bot CROUS actif</b> — {ici.strftime('%d/%m/%Y %H:%M')} ({libelle_fuseau()})\n"
        f"{situation}{ligne_ok}",
        silencieux=True,  # notification discrète : pas de sonnerie pour le heartbeat
    )
    etat["dernier_heartbeat"] = aujourd_hui


def gerer_alerte_panne(etat: dict) -> None:
    """Prévient (au plus une fois toutes les 6 h) si le site est injoignable > 1 h."""
    echecs = etat.get("echecs_consecutifs", 0)
    seuil = max(1, round(60 / CFG.intervalle_min))  # ~1 h d'échecs consécutifs
    if echecs < seuil:
        return
    derniere_alerte = etat.get("derniere_alerte_panne")
    ici = maintenant()
    if derniere_alerte:
        try:
            ecart_h = (ici - datetime.fromisoformat(derniere_alerte)).total_seconds() / 3600
            if ecart_h < 6:
                return
        except ValueError:
            pass
    envoyer_telegram(
        "⚠️ <b>Info</b> : impossible de consulter le site CROUS depuis environ "
        f"{echecs * CFG.intervalle_min} minutes (surcharge « Vous êtes trop nombreux » "
        "ou maintenance). Le bot continue d'essayer toutes les "
        f"~{CFG.intervalle_min} minutes — rien à faire de ton côté.",
        silencieux=True,
    )
    etat["derniere_alerte_panne"] = ici.isoformat()


# --------------------------------------------------------------------------
# Un cycle complet de vérification
# --------------------------------------------------------------------------
def executer_verification(etat: dict, fetch=None) -> tuple[int | None, int]:
    """Effectue UNE vérification. Renvoie (total_visible, nb_nouveaux).

    total_visible vaut None si la lecture a échoué (surcharge, réseau...).
    Ne lève jamais d'exception : tout est journalisé et l'état est sauvegardé.
    """
    premier_lancement = not CFG.fichier_etat.exists()
    total_visible: int | None = None
    nb_nouveaux = 0
    try:
        tool_id = obtenir_tool_id(etat)
        logements = (fetch or recuperer_logements)(tool_id, etat)
        total_visible = len(logements)

        connus = set(etat.get("disponibles", {}))
        actuels = set(logements)
        nouveaux = sorted(actuels - connus)
        disparus = connus - actuels
        for ident in disparus:  # un logement parti puis revenu = nouvelle dispo → re-notifié
            etat["disponibles"].pop(ident, None)

        if premier_lancement:
            envoyer_telegram(
                f"🚀 <b>Bot CROUS démarré</b> — je surveille {esc(CFG.ville)} "
                f"toutes les ~{CFG.intervalle_min} min.\n"
                f"📊 {total_visible} logement(s) visible(s) en ce moment."
            )
            # Au premier lancement on n'alerte pas logement par logement :
            # tout l'existant deviendrait « nouveau ». On enregistre simplement.
            nouveaux_a_notifier = []
        else:
            nouveaux_a_notifier = nouveaux

        prioritaires = [i for i in nouveaux_a_notifier if est_prioritaire(logements[i])]
        autres = [i for i in nouveaux_a_notifier if i not in prioritaires]
        for ident in prioritaires:  # les prioritaires passent toujours en détail
            notifier_logement(logements[ident], prioritaire=True)
        quota = max(0, CFG.max_alertes_detaillees - len(prioritaires))
        for ident in autres[:quota]:
            notifier_logement(logements[ident], prioritaire=False)
        if len(autres) > quota:
            notifier_resume(len(autres) - quota, tool_id)

        horodatage = maintenant().isoformat(timespec="seconds")
        for ident in nouveaux:
            etat["disponibles"][ident] = horodatage
        etat["derniere_reussite"] = horodatage
        etat["echecs_consecutifs"] = 0
        nb_nouveaux = len(nouveaux_a_notifier)
        LOG.info(
            "Vérification OK (moteur %s, outil %s) : %d visible(s), %d nouveau(x), %d retiré(s)",
            etat.get("mode", "?").upper(), tool_id, total_visible, len(nouveaux), len(disparus),
        )
    except SurchargeSite:
        etat["echecs_consecutifs"] = etat.get("echecs_consecutifs", 0) + 1
        LOG.info("Site en surcharge (« Vous êtes trop nombreux ») — nouvel essai au prochain cycle.")
        gerer_alerte_panne(etat)
    except Exception as exc:
        etat["echecs_consecutifs"] = etat.get("echecs_consecutifs", 0) + 1
        LOG.warning("Vérification en échec : %s", exc)
        gerer_alerte_panne(etat)

    gerer_heartbeat(etat, total_visible)
    sauvegarder_etat(etat)
    return total_visible, nb_nouveaux


# --------------------------------------------------------------------------
# Modes d'exécution
# --------------------------------------------------------------------------
def boucle_infinie() -> None:
    LOG.info(
        "Démarrage en boucle : %s toutes les %d min (±%d s), heartbeat à %02dh00 (%s)",
        CFG.ville, CFG.intervalle_min, CFG.jitter_s, CFG.heure_heartbeat, libelle_fuseau(),
    )
    etat = charger_etat()
    while True:
        try:
            executer_verification(etat)
        except Exception:  # ceinture + bretelles : la boucle ne meurt JAMAIS
            LOG.exception("Erreur inattendue dans le cycle (le bot continue)")
        attente = CFG.intervalle_min * 60 + random.uniform(-CFG.jitter_s, CFG.jitter_s)
        time.sleep(max(180, attente))  # jamais moins de 3 minutes entre deux cycles


def test_telegram() -> int:
    ok = envoyer_telegram(
        f"🔔 <b>Test réussi !</b>\nLe bot CROUS peut t'écrire ici.\n"
        f"🕒 {maintenant().strftime('%d/%m/%Y %H:%M')} ({libelle_fuseau()})"
    )
    print("Message de test envoyé ✅" if ok else "Échec de l'envoi ❌ (vérifie token et chat_id)")
    return 0 if ok else 1


def selftest() -> int:
    """Test hors-ligne : vérifie la logique de détection sans aucune requête réseau."""
    import tempfile

    CFG.dry_run = True
    CFG.prioritaires = [normaliser("Camille Guérin")]
    CFG.fichier_etat = Path(tempfile.mkdtemp()) / "etat.json"

    def faux(residence, ident):
        return {
            "id": ident, "titre": f"T1 — {residence}", "residence": residence,
            "adresse": "Limoges", "prix": "255,00 €/mois", "details": "18 m²",
            "lien": f"{BASE}/tools/47/accommodations/{ident}",
        }

    tour = {"n": 0}
    scenario = [
        {"101": faux("Résidence La Borie", "101")},                                      # 1er lancement
        {"101": faux("Résidence La Borie", "101"), "202": faux("Camille Guérin", "202")},  # +1 prioritaire
        {"202": faux("Camille Guérin", "202")},                                          # 101 disparaît
        {"202": faux("Camille Guérin", "202"), "101": faux("Résidence La Borie", "101")},  # 101 revient
    ]

    def fetch_factice(tool_id, etat):
        etat["mode"] = "api"
        resultat = scenario[tour["n"]]
        tour["n"] += 1
        return resultat

    def verif(attendu_total, attendu_nouveaux):
        etat = charger_etat()
        etat.setdefault("tool_id", 47)
        etat["tool_id_verifie_le"] = maintenant().date().isoformat()
        total, nouveaux = executer_verification(etat, fetch=fetch_factice)
        assert total == attendu_total, f"total={total}, attendu {attendu_total}"
        assert nouveaux == attendu_nouveaux, f"nouveaux={nouveaux}, attendu {attendu_nouveaux}"

    verif(1, 0)  # premier lancement : on enregistre sans spammer
    verif(2, 1)  # le logement Camille Guérin apparaît → 1 alerte (prioritaire)
    verif(1, 0)  # un logement disparaît → aucune alerte
    verif(2, 1)  # il revient → re-notifié (nouvelle disponibilité)
    assert est_prioritaire(faux("Résidence CAMILLE GUERIN", "x")), "correspondance sans accents KO"
    assert not est_prioritaire(faux("Résidence La Borie", "x"))
    print("SELFTEST OK ✅ — logique de détection, priorités et état validés")
    return 0


def main() -> int:
    global CFG
    charger_fichier_env()
    CFG = Config()
    configurer_logs()

    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--selftest":
        return selftest()
    CFG.verifier_telegram()
    if arg == "--test-telegram":
        return test_telegram()
    if arg == "--once":
        etat = charger_etat()
        executer_verification(etat)
        return 0  # même en cas d'échec doux : GitHub Actions ne doit pas « échouer »
    boucle_infinie()
    return 0


if __name__ == "__main__":
    sys.exit(main())
