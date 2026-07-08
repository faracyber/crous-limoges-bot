# 🏠 Bot de surveillance des logements CROUS — Limoges

Ce bot vérifie environ **toutes les 5 minutes** les logements CROUS disponibles à
Limoges sur [trouverunlogement.lescrous.fr](https://trouverunlogement.lescrous.fr)
et t'envoie une **notification Telegram instantanée** dès qu'un logement apparaît,
avec le lien direct pour réserver. Les résidences prioritaires (par défaut :
**Camille Guérin**) déclenchent une alerte spéciale 🚨 répétée plusieurs fois.

## Ce que fait le bot

- **Double moteur de détection.** Il interroge d'abord l'API JSON interne du site
  (`POST /api/fr/search/<idTool>`, celle que le site utilise lui-même). Si l'API
  change de format un jour, il bascule automatiquement sur la lecture de la page
  HTML de recherche. Il retient le moteur qui fonctionne.
- **Détection automatique de la campagne.** L'identifiant d'« outil » change à
  chaque phase (42 pour 2025-2026, 47 pour 2026-2027 au moment où j'écris). Le bot
  le relit chaque jour sur la page d'accueil : rien à modifier quand le CROUS
  change de phase.
- **Notifie uniquement les nouveautés.** L'état est sauvegardé dans
  `data/etat.json` ; chaque vérification est comparée à la précédente. Un logement
  qui disparaît puis revient est re-signalé (c'est une nouvelle disponibilité).
- **Heartbeat quotidien** à 9h00 (silencieux, sans sonnerie) pour confirmer que le
  bot tourne, avec le nombre de logements visibles.
- **Alerte de panne** : si le site est inaccessible plus d'une heure (surcharge,
  maintenance), tu reçois UNE info discrète, pas un déluge.
- **Robuste** : erreurs réseau réessayées avec pauses croissantes (5 s → 15 s →
  45 s), écriture d'état atomique, la boucle ne meurt jamais.

## Ce que le bot ne fait PAS (volontairement)

- Il **ne réserve pas à ta place** : la réservation passe par ton compte
  [messervices.etudiant.gouv.fr](https://messervices.etudiant.gouv.fr) et doit
  rester manuelle.
- Il **ne contourne aucune protection**. La page officielle « Vous êtes trop
  nombreux ! » (salle d'attente activée quand le site est saturé, comme en ce
  début de phase complémentaire) est détectée et traitée comme une simple pause :
  le bot réessaie au cycle suivant, c'est tout.
- Il reste **léger pour le serveur** : une requête toutes les ~5 minutes (jamais
  moins de 3), avec un décalage aléatoire de ±60 s.
- Il surveille **l'offre publique (anonyme)**. Le site précise que l'offre
  affichée peut différer selon ton profil une fois connecté : pense donc aussi à
  vérifier de temps en temps en étant authentifié.

---

## Étape 1 — Créer le bot Telegram (2 minutes)

1. Dans Telegram, ouvre **@BotFather** (le compte officiel, coche bleue).
2. Envoie `/newbot`.
3. Donne un nom d'affichage (ex. `CROUS Limoges Alerte`).
4. Donne un identifiant unique se terminant par `bot` (ex. `crous_limoges_sam_bot`).
5. BotFather te répond avec le **token**, de la forme
   `123456789:AAxxxxxxxxxxxxxxxxxxxxxxxx`. **Garde-le secret** — c'est lui que tu
   mettras dans `TELEGRAM_BOT_TOKEN`.

### Récupérer ton `chat_id`

1. Ouvre ton nouveau bot dans Telegram (lien donné par BotFather) et envoie-lui
   n'importe quel message, par exemple `salut` — indispensable : un bot ne peut
   écrire qu'aux personnes qui lui ont d'abord parlé.
2. Dans un navigateur, ouvre (en remplaçant `<TOKEN>` par ton token) :
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Dans la réponse, repère `"chat":{"id":123456789,...}` → ce nombre est ton
   `TELEGRAM_CHAT_ID`.
   *(Alternative : envoie `/start` à @userinfobot, qui affiche ton id.)*

---

## Étape 2 — Tester sur ton ordinateur (5 minutes)

Prérequis : Python 3.10 ou plus récent ([python.org](https://www.python.org/downloads/) ;
sous Windows, coche « Add Python to PATH » à l'installation).

```bash
# 1. Dans le dossier du projet
cd crous-limoges-bot

# 2. Environnement virtuel + dépendances
python -m venv .venv
# Windows :
.venv\Scripts\activate
# Mac / Linux :
source .venv/bin/activate
pip install -r requirements.txt

# 3. Configuration
# copie .env.example en .env, puis ouvre .env et remplis
# TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID
# Windows :  copy .env.example .env
# Mac/Linux: cp .env.example .env

# 4. Les trois tests, dans l'ordre
python crous_bot.py --selftest        # logique interne, sans réseau → "SELFTEST OK ✅"
python crous_bot.py --test-telegram   # tu dois recevoir 🔔 sur Telegram
python crous_bot.py --once            # une vraie vérification sur le site
```

Au premier `--once`, tu reçois « 🚀 Bot CROUS démarré » avec le nombre de
logements visibles (souvent 0 en ce moment — c'est normal, c'est justement
pour ça qu'on surveille). Si le site affiche sa page de surcharge, le log
indique `Site en surcharge` : le bot réessaiera, rien à corriger.

Relance `--once` une deuxième fois : cette fois, aucun message (rien de
nouveau) — la mémoire fonctionne. Tu peux ensuite laisser tourner en continu
avec `python crous_bot.py` tant que ton PC est allumé, mais l'objectif est
l'étape 3.

---

## Étape 3 — Hébergement gratuit 24h/24

### Comparatif rapide (situation vérifiée début juillet 2026)

| Option | Prix | Carte bancaire ? | Fréquence réelle | Verdict |
|---|---|---|---|---|
| **GitHub Actions** | 0 € (dépôt public) | Non | ~5 à 15 min (planification parfois retardée) | ✅ **Recommandé pour démarrer aujourd'hui** |
| **Oracle Cloud Always Free** | 0 € permanent | Oui, à l'inscription (vérification souvent capricieuse avec les cartes algériennes) | 5 min pile, 24h/24 | ✅ Le meilleur techniquement, si l'inscription passe |
| PythonAnywhere gratuit | 0 € | Non | ✗ 1 tâche planifiée **par jour** seulement + accès sortant limité à une liste blanche | ❌ Inadapté |
| Render gratuit | 0 € | — | ✗ les services gratuits s'endorment après inactivité ; workers/cron payants | ❌ Inadapté |
| Railway | — | — | ✗ plus d'offre gratuite permanente (crédit d'essai puis payant) | ❌ |

**Ma recommandation : commence par GitHub Actions maintenant** (10 minutes de
mise en place, zéro carte bancaire, zéro serveur à administrer), et si tu veux
la précision « 5 minutes pile » plus tard, passe à un petit serveur (section
suivante). La latence de GitHub Actions est le seul compromis : les exécutions
planifiées partent parfois avec quelques minutes de retard aux heures chargées.
Même ainsi, tu passes de « je ne peux pas surveiller » à « vérifié en continu,
jour et nuit ».

### Guide pas à pas : GitHub Actions

1. **Crée un compte** sur [github.com](https://github.com) si besoin.
2. **Crée un dépôt** : bouton « New repository » → nom `crous-limoges-bot` →
   **Public** (les minutes d'exécution sont illimitées sur les dépôts publics ;
   en privé, la fréquence 5 min dépasserait le quota gratuit mensuel).
   Ne coche rien d'autre → « Create repository ».
3. **Envoie les fichiers** : sur la page du dépôt, « uploading an existing
   file » → glisse **tout le contenu du dossier** `crous-limoges-bot`
   (`crous_bot.py`, `requirements.txt`, `.gitignore`, et surtout le dossier
   `.github/workflows/` avec `surveillance-crous.yml` — la structure des
   dossiers doit être conservée) → « Commit changes ».
   ⚠️ **N'envoie jamais ton fichier `.env`** : les secrets vont ailleurs (point 4).
   💡 Si l'upload par glisser-déposer ne conserve pas le dossier `.github`,
   crée le fichier à la main : « Add file → Create new file », tape comme nom
   `.github/workflows/surveillance-crous.yml` (les `/` créent les dossiers),
   puis colle le contenu du fichier.
4. **Ajoute les secrets** : `Settings` → `Secrets and variables` → `Actions` →
   `New repository secret` :
   - Nom `TELEGRAM_BOT_TOKEN`, valeur = ton token BotFather ;
   - Nom `TELEGRAM_CHAT_ID`, valeur = ton chat id.
   Les secrets sont chiffrés et invisibles, même sur un dépôt public.
5. **Active et teste** : onglet `Actions` → accepte l'activation des workflows →
   clique `Surveillance CROUS Limoges` → bouton `Run workflow`. En ~1 minute
   l'exécution passe au vert et tu reçois « 🚀 Bot CROUS démarré » sur Telegram.
6. C'est tout. Le planificateur prend le relais (~toutes les 5 min). Le fichier
   `data/etat.json` est committé automatiquement après chaque changement : c'est
   la mémoire du bot entre deux exécutions, et ces commits gardent le dépôt
   « actif » (GitHub coupe les planifications après 60 jours d'inactivité — ici,
   ça n'arrivera donc pas).

Pour vérifier que tout roule : onglet `Actions` (liste des exécutions), et le
heartbeat quotidien de 9h sur Telegram. Pour **mettre en pause** : `Actions` →
`Surveillance CROUS Limoges` → menu `⋯` → `Disable workflow`.

Pour ajuster ville, résidences prioritaires ou heure du heartbeat : modifie le
bloc `env:` dans `.github/workflows/surveillance-crous.yml` directement sur
GitHub (icône crayon) — pas besoin de toucher au code.

### Option « précision maximale » : un serveur 24h/24

Un vrai serveur exécute la boucle en continu : 5 minutes pile, aucune latence.

- **Oracle Cloud Always Free** ([oracle.com/cloud/free](https://www.oracle.com/cloud/free/)) :
  2 petites VM AMD ou jusqu'à 4 cœurs ARM gratuits en permanence — largement
  au-delà du besoin. Seul obstacle : l'inscription exige une carte bancaire de
  vérification, souvent refusée depuis l'Algérie. Si tu as une carte
  internationale qui passe, c'est la meilleure option gratuite.
- **Alternative à ~3 €/mois** : un mini-VPS (Hetzner, Ionos, OVH...) — même
  procédure ci-dessous.
- **Une fois en France** : un vieux PC ou un Raspberry Pi chez toi fait
  parfaitement l'affaire (déconseillé depuis l'Algérie à cause des coupures
  internet possibles, notamment en période d'examens nationaux).

Installation sur une VM Ubuntu (Oracle, VPS ou Raspberry) :

```bash
sudo apt update && sudo apt install -y python3-venv git
git clone https://github.com/TON_COMPTE/crous-limoges-bot.git
cd crous-limoges-bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env      # remplis token + chat_id, ajuste TIMEZONE
.venv/bin/python crous_bot.py --test-telegram   # test

# Lancement en service permanent (redémarre seul après erreur ou reboot)
sudo cp deploy/crous-bot.service /etc/systemd/system/
# ⚠️ ouvre le fichier et adapte User= et les chemins si ton utilisateur n'est pas "ubuntu"
sudo systemctl daemon-reload
sudo systemctl enable --now crous-bot
journalctl -u crous-bot -f            # suivre les logs en direct (Ctrl+C pour quitter)
```

---

## Comprendre les notifications

| Message | Signification |
|---|---|
| 🚀 Bot CROUS démarré | Premier lancement : l'existant est mémorisé, sans alerte logement par logement |
| 🏠 Nouveau logement CROUS | Un logement vient d'apparaître — **fonce** |
| 🚨🚨🚨 RÉSIDENCE PRIORITAIRE | Une résidence de ta liste `PRIORITY_RESIDENCES` — alerte répétée 3× |
| ➕ X autres nouveaux logements | Plus de 8 nouveautés d'un coup : résumé avec lien vers la liste complète |
| ✅ Bot CROUS actif (silencieux) | Heartbeat quotidien de 9h : preuve de vie + nombre de logements visibles |
| ⚠️ Impossible de consulter le site | Site saturé/maintenance depuis ~1h ; le bot continue seul, rien à faire |

## Le jour où l'alerte sonne — sois prêt·e à réserver en quelques minutes

1. **Compte prêt** : identifiants [messervices.etudiant.gouv.fr](https://messervices.etudiant.gouv.fr)
   enregistrés dans ton téléphone ET ton navigateur, session testée à l'avance.
2. En phase complémentaire, on ne peut déposer qu'**une seule demande à la fois
   par Crous** : choisis vite, tu pourras ajuster ensuite si elle n'aboutit pas.
3. **Prépare dès maintenant** ce qui sera demandé pour finaliser : une avance
   sur loyer d'environ 70 € (carte bancaire fonctionnelle en ligne !), un garant
   ou la caution gratuite [Visale](https://www.visale.fr/) (fais la demande
   Visale dès maintenant, c'est le point le plus long), et une attestation
   d'assurance habitation.
4. Clique le lien de la notification, connecte-toi, dépose la demande. Chaque
   minute compte : les logements repartent vite, d'où ce bot.

## FAQ / Dépannage

**Le log affiche « Site en surcharge » presque à chaque cycle.**
Normal en début de phase complémentaire : le CROUS active sa salle d'attente
quand trop de monde se connecte. Le bot réessaie sans forcer ; les créneaux plus
calmes (nuit, tôt le matin) passent généralement.

**Je veux surveiller une autre ville.**
Fais une recherche manuelle de la ville sur le site, copie la valeur `bounds=`
dans l'URL de résultats, colle-la dans `BOUNDS` (et change `VILLE`).

**Changer la fréquence ?**
`CHECK_INTERVAL_MINUTES` (boucle serveur) ou la ligne `cron:` du workflow.
Reste à 5 min minimum : plus agressif n'apporte presque rien et charge le site.

**La phase change (nouvel « outil ») — je dois faire quoi ?**
Rien. Le bot re-détecte l'outil chaque jour et te prévient (« ℹ️ Nouvelle
campagne détectée ») en repartant sur une mémoire propre.

**Je déménage en France fin août.**
Mets `TIMEZONE=Europe/Paris` (dans `.env` ou le workflow) pour que les heures
affichées suivent. Le reste ne change pas.

**Arrêter le bot ?**
GitHub Actions : `Disable workflow`. Serveur : `sudo systemctl disable --now crous-bot`.

**GitHub Actions : l'exécution est verte mais aucun message reçu.**
Vérifie les deux secrets (noms exacts), et que tu as bien envoyé un premier
message à ton bot sur Telegram. Le détail des logs est dans l'exécution,
étape « Vérifier les logements ».

## Toutes les variables

| Variable | Défaut | Rôle |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Token donné par BotFather (**obligatoire**) |
| `TELEGRAM_CHAT_ID` | — | Ton identifiant de chat (**obligatoire**) |
| `VILLE` | Limoges | Nom affiché dans les messages |
| `BOUNDS` | `1.15_45.93_1.35_45.77` | Rectangle ouest_nord_est_sud de recherche |
| `TARGET_YEAR` | 2026-2027 | Année pour la détection automatique de l'outil |
| `TOOL_ID` | *(vide)* | Force un identifiant d'outil (sinon détection auto) |
| `PRIORITY_RESIDENCES` | Camille Guérin | Résidences à alerte 🚨 (virgules, accents ignorés) |
| `PRIORITY_REPEAT` | 3 | Nombre de messages pour une alerte prioritaire |
| `CHECK_INTERVAL_MINUTES` | 5 | Intervalle entre vérifications (boucle) |
| `JITTER_SECONDS` | 60 | Décalage aléatoire ± appliqué à l'intervalle |
| `HEARTBEAT_HOUR` | 9 | Heure du message quotidien de preuve de vie |
| `TIMEZONE` | Africa/Algiers | Fuseau des logs et notifications |
| `STATE_FILE` | data/etat.json | Mémoire des logements déjà vus |
| `LOG_FILE` | crous_bot.log | Journal (vide = console seulement) |
| `MAX_DETAILED_ALERTS` | 8 | Au-delà : un résumé au lieu du détail |
| `DRY_RUN` | *(off)* | `1` = tout sauf l'envoi Telegram (tests) |
