"""
Allerta Bisenzio - aggiornamento dati.

Scarica dal Servizio Idrologico Regionale Toscana (sir.toscana.it):
  - i valori in tempo reale di livelli e piogge (ogni esecuzione);
  - le serie dettagliate dei livelli (ultime 36 ore) per velocità di salita e colmi;
  - l'archivio giornaliero dal 1998 (una volta al giorno).
Legge anche l'allerta ufficiale del Centro Funzionale e le previsioni meteo.
Calcola l'indice di allerta e scrive docs/data.json, letto dalla pagina web.
Se sono impostati TELEGRAM_TOKEN e TELEGRAM_CHAT_ID manda un messaggio
quando l'allerta stimata cambia o un livello supera una soglia.

Fonte dei dati: Servizio Idrologico Regionale Toscana. Dati in tempo reale
non validati. Stima indicativa: valgono gli avvisi ufficiali del CFR.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = "https://www.sir.toscana.it"
ROMA = ZoneInfo("Europe/Rome")
DATA_FILE = Path(__file__).resolve().parent.parent / "docs" / "data.json"
STORICO_FILE = DATA_FILE.with_name("storico.json")  # aggiornato una volta al giorno
CAMPI_STORICO = ["data", "pioggia_gamberame_mm", "fonte_pioggia_gamberame", "pioggia_campi_mm",
                 "livello_san_piero_m", "livello_gamberame_m", "indice", "livello", "causa"]
INIZIO_STORICO = "1998-01-01"  # da qui c'è il livello di S. Piero a Ponti
# Pluviometri usati per stimare Gamberame dove manca il dato (nome, codici SIR)
VICINI_GAMBERAME = [
    ("Vaiano acquedotto", ["TOS11000503"]),
    ("Prato Galceti", ["TOS10001210"]),
    ("Cantagallo", ["TOS10001150", "TOS01001151"]),
    ("Vernio", ["TOS01001171"]),
]
LETTURE_REGISTRO = 4 * 24 * 7  # letture ogni 15 minuti per 7 giorni
MINUTI_DATO_VECCHIO = 75      # oltre questo un dato di stazione è considerato non aggiornato

# Stazioni
IDRO = {
    "san_piero": {"id": "TOS01004791", "nome": "S. Piero a Ponti", "fiume": "Bisenzio"},
    "gamberame": {"id": "TOS01004779", "nome": "Gamberame", "fiume": "Bisenzio"},
    "marina": {"id": "TOS01004784", "nome": "Calenzano", "fiume": "Marina"},
    "signa": {"id": "TOS01004811", "nome": "Ponte a Signa", "fiume": "Arno"},
    "prato": {"id": "TOS01004782", "nome": "Prato", "fiume": "Bisenzio"},
}
# Stazioni di cui si scarica anche la serie dettagliata (5-15 minuti, ultime ~36 ore)
IDRO_SERIE = ["san_piero", "gamberame", "marina", "signa"]
PLUVIO = {
    "gamberame": {"id": "TOS01004779", "nome": "Gamberame (Vaiano)"},
    "campi": {"id": "TOS01001225", "nome": "Case Passerini (Campi Bisenzio)"},
    "vernio": {"id": "TOS01001171", "nome": "Vernio"},
    "cantagallo": {"id": "TOS01001151", "nome": "Cantagallo"},
    "vaiano": {"id": "TOS11000503", "nome": "Vaiano acquedotto"},
    "calenzano": {"id": "TOS01004784", "nome": "Calenzano (Marina)"},
}
# Soglie dell'indicatore (100 = rosso)
SOGLIA = {"pioggia24h": 100.0, "pioggia48h": 140.0}
# Livelli del Bisenzio [giallo, arancione, rosso] in metri.
# Giallo = prima soglia ufficiale SIR, rosso = seconda soglia, arancione a metà.
SOGLIE_LIVELLO = {"san_piero": [3.5, 4.5, 5.5], "gamberame": [1.8, 2.3, 2.8],
                  "marina": [1.5, 2.0, 2.5], "signa": [5.0, 6.25, 7.5]}
# Velocità di crescita del livello [giallo, arancione, rosso] in cm/ora (max tra ultima ora e media 3 ore).
# Valori iniziali da verificare quando l'app avrà raccolto qualche piena con dati ogni 15 minuti.
SOGLIE_VELOCITA = {"san_piero": [15, 30, 50], "gamberame": [25, 50, 80], "marina": [20, 40, 60], "signa": [15, 30, 50]}
# Pioggia breve: tempo di ritorno stimato dal SIR [giallo, arancione, rosso] in anni (1, 3, 6, 12 e 24 ore)
SOGLIE_TEMPO_RITORNO = [2, 5, 20]
DURATE_TR = ["1h", "3h", "6h", "12h", "24h"]
# Arno alto a Signa: il Bisenzio scarica più lentamente (rigurgito)
ARNO_RIGURGITO_M = 4.0
# Tempo di arrivo del colmo da Gamberame a S. Piero a Ponti (ore). Valore iniziale dall'evento del 16/11/2026,
# poi aggiornato dalla mediana delle piene osservate dall'app.
RITARDO_INIZIALE_ORE = 4.0


def quota_livello(v, soglie):
    """Trasforma un livello in quota dell'indice: soglia gialla = 0,55, arancione = 0,75, rossa = 1."""
    giallo, arancio, rosso = soglie
    if v <= giallo:
        return 0.55 * v / giallo
    if v <= arancio:
        return 0.55 + 0.20 * (v - giallo) / (arancio - giallo)
    if v <= rosso:
        return 0.75 + 0.25 * (v - arancio) / (rosso - arancio)
    return 1.0 + 0.25 * (v - rosso) / (rosso - arancio)

# Punti dell'alta valle per la previsione di pioggia del modello meteo (nome, lat, lon)
PUNTI_PREVISIONE = [
    ("Gamberame", 43.923, 11.127),
    ("Vaiano", 43.971, 11.129),
    ("Cantagallo", 44.022, 11.079),
    ("Vernio", 44.057, 11.121),
]
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
CFR_VIGILANZA = "https://www.cfr.toscana.it/index.php?IDS=2&IDSS=71"
MESI = {m: i + 1 for i, m in enumerate(["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
                                         "agosto", "settembre", "ottobre", "novembre", "dicembre"])}

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "bisenzio-live/2.0 (uso personale, poche richieste ogni 15 minuti)"


def scarica(path):
    r = SESSION.get(BASE + path, timeout=60)
    r.raise_for_status()
    return r.content.decode("latin-1")


def num(v):
    if v is None:
        return None
    v = re.sub(r"<[^>]+>", "", str(v)).strip().replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return None


def riga_js(html, codice):
    """Estrae l'array JavaScript `new Array("CODICE", ...)` dalla pagina di monitoraggio."""
    m = re.search(r'new Array\("' + codice + r'"(.*?)\);', html, re.S)
    if not m:
        return None
    return [codice] + re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))


def data_ora(s, adesso):
    """'12/11 11.20' -> ISO con anno (gestisce il cambio d'anno)."""
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})\s+(\d{1,2})[.:](\d{2})", s or "")
    if not m:
        return None
    g, me, h, mi = map(int, m.groups())
    dt = datetime(adesso.year, me, g, h, mi, tzinfo=ROMA)
    if dt - adesso > timedelta(days=2):
        dt = dt.replace(year=adesso.year - 1)
    return dt.isoformat()


def eta_minuti(iso, adesso):
    if not iso:
        return None
    return round((adesso - datetime.fromisoformat(iso)).total_seconds() / 60)


def tempo_reale(adesso):
    idro_html = scarica("/monitoraggio/stazioni.php?type=idro")
    pluvio_html = scarica("/monitoraggio/stazioni.php?type=pluvio")
    out = {"livelli": {}, "piogge": {}}
    for chiave, st in IDRO.items():
        r = riga_js(idro_html, st["id"])
        if not r or len(r) < 14:
            out["livelli"][chiave] = None
            continue
        ora = data_ora(r[13], adesso)
        eta = eta_minuti(ora, adesso)
        out["livelli"][chiave] = {
            "nome": st["nome"], "fiume": st["fiume"],
            "soglia1": num(r[7]),
            "soglia2": num(r[6]),
            "livello": num(r[8]),
            "variazioni": [num(r[10]), num(r[11]), num(r[12])],
            "ora": ora, "eta_min": eta,
            "vecchio": eta is None or eta > MINUTI_DATO_VECCHIO,
        }
    for chiave, st in PLUVIO.items():
        r = riga_js(pluvio_html, st["id"])
        if not r or len(r) < 12:
            out["piogge"][chiave] = None
            continue
        ora = data_ora(r[11], adesso)
        eta = eta_minuti(ora, adesso)
        tr = {d: num(r[12 + i]) if len(r) > 12 + i else None for i, d in enumerate(DURATE_TR)}
        out["piogge"][chiave] = {
            "nome": st["nome"],
            "mm_15min": num(r[4]), "mm_1h": num(r[5]), "mm_3h": num(r[6]),
            "mm_6h": num(r[7]), "mm_12h": num(r[8]), "mm_24h": num(r[9]),
            "mm_36h": num(r[10]),
            "tempo_ritorno": tr,
            "ora": ora, "eta_min": eta,
            "vecchio": eta is None or eta > MINUTI_DATO_VECCHIO,
        }
    return out


def serie_dettaglio(chiave, adesso):
    """Serie del livello ogni 5-15 minuti, ultime ~36 ore (pagina di dettaglio del SIR)."""
    testo = scarica(f"/monitoraggio/dettaglio.php?id={IDRO[chiave]['id']}&type=idro")
    punti = []
    for m in re.finditer(r'new Array\("[^"]*","(\d{2})/(\d{2})/(\d{4}) (\d{2})[.:](\d{2})","([^"]*)"', testo):
        g, me, a, h, mi, v = m.groups()
        val = num(v)
        if val is not None:
            punti.append((datetime(int(a), int(me), int(g), int(h), int(mi), tzinfo=ROMA), val))
    punti.sort()
    return punti


def valore_a(punti, quando, tolleranza_min=40):
    """Valore della serie più vicino all'istante richiesto (entro la tolleranza)."""
    migliore = min(punti, key=lambda p: abs((p[0] - quando).total_seconds()), default=None)
    if migliore and abs((migliore[0] - quando).total_seconds()) <= tolleranza_min * 60:
        return migliore[1]
    return None


def analisi_serie(punti):
    """Velocità di crescita (cm/ora) e colmo delle ultime 24 ore."""
    if len(punti) < 2:
        return None
    t_ult, v_ult = punti[-1]
    v1, v3 = valore_a(punti, t_ult - timedelta(hours=1)), valore_a(punti, t_ult - timedelta(hours=3))
    ultime24 = [p for p in punti if p[0] >= t_ult - timedelta(hours=24)]
    picco = max(ultime24, key=lambda p: p[1])
    return {
        "cm_ora_1h": None if v1 is None else round((v_ult - v1) * 100),
        "cm_ora_3h": None if v3 is None else round((v_ult - v3) * 100 / 3),
        "colmo_24h": {"ora": picco[0].isoformat(timespec="minutes"), "livello": picco[1]},
        "minimo_24h": min(p[1] for p in ultime24),
    }


def serie_quarti_ora(punti):
    """Riduce la serie a un punto ogni 15 minuti per il grafico della pagina."""
    out, ultimo = [], None
    for t, v in punti:
        q = t.replace(minute=t.minute - t.minute % 15, second=0)
        if q != ultimo:
            out.append([q.isoformat(timespec="minutes"), v])
            ultimo = q
    return out


def ritardi_osservati(serie, vecchi, adesso):
    """Cerca piene concluse in cui il colmo è passato da Gamberame a S. Piero e ne misura il ritardo."""
    g, sp = serie.get("gamberame") or [], serie.get("san_piero") or []
    noti = {r["colmo_gamberame"] for r in vecchi}
    nuovi = []
    if len(g) < 10 or len(sp) < 10:
        return vecchi
    for i, (t, v) in enumerate(g):
        finestra = [p for p in g if abs((p[0] - t).total_seconds()) <= 6 * 3600]
        if v < max(p[1] for p in finestra) or t > adesso - timedelta(hours=3):
            continue  # non è il massimo locale, o è troppo recente per dire che è un colmo
        prima = [p[1] for p in g if t - timedelta(hours=12) <= p[0] < t]
        if not prima or v - min(prima) < 0.15:
            continue  # salita troppo piccola per essere una piena
        dopo = [p for p in sp if t + timedelta(hours=1) <= p[0] <= t + timedelta(hours=10)]
        if not dopo:
            continue
        colmo_sp = max(dopo, key=lambda p: p[1])
        base_sp = valore_a(sp, t - timedelta(hours=1), 90)
        if base_sp is None or colmo_sp[1] - base_sp < 0.2 or colmo_sp[0] > adesso - timedelta(hours=1):
            continue
        if colmo_sp[0] == dopo[-1][0]:
            continue  # sta ancora salendo alla fine della finestra
        chiave = t.isoformat(timespec="minutes")
        if chiave in noti:
            continue
        noti.add(chiave)
        nuovi.append({"colmo_gamberame": chiave, "colmo_san_piero": colmo_sp[0].isoformat(timespec="minutes"),
                      "ore": round((colmo_sp[0] - t).total_seconds() / 3600, 1),
                      "salita_gamberame_m": round(v - min(prima), 2)})
    return (vecchi + nuovi)[-20:]


def stima_ritardo(ritardi):
    ore = sorted(r["ore"] for r in ritardi)
    if not ore:
        return {"ore": RITARDO_INIZIALE_ORE, "eventi": 1, "fonte": "stima iniziale dalla piena del 16/11/2026"}
    m = len(ore) // 2
    med = ore[m] if len(ore) % 2 else (ore[m - 1] + ore[m]) / 2
    return {"ore": round(med, 1), "eventi": len(ore), "fonte": "1 piena osservata" if len(ore) == 1 else f"mediana di {len(ore)} piene osservate"}


def previsione_colmo(analisi, ritardo, adesso):
    """Se a Gamberame è in corso o appena passata una piena, stima quando il colmo arriva a S. Piero."""
    g, sp = analisi.get("gamberame"), analisi.get("san_piero")
    if not g:
        return None
    salita = g["colmo_24h"]["livello"] - g["minimo_24h"]
    if salita < 0.15:
        return None
    ore = ritardo["ore"]
    sale = (g["cm_ora_1h"] or 0) > 3
    t_colmo_g = datetime.fromisoformat(g["colmo_24h"]["ora"])
    if sale:
        return {"stato": "in_salita", "non_prima_di": (adesso + timedelta(hours=ore)).isoformat(timespec="minutes"),
                "ritardo_ore": ore,
                "testo": f"A Gamberame il livello sta ancora salendo: il colmo a S. Piero a Ponti arriverà non prima delle {adesso + timedelta(hours=ore):%H:%M} (circa {ore:.0f} ore dopo il colmo di Gamberame)."}
    arrivo = t_colmo_g + timedelta(hours=ore)
    if arrivo < adesso - timedelta(hours=1):
        return None
    if sp and sp["colmo_24h"] and datetime.fromisoformat(sp["colmo_24h"]["ora"]) > t_colmo_g + timedelta(hours=1) and (sp["cm_ora_1h"] or 0) <= 0:
        return {"stato": "passato", "ora": sp["colmo_24h"]["ora"], "ritardo_ore": ore,
                "testo": "Il colmo è già passato anche a S. Piero a Ponti."}
    return {"stato": "atteso", "ora": arrivo.isoformat(timespec="minutes"), "ritardo_ore": ore,
            "testo": f"Colmo passato a Gamberame alle {t_colmo_g:%H:%M}: atteso a S. Piero a Ponti verso le {arrivo:%H:%M}."}


def serie_archivio(tipo, codice):
    """CSV dell'archivio storico: righe 'gg/mm/aaaa;valore;flag'."""
    testo = scarica(f"/archivio/download.php?IDST={tipo}&IDS={codice}")
    serie = {}
    for riga in testo.splitlines():
        m = re.match(r"(\d{2})/(\d{2})/(\d{4});([^;]*);", riga)
        if not m:
            continue
        g, me, a, v = m.groups()
        d = f"{a}-{me}-{g}"
        if d >= INIZIO_STORICO:
            serie[d] = num(v)
    return serie


def storico():
    pg = serie_archivio("pluvio", PLUVIO["gamberame"]["id"])
    pc = serie_archivio("pluvio", PLUVIO["campi"]["id"])
    ls = serie_archivio("idro_l", IDRO["san_piero"]["id"])
    lg = serie_archivio("idro_l", IDRO["gamberame"]["id"])

    # Pluviometri vicini per stimare la pioggia di Gamberame dove manca
    # (misurata solo dal 2001, quasi assente nel 2013). In ordine di affidabilità.
    vicini = []
    for nome, codici in VICINI_GAMBERAME:
        serie = {}
        for c in codici:
            serie.update(serie_archivio("pluvio", c))
        comuni = [d for d in pg if d in serie and pg[d] is not None and serie[d] is not None]
        somma_v = sum(serie[d] for d in comuni)
        fattore = (sum(pg[d] for d in comuni) / somma_v) if comuni and somma_v > 0 else None
        if fattore:
            vicini.append((nome, serie, fattore))

    giorni = sorted(d for d in set(pg) | set(pc) | set(ls) | set(lg) if d >= INIZIO_STORICO)
    righe = []
    for d in giorni:
        g, fonte = pg.get(d), "Gamberame"
        if g is None:
            fonte = ""
            for nome, serie, fattore in vicini:
                if serie.get(d) is not None:
                    g, fonte = round(serie[d] * fattore, 1), f"stima da {nome}"
                    break
        righe.append({"data": d, "pioggia_gamberame_mm": g, "fonte_pioggia_gamberame": fonte,
                      "pioggia_campi_mm": pc.get(d),
                      "livello_san_piero_m": ls.get(d), "livello_gamberame_m": lg.get(d)})
    return righe


def livello_allerta(indice):
    if indice is None:
        return None
    if indice >= 100:
        return "Rosso"
    if indice >= 75:
        return "Arancione"
    if indice >= 55:
        return "Giallo"
    return "Nessuna allerta"


def indice_giornaliero(righe):
    """Stesso indicatore del cruscotto, sui dati giornalieri."""
    ieri = 0.0
    for r in righe:
        p24 = max(r["pioggia_gamberame_mm"] or 0, r["pioggia_campi_mm"] or 0)
        p48 = p24 + ieri
        ieri = p24
        misure = [("Pioggia 24h", p24 / SOGLIA["pioggia24h"]), ("Pioggia 48h", p48 / SOGLIA["pioggia48h"])]
        if r["livello_san_piero_m"] is not None:
            misure.append(("Livello S. Piero", quota_livello(r["livello_san_piero_m"], SOGLIE_LIVELLO["san_piero"])))
        if r["livello_gamberame_m"] is not None:
            misure.append(("Livello Gamberame", quota_livello(r["livello_gamberame_m"], SOGLIE_LIVELLO["gamberame"])))
        causa, quota = max(misure, key=lambda x: x[1])
        r["indice"] = round(100 * quota)
        r["livello"] = livello_allerta(r["indice"])
        r["causa"] = causa
    return righe


def quota_tr(tr):
    """Tempo di ritorno (anni) -> quota dell'indice: 2 anni = 0,55, 5 anni = 0,75, 20 anni = 1."""
    if tr is None or tr <= 0:
        return 0.0
    return quota_livello(tr, SOGLIE_TEMPO_RITORNO)


def indice_tempo_reale(tr, analisi):
    """Stima su valori istantanei. Prende la misura più critica tra pioggia, intensità, livelli e velocità di salita."""
    misure = []
    p = tr["piogge"]
    p24 = max([x["mm_24h"] for x in p.values() if x and not x["vecchio"] and x["mm_24h"] is not None] or [0])
    misure.append(("Pioggia 24 ore", p24 / SOGLIA["pioggia24h"]))
    g = p.get("gamberame")
    if g and g["mm_36h"] is not None:
        misure.append(("Pioggia 36 ore a Gamberame", g["mm_36h"] / SOGLIA["pioggia48h"]))
    for chiave, x in p.items():
        if not x or x["vecchio"]:
            continue
        for durata, anni in (x.get("tempo_ritorno") or {}).items():
            if anni:
                quanto = "1 ora" if durata == "1h" else durata.replace("h", " ore")
                misure.append((f"Pioggia in {quanto} a {x['nome']}: tempo di ritorno {anni:g} anni", quota_tr(anni)))
    for chiave in SOGLIE_LIVELLO:
        l = tr["livelli"].get(chiave)
        if l and l["livello"] is not None and not l["vecchio"]:
            misure.append((f"Livello {l['fiume']} a {l['nome']}", quota_livello(l["livello"], SOGLIE_LIVELLO[chiave])))
        a = analisi.get(chiave)
        if a and l and not l["vecchio"]:
            vel = max([v for v in (a["cm_ora_1h"], a["cm_ora_3h"]) if v is not None] or [0])
            if vel > 0:
                misure.append((f"Salita {l['fiume']} a {l['nome']} ({vel} cm/ora)", quota_livello(vel, SOGLIE_VELOCITA[chiave])))
    misure.sort(key=lambda x: -x[1])
    causa, quota = misure[0]
    indice = round(100 * quota)
    vecchi = [f"{x['nome']}" for x in list(tr["livelli"].values()) + list(p.values()) if x and x["vecchio"]]
    return {"indice": indice, "livello": livello_allerta(indice), "causa": causa,
            "altre_cause": [{"misura": m, "indice": round(100 * q)} for m, q in misure[1:4] if q >= 0.4],
            "dati_non_aggiornati": vecchi}


def terreno(storico_righe, adesso):
    """Quanto è bagnato il terreno: pioggia a Gamberame nei 7 e 15 giorni precedenti, confrontata con lo storico dello stesso periodo dell'anno."""
    if not storico_righe:
        return None
    oggi = adesso.date().isoformat()
    righe = [r for r in storico_righe if r["data"] < oggi]
    def somma(fine_idx, giorni):
        return sum((r["pioggia_gamberame_mm"] or 0) for r in righe[max(0, fine_idx - giorni + 1):fine_idx + 1])
    n = len(righe) - 1
    api7, api15 = round(somma(n, 7), 1), round(somma(n, 15), 1)
    mese = adesso.month
    vicini_mesi = {((mese - 2) % 12) + 1, mese, (mese % 12) + 1}
    storici = [somma(i, 15) for i, r in enumerate(righe) if i >= 14 and int(r["data"][5:7]) in vicini_mesi]
    perc = round(100 * sum(1 for x in storici if x < api15) / len(storici)) if storici else None
    stato = ("asciutto" if perc is None or perc < 40 else "normale" if perc < 70 else
             "umido" if perc < 85 else "molto umido" if perc < 95 else "saturo")
    return {"pioggia_7g_mm": api7, "pioggia_15g_mm": api15, "percentile": perc, "stato": stato,
            "fino_al": righe[-1]["data"] if righe else None}


def soglie_superate(tr):
    out = []
    for chiave, l in tr["livelli"].items():
        if not l or l["livello"] is None:
            continue
        if l["soglia2"] is not None and l["livello"] >= l["soglia2"]:
            out.append(f"{l['nome']}: {fmt(l['livello'], 2, ' m')}, oltre la SECONDA soglia ({fmt(l['soglia2'], 2, ' m')})")
        elif l["soglia1"] is not None and l["livello"] >= l["soglia1"]:
            out.append(f"{l['nome']}: {fmt(l['livello'], 2, ' m')}, oltre la prima soglia ({fmt(l['soglia1'], 2, ' m')})")
    return out


# ---------------------------------------------------------------- previsioni

def previsione_modello():
    """Pioggia giornaliera prevista dal modello meteo (Open-Meteo, gratuito) su alcuni punti dell'alta valle."""
    params = {
        "latitude": ",".join(str(p[1]) for p in PUNTI_PREVISIONE),
        "longitude": ",".join(str(p[2]) for p in PUNTI_PREVISIONE),
        "daily": "precipitation_sum",
        "timezone": "Europe/Rome",
        "forecast_days": 3,
    }
    r = requests.get(OPEN_METEO, params=params, timeout=60)
    r.raise_for_status()
    risposta = r.json()
    if isinstance(risposta, dict):
        risposta = [risposta]
    giorni = {}
    for (nome, _, _), punto in zip(PUNTI_PREVISIONE, risposta):
        daily = punto.get("daily") or {}
        for d, mm in zip(daily.get("time", []), daily.get("precipitation_sum", [])):
            if mm is not None:
                giorni.setdefault(d, {})[nome] = mm
    out = []
    for d in sorted(giorni):
        valori = list(giorni[d].values())
        out.append({"data": d, "media_mm": round(sum(valori) / len(valori), 1),
                    "max_mm": round(max(valori), 1), "punti": giorni[d]})
    return {"fonte": "Open-Meteo (modelli meteorologici), media di Gamberame, Vaiano, Cantagallo e Vernio",
            "giorni": out}


def _intervallo(testo):
    numeri = [float(x) for x in re.findall(r"\d+(?:[.,]\d+)?", testo.replace(",", "."))]
    return (min(numeri), max(numeri)) if numeri else None


def _cerca_zona(frase, zona):
    """Valori medi e massimi per la zona dentro una frase del bollettino."""
    schemi = [
        # "...intorno a 60-80 mm sulle aree V, S2 e B con massimi fino a 100-150 mm"
        r"(?:medi\s+)?intorno\s+a\s+([\d\-–\s]+)\s*mm\s+(?:sull[ae]|su)\s+(?:aree|area|zone|zona)\s+([A-Z0-9,\se]+?)\s+con\s+massimi\s+fino\s+a\s+([\d\-–\s]+)\s*mm",
        # "(aree V, S2, B) medi intorno a 30-40 mm e massimi fino a 80-100 mm"
        r"\((?:aree|area|zone|zona)\s+([A-Z0-9,\se]+?)\)\s+medi\s+intorno\s+a\s+([\d\-–\s]+)\s*mm\s+e\s+massimi\s+fino\s+a\s+([\d\-–\s]+)\s*mm",
    ]
    for i, schema in enumerate(schemi):
        for m in re.finditer(schema, frase):
            if i == 0:
                medi, aree, massimi = m.group(1), m.group(2), m.group(3)
            else:
                aree, medi, massimi = m.group(1), m.group(2), m.group(3)
            if zona in re.split(r"[,\s]+|\be\b", aree):
                return {"medi_mm": _intervallo(medi), "massimi_mm": _intervallo(massimi)}
    return None


def bollettino_cfr(zona="B"):
    """Legge il bollettino di vigilanza meteo del Centro Funzionale e cerca i cumulati per la zona."""
    r = requests.get(CFR_VIGILANZA, timeout=60)
    r.raise_for_status()
    testo = r.content.decode("utf-8", errors="replace")
    if "Emissione" not in testo:
        testo = r.content.decode("latin-1")
    testo = re.sub(r"<[^>]+>", " ", testo)
    testo = re.sub(r"&nbsp;|\s+", " ", testo)
    m = re.search(r"Emissione di \w+,? (\d{1,2}) (\w+) (\d{4}), ore (\d{1,2})[.:](\d{2})", testo)
    if not m:
        return None
    emissione = datetime(int(m.group(3)), MESI.get(m.group(2).lower(), 1), int(m.group(1)),
                         int(m.group(4)), int(m.group(5)), tzinfo=ROMA)
    pioggia = re.search(r"PIOGGIA:(.*?)(?:TEMPORALI:|VENTO:|NEVE:|$)", testo)
    if not pioggia:
        return None
    paragrafo = pioggia.group(1)
    out = {"emissione": emissione.isoformat(timespec="minutes"), "zona": zona, "giorni": []}
    # Il bollettino parla di "Oggi" e "Domani": separa le due parti
    parti = re.split(r"(?=\b(?:Oggi|Domani|Dopodomani)\b)", paragrafo)
    for parte in parti:
        quando = re.match(r"(Oggi|Domani|Dopodomani)", parte)
        if not quando:
            continue
        giorno = emissione.date() + timedelta(days={"Oggi": 0, "Domani": 1, "Dopodomani": 2}[quando.group(1)])
        valori = _cerca_zona(parte, zona)
        if valori:
            out["giorni"].append({"data": giorno.isoformat(), **valori, "testo": parte.strip()[:600]})
    return out


CFR_CRITICITA = "https://www.cfr.toscana.it/index.php?IDS=2&IDSS=76"
COLORI = ["VERDE", "GIALLO", "ARANCIONE", "ROSSO"]


def _data_testo(testo):
    m = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", testo)
    return datetime(int(m.group(3)), MESI.get(m.group(2).lower(), 1), int(m.group(1))).date() if m else None


def allerta_ufficiale(zona="B"):
    """Codice colore ufficiale della Regione per la zona, oggi e domani (bollettino di valutazione delle criticità)."""
    r = requests.get(CFR_CRITICITA, timeout=60)
    r.raise_for_status()
    html = r.content.decode("utf-8", errors="replace")
    em = re.search(r"Emissione di\s*(?:<b>)?\s*\w+,?\s*(\d{1,2}\s+\w+\s+\d{4})\s*(?:</b>)?,\s*ore\s*(?:<b>)?\s*(\d{1,2})[.:](\d{2})", html)
    if not em:
        return None
    d_em = _data_testo(em.group(1))
    emissione = datetime(d_em.year, d_em.month, d_em.day, int(em.group(2)), int(em.group(3)), tzinfo=ROMA)
    giorni = {}
    for riga in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        celle = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", riga, re.S)]
        if len(celle) < 4 or celle[-1].upper() not in COLORI:
            continue
        zone = [z.strip() for z in celle[0].split(",")]
        if zona not in zone:
            continue
        rischio, periodo, colore = celle[1], celle[2], celle[-1].upper()
        date = [d for d in (_data_testo(x) for x in re.split(r"(?=\b(?:dalle|alle|fino)\b)", periodo)) if d]
        if not date:
            continue
        inizio = date[0] if periodo.lower().startswith("dalle") else emissione.date()
        fine = date[-1]
        g = inizio
        while g <= fine:
            k = g.isoformat()
            voce = giorni.setdefault(k, {"colore": "VERDE", "rischi": []})
            if COLORI.index(colore) > COLORI.index(voce["colore"]):
                voce["colore"] = colore
            voce["rischi"].append({"rischio": rischio.capitalize(),
                                   "colore": colore, "periodo": periodo})
            g += timedelta(days=1)
    return {"zona": zona, "emissione": emissione.isoformat(timespec="minutes"), "giorni": giorni,
            "fonte": CFR_CRITICITA}


def pioggia_prossime_ore(ore=3):
    """Pioggia prevista dal modello meteo nelle prossime ore sui punti dell'alta valle (non è il radar)."""
    params = {
        "latitude": ",".join(str(p[1]) for p in PUNTI_PREVISIONE),
        "longitude": ",".join(str(p[2]) for p in PUNTI_PREVISIONE),
        "minutely_15": "precipitation", "forecast_minutely_15": ore * 4,
        "timezone": "Europe/Rome",
    }
    r = requests.get(OPEN_METEO, params=params, timeout=60)
    r.raise_for_status()
    risposta = r.json()
    if isinstance(risposta, dict):
        risposta = [risposta]
    somme = {}
    for (nome, _, _), punto in zip(PUNTI_PREVISIONE, risposta):
        valori = [v for v in (punto.get("minutely_15") or {}).get("precipitation", []) if v is not None]
        somme[nome] = round(sum(valori), 1)
    if not somme:
        return None
    return {"ore": ore, "media_mm": round(sum(somme.values()) / len(somme), 1), "max_mm": max(somme.values()),
            "punti": somme, "fonte": "Open-Meteo, modello meteo a 15 minuti (non radar)"}


def simula(storico_righe, pioggia, livello, pioggia_prima, pioggia_15g=None, k=6):
    """Stessa logica del simulatore della pagina: i k giorni dal 1998 più simili
    per pioggia, livello di partenza, pioggia del giorno prima e terreno (pioggia dei 15 giorni prima)."""
    pool = []
    cum = [0.0]
    for r in storico_righe:
        cum.append(cum[-1] + (r["pioggia_gamberame_mm"] or 0))
    for i in range(1, len(storico_righe)):
        prima, oggi = storico_righe[i - 1], storico_righe[i]
        if oggi["pioggia_gamberame_mm"] is None or prima["livello_san_piero_m"] is None or oggi["livello_san_piero_m"] is None:
            continue
        api = cum[i] - cum[max(0, i - 15)]
        pool.append((oggi["pioggia_gamberame_mm"], prima["pioggia_gamberame_mm"] or 0,
                     prima["livello_san_piero_m"], oggi["livello_san_piero_m"], api))
    if not pool:
        return None
    def distanza(r):
        d = ((r[0] - pioggia) / 15) ** 2 + ((r[2] - livello) / 0.25) ** 2 + ((r[1] - pioggia_prima) / 30) ** 2
        if pioggia_15g is not None:
            d += ((r[4] - pioggia_15g) / 40) ** 2
        return d
    pool.sort(key=distanza)
    vicini = sorted(r[3] for r in pool[:k])
    stima = (vicini[k // 2 - 1] + vicini[k // 2]) / 2
    quota = max(pioggia / SOGLIA["pioggia24h"], (pioggia + pioggia_prima) / SOGLIA["pioggia48h"], quota_livello(stima, SOGLIE_LIVELLO["san_piero"]))
    indice = round(100 * quota)
    return {"livello_stimato_m": round(stima, 2), "min_m": vicini[0], "max_m": vicini[-1],
            "indice": indice, "livello": livello_allerta(indice)}


def previsione_domani(adesso, tr, storico_righe, vecchia, api15=None):
    """Raccoglie le previsioni per domani e lancia il simulatore. Non blocca mai l'aggiornamento."""
    domani = (adesso.date() + timedelta(days=1)).isoformat()
    out = {"data": domani, "modello": None, "cfr": None}
    try:
        mod = previsione_modello()
        out["modello"] = next((g for g in mod["giorni"] if g["data"] == domani), None)
        out["modello_fonte"] = mod["fonte"]
    except (requests.RequestException, ValueError) as e:
        print("Previsione modello non disponibile:", e, file=sys.stderr)
        out["modello"] = (vecchia or {}).get("modello") if (vecchia or {}).get("data") == domani else None
    try:
        cfr = bollettino_cfr()
        if cfr:
            out["cfr_emissione"] = cfr["emissione"]
            out["cfr"] = next((g for g in cfr["giorni"] if g["data"] == domani), None)
    except requests.RequestException as e:
        print("Bollettino CFR non disponibile:", e, file=sys.stderr)
        out["cfr"] = (vecchia or {}).get("cfr") if (vecchia or {}).get("data") == domani else None

    # Valore usato: il massimo dei "medi" del CFR se c'è, altrimenti la media del modello
    if out["cfr"] and out["cfr"].get("medi_mm"):
        out["pioggia_usata_mm"], out["pioggia_usata_fonte"] = out["cfr"]["medi_mm"][1], "bollettino CFR, cumulato medio zona B"
    elif out["modello"]:
        out["pioggia_usata_mm"], out["pioggia_usata_fonte"] = out["modello"]["media_mm"], "modello meteo, media alta valle"
    else:
        out["pioggia_usata_mm"] = None

    sp = (tr["livelli"].get("san_piero") or {}).get("livello")
    p24 = (tr["piogge"].get("gamberame") or {}).get("mm_24h") or 0
    if out["pioggia_usata_mm"] is not None and sp is not None and storico_righe:
        out["simulazione"] = simula(storico_righe, out["pioggia_usata_mm"], sp, p24, api15)
    return out


def telegram(testo):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": testo}, timeout=30)
    except requests.RequestException as e:
        print("Telegram non raggiungibile:", e, file=sys.stderr)


def fmt(v, dec=2, unita=""):
    return "n.d." if v is None else f"{v:.{dec}f}".replace(".", ",") + unita


def main():
    adesso = datetime.now(ROMA)
    vecchio = json.loads(DATA_FILE.read_text(encoding="utf-8")) if DATA_FILE.exists() else {}

    # Archivio giornaliero: una volta al giorno (o se manca), in un file a parte
    aggiornato_storico = vecchio.get("storico_aggiornato")
    n_vecchio = vecchio.get("storico_giorni", 0)
    storico_agg = aggiornato_storico
    storico_righe = None
    if not STORICO_FILE.exists() or not aggiornato_storico or aggiornato_storico[:10] != adesso.date().isoformat():
        try:
            righe = indice_giornaliero(storico())
            if len(righe) < 0.9 * n_vecchio:
                raise requests.RequestException("archivio incompleto, tengo quello precedente")
            compatto = {"campi": CAMPI_STORICO, "righe": [[r.get(k) for k in CAMPI_STORICO] for r in righe]}
            storico_righe = righe
            STORICO_FILE.write_text(json.dumps(compatto, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            storico_agg, n_vecchio = adesso.isoformat(timespec="minutes"), len(righe)
        except requests.RequestException as e:
            print("Archivio non scaricato:", e, file=sys.stderr)

    if storico_righe is None and STORICO_FILE.exists():
        c = json.loads(STORICO_FILE.read_text(encoding="utf-8"))
        storico_righe = [dict(zip(c["campi"], r)) for r in c["righe"]]

    # Tempo reale
    tr = tempo_reale(adesso)
    serie, analisi = {}, {}
    for chiave in IDRO_SERIE:
        try:
            serie[chiave] = serie_dettaglio(chiave, adesso)
            a = analisi_serie(serie[chiave])
            if a:
                analisi[chiave] = a
                if tr["livelli"].get(chiave):
                    tr["livelli"][chiave].update(a)
        except requests.RequestException as e:
            print(f"Serie {chiave} non scaricata:", e, file=sys.stderr)
    stima = indice_tempo_reale(tr, analisi)

    ritardi = ritardi_osservati(serie, vecchio.get("ritardi", []), adesso)
    ritardo = stima_ritardo(ritardi)
    colmo = previsione_colmo(analisi, ritardo, adesso)
    arno = tr["livelli"].get("signa") or {}
    rigurgito = bool(arno.get("livello") is not None and not arno.get("vecchio") and arno["livello"] >= ARNO_RIGURGITO_M)

    lettura = {
        "ora": adesso.isoformat(timespec="minutes"),
        **{k: (tr["livelli"].get(k) or {}).get("livello") for k in IDRO},
        "pioggia_gamberame_1h": (tr["piogge"].get("gamberame") or {}).get("mm_1h"),
        "pioggia_campi_1h": (tr["piogge"].get("campi") or {}).get("mm_1h"),
        "indice": stima["indice"],
    }
    registro = [x for x in vecchio.get("registro", []) if x.get("ora") != lettura["ora"]] + [lettura]
    registro = registro[-LETTURE_REGISTRO:]

    suolo = terreno(storico_righe, adesso)
    previsione = previsione_domani(adesso, tr, storico_righe, vecchio.get("previsione"),
                                   (suolo or {}).get("pioggia_15g_mm"))
    try:
        prossime = pioggia_prossime_ore()
    except (requests.RequestException, ValueError) as e:
        print("Pioggia prossime ore non disponibile:", e, file=sys.stderr)
        prossime = None
    try:
        ufficiale = allerta_ufficiale()
    except requests.RequestException as e:
        print("Allerta ufficiale non disponibile:", e, file=sys.stderr)
        ufficiale = vecchio.get("allerta_ufficiale")

    dati = {
        "aggiornato": adesso.isoformat(timespec="minutes"),
        "fonte": "Servizio Idrologico Regionale Toscana - sir.toscana.it (dati in tempo reale non validati)",
        "soglie_indice": {**SOGLIA, "livelli": SOGLIE_LIVELLO, "velocita_cm_ora": SOGLIE_VELOCITA,
                          "tempo_ritorno_anni": SOGLIE_TEMPO_RITORNO, "arno_rigurgito_m": ARNO_RIGURGITO_M},
        "allerta_ufficiale": ufficiale,
        "tempo_reale": tr,
        "stima": stima,
        "colmo": colmo,
        "ritardo_gamberame_san_piero": ritardo,
        "ritardi": ritardi,
        "rigurgito_arno": rigurgito,
        "prossime_ore": prossime,
        "terreno": suolo,
        "serie": {k: serie_quarti_ora(v) for k, v in serie.items()},
        "registro": registro,
        "storico_aggiornato": storico_agg,
        "storico_giorni": n_vecchio,
        "previsione": previsione,
    }
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(dati, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    # ---- Avvisi Telegram, solo quando cambia qualcosa di importante
    motivi = []
    prima = (vecchio.get("stima") or {}).get("livello")
    if stima["livello"] != prima:
        motivi.append("cambia l'allerta stimata")
    superate = soglie_superate(tr)
    if superate and set(superate) != set(vecchio.get("_soglie_superate", [])):
        motivi.append("soglia superata")
    oggi_k, domani_k = adesso.date().isoformat(), (adesso.date() + timedelta(days=1)).isoformat()
    def colore_uff(dati_uff, k):
        return (((dati_uff or {}).get("giorni") or {}).get(k) or {}).get("colore", "VERDE")
    uff_prima = vecchio.get("allerta_ufficiale")
    if ufficiale and (colore_uff(ufficiale, oggi_k), colore_uff(ufficiale, domani_k)) != (colore_uff(uff_prima, oggi_k), colore_uff(uff_prima, domani_k)):
        motivi.append("nuova allerta ufficiale")
    if colmo and colmo["stato"] in ("atteso", "in_salita") and (vecchio.get("colmo") or {}).get("stato") != colmo["stato"]:
        motivi.append("piena in arrivo")
    sim = previsione.get("simulazione") or {}
    sim_prima = ((vecchio.get("previsione") or {}).get("simulazione") or {}).get("livello")
    if sim.get("livello") in ("Arancione", "Rosso") and sim.get("livello") != sim_prima:
        motivi.append("previsione per domani")
    if stima["dati_non_aggiornati"] and not (vecchio.get("stima") or {}).get("dati_non_aggiornati"):
        motivi.append("stazioni senza dati")
    dati["_soglie_superate"] = superate
    DATA_FILE.write_text(json.dumps(dati, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    if motivi:
        sp, ga = tr["livelli"].get("san_piero") or {}, tr["livelli"].get("gamberame") or {}
        pg = tr["piogge"].get("gamberame") or {}
        def vel(l):
            v = l.get("cm_ora_1h")
            return "" if v is None else f" ({'+' if v > 0 else ''}{v} cm/h)"
        msg = [f"Bisenzio {adesso:%d/%m %H:%M} - allerta stimata: {stima['livello']} (indice {stima['indice']})",
               f"Causa: {stima['causa']}"]
        if ufficiale:
            msg.append(f"Allerta ufficiale zona B: oggi {colore_uff(ufficiale, oggi_k).lower()}, domani {colore_uff(ufficiale, domani_k).lower()}")
        msg.append(f"S. Piero a Ponti {fmt(sp.get('livello'), 2, ' m')}{vel(sp)} | Gamberame {fmt(ga.get('livello'), 2, ' m')}{vel(ga)}")
        msg.append(f"Pioggia Gamberame: 1h {fmt(pg.get('mm_1h'), 1, ' mm')}, 24h {fmt(pg.get('mm_24h'), 1, ' mm')}")
        if colmo:
            msg.append(colmo["testo"])
        if rigurgito:
            msg.append(f"Arno alto a Signa ({fmt(arno.get('livello'), 2, ' m')}): il Bisenzio scarica piu' lentamente.")
        msg += superate
        if stima["dati_non_aggiornati"]:
            msg.append("Senza dati recenti: " + ", ".join(stima["dati_non_aggiornati"]))
        if sim:
            msg.append(f"Domani ({previsione['data'][8:10]}/{previsione['data'][5:7]}): pioggia prevista {fmt(previsione['pioggia_usata_mm'], 0, ' mm')} "
                       f"-> S. Piero stimato {fmt(sim['livello_stimato_m'], 2, ' m')}, allerta {sim['livello']}")
        msg.append("Stima indicativa: seguire gli avvisi del CFR Toscana e del Comune.")
        telegram("\n".join(msg))

    print(json.dumps({"stima": stima, "colmo": colmo, "motivi": motivi}, ensure_ascii=False))


if __name__ == "__main__":
    main()
