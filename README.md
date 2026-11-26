# Allerta Bisenzio

Pagina web gratuita che mostra livelli del Bisenzio, pioggia a Gamberame e Campi Bisenzio, un indice di allerta stimato e un simulatore per il giorno dopo. Si aggiorna da sola ogni ora con GitHub Actions e si pubblica con GitHub Pages.

Dati: Servizio Idrologico Regionale Toscana (sir.toscana.it). È una stima indicativa: per le decisioni valgono gli avvisi del Centro Funzionale Regionale e della Protezione Civile.

## Cosa c'è nel progetto

| File | A cosa serve |
|---|---|
| `scripts/aggiorna.py` | Scarica i dati dal SIR, calcola l'indice, scrive `docs/data.json`, manda l'avviso Telegram |
| `.github/workflows/aggiorna.yml` | Fa girare lo script ogni ora, gratis, sui server di GitHub |
| `docs/index.html` | La pagina web (funziona anche da telefono) |
| `docs/data.json` | I dati in tempo reale, aggiornati ogni ora |
| `docs/storico.json` | Lo storico giornaliero dal 1998 (già pieno, aggiornato una volta al giorno) |
| `docs/manifest.json`, `docs/sw.js`, icone | Rendono la pagina installabile come app (e pronta per il Play Store) |

---

## Passo 1 – Crea l'account GitHub

1. Vai su **github.com** → **Sign up**.
2. Inserisci email, password e un nome utente (es. `gamba-bisenzio`): comparirà nell'indirizzo del sito.
3. Conferma l'email.

## Passo 2 – Crea il repository

1. In alto a destra **+** → **New repository**.
2. **Repository name:** `BisenzioLive`.
3. Scegli **Public** (GitHub Pages gratuito richiede un repository pubblico).
4. Non spuntare nient'altro → **Create repository**.

## Passo 3 – Carica i file

1. Scompatta lo zip sul computer.
2. Nella pagina del repository appena creato clicca **uploading an existing file**.
3. Trascina **il contenuto** della cartella `BisenzioLive` (le cartelle `docs`, `scripts`, `.github` e i file `README.md`, `.gitignore`).
   - Attenzione: la cartella `.github` su Windows può essere nascosta. In Esplora file: **Visualizza → Mostra → Elementi nascosti**.
   - Se il trascinamento non prende la cartella `.github`, creala a mano: **Add file → Create new file**, nome `.github/workflows/aggiorna.yml`, e incolla il contenuto del file.
4. In fondo **Commit changes**.

## Passo 4 – Permetti al workflow di salvare i dati

1. **Settings** (del repository) → **Actions** → **General**.
2. In **Workflow permissions** scegli **Read and write permissions** → **Save**.

## Passo 5 – Pubblica il sito

1. **Settings** → **Pages**.
2. **Source:** *Deploy from a branch*. **Branch:** `main`, cartella **`/docs`** → **Save**.
3. Dopo 1–2 minuti il sito è su `https://TUONOME.github.io/BisenzioLive/`.

## Passo 6 – Primo aggiornamento a mano

1. Scheda **Actions** → se chiede di abilitare i workflow, conferma.
2. Clicca **Aggiorna dati Bisenzio** → **Run workflow** → **Run workflow**.
3. Dopo circa un minuto deve comparire un segno verde. Ricarica il sito: la data in alto deve essere quella di adesso.
4. Da qui in poi gira da solo ogni ora (GitHub può ritardare di qualche minuto).

Se compare una X rossa, apri l'esecuzione e guarda il passo *Scarica dati*: se dice che sir.toscana.it non risponde, è possibile che il sito blocchi i server di GitHub (che sono all'estero). In quel caso lo script si può far girare da un PC di casa: scrivimi e ti preparo l'alternativa.

## Passo 7 (facoltativo) – Avvisi su Telegram

Ricevi un messaggio quando l'allerta stimata cambia o un livello supera una soglia.

1. In Telegram cerca **@BotFather** → scrivi `/newbot` → scegli un nome (es. *Allerta Bisenzio Gamba*) e un nome utente che finisca in `bot`.
2. BotFather ti dà un **token** (tipo `123456:ABC...`). Tienilo segreto.
3. Apri il tuo nuovo bot e scrivigli un messaggio qualsiasi (es. "ciao").
4. Nel browser apri `https://api.telegram.org/botTOKEN/getUpdates` (sostituisci TOKEN). Cerca `"chat":{"id":123456789` : quel numero è il tuo **chat id**.
5. Su GitHub: **Settings** → **Secrets and variables** → **Actions** → **New repository secret**:
   - nome `TELEGRAM_TOKEN`, valore il token;
   - nome `TELEGRAM_CHAT_ID`, valore il chat id.
6. Rilancia il workflow (Passo 6): alla prima esecuzione arriva il messaggio con la situazione.

## Passo 8 – Installarla sul telefono

Apri il sito con Chrome su Android → menu **⋮** → **Aggiungi a schermata Home** (o **Installa app**). Si apre a schermo intero come un'app, con la sua icona.

---

## Dopo: pubblicarla sul Play Store

La pagina è già una *Progressive Web App* (manifest, icone, funzionamento offline). Per il Play Store si "impacchetta" senza riscriverla:

1. **PWABuilder** (pwabuilder.com, di Microsoft, gratuito): inserisci l'indirizzo del sito → **Package for stores** → **Android**. Genera il pacchetto `.aab` per il Play Store (e un `.apk` per provarlo sul tuo telefono) usando la tecnologia *Trusted Web Activity*.
2. PWABuilder ti dà anche un file `assetlinks.json`: va messo in `docs/.well-known/assetlinks.json`, serve a Google per verificare che il sito è tuo.
3. **Account sviluppatore Google Play**: costo una tantum di 25 dollari. Per i nuovi account personali Google chiede un periodo di test chiuso con alcuni tester prima della pubblicazione.
4. Servono: privacy policy (l'app non raccoglie dati personali, va comunque scritta e pubblicata), screenshot, descrizione, classificazione dei contenuti.

Prima di pubblicare per altre persone:
- **Dati:** chiedi al Servizio Idrologico l'autorizzazione o un accesso ufficiale ai dati (servizio.idrologico@regione.toscana.it) e cita sempre la fonte.
- **Responsabilità:** l'app deve dire chiaramente che non è un servizio ufficiale di allerta. Google è attento alle app che sembrano servizi di emergenza o governativi: evita nomi e loghi che possano farla sembrare della Regione o della Protezione Civile.
- **Affidabilità:** GitHub Actions non garantisce l'esecuzione puntuale ogni ora. Per un'app pubblica conviene un piccolo server a pagamento (pochi euro al mese).

## Come funziona l'indice

Per ogni giorno si prende la misura più critica rispetto alla sua soglia "rossa":

| Misura | Soglia rossa |
|---|---|
| Pioggia 24 ore (max tra Gamberame e Case Passerini) | 100 mm |
| Pioggia 48 ore | 140 mm |
| Livello medio San Piero a Ponti | 3,50 m |
| Livello medio Gamberame | 1,50 m |

Indice ≥ 100 rosso, ≥ 75 arancione, ≥ 55 giallo. Tarato sull'alluvione del 2–3 novembre 2023, che risulta il giorno peggiore dal 2022. Le soglie si cambiano in `scripts/aggiorna.py` (variabile `SOGLIA`).

Il simulatore cerca i 6 giorni dal 1998 più simili per pioggia a Gamberame, livello di partenza a San Piero a Ponti e pioggia del giorno prima, e mostra la mediana del livello raggiunto.

## Lo storico dal 1998

Il livello di San Piero a Ponti è in archivio dal 1998, per questo lo storico parte da lì. La pioggia a Gamberame è misurata dal 2001 e manca quasi tutto il 2013: in quei giorni è stimata dal primo pluviometro vicino disponibile (Vaiano acquedotto, Prato Galceti, Cantagallo, Vernio), moltiplicato per il rapporto medio con Gamberame. La colonna `fonte_pioggia_gamberame` dice per ogni giorno se il dato è misurato o stimato.
