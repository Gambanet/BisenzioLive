"""
Allerta Bisenzio - aggiornamento dati.

Scarica dal Servizio Idrologico Regionale Toscana (sir.toscana.it):
  - i valori in tempo reale di livelli e piogge (ogni esecuzione);
  - l'archivio giornaliero dal 2022 (una volta al giorno).
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
ORE_REGISTRO = 24 * 7  # quante letture orarie tenere

# Stazioni
IDRO = {
    "san_piero": {"id": "TOS01004791", "nome": "S. Piero a Ponti"},
    "gamberame": {"id": "TOS01004779", "nome": "Gamberame"},
    "prato": {"id": "TOS01004782", "nome": "Prato"},
}
PLUVIO = {
    "gamberame": {"id": "TOS01004779", "nome": "Gamberame (Vaiano)"},
    "campi": {"id": "TOS01001225", "nome": "Case Passerini (Campi Bisenzio)"},
}
# Soglie dell'indicatore (100 = rosso)
SOGLIA = {"pioggia24h": 100.0, "pioggia48h": 140.0}
# Livelli del Bisenzio [giallo, arancione, rosso] in metri.
# Giallo = prima soglia ufficiale SIR, rosso = seconda soglia, arancione a metà.
SOGLIE_LIVELLO = {"san_piero": [3.5, 4.5, 5.5], "gamberame": [1.8, 2.3, 2.8]}


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
SESSION.headers["User-Agent"] = "allerta-bisenzio/1.0 (uso personale, una richiesta all'ora)"


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


def tempo_reale(adesso):
    idro_html = scarica("/monitoraggio/stazioni.php?type=idro")
    pluvio_html = scarica("/monitoraggio/stazioni.php?type=pluvio")
    out = {"livelli": {}, "piogge": {}}
    for chiave, st in IDRO.items():
        r = riga_js(idro_html, st["id"])
        if not r or len(r) < 14:
            out["livelli"][chiave] = None
            continue
        out["livelli"][chiave] = {
            "nome": st["nome"],
            "soglia1": num(r[7]),
            "soglia2": num(r[6]),
            "livello": num(r[8]),
            "variazioni": [num(r[10]), num(r[11]), num(r[12])],
            "ora": data_ora(r[13], adesso),
        }
    for chiave, st in PLUVIO.items():
        r = riga_js(pluvio_html, st["id"])
        if not r or len(r) < 12:
            out["piogge"][chiave] = None
            continue
        out["piogge"][chiave] = {
            "nome": st["nome"],
            "mm_15min": num(r[4]), "mm_1h": num(r[5]), "mm_3h": num(r[6]),
            "mm_6h": num(r[7]), "mm_12h": num(r[8]), "mm_24h": num(r[9]),
            "mm_36h": num(r[10]),
            "ora": data_ora(r[11], adesso),
        }
    return out


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


def indice_tempo_reale(tr):
    """Stima con i valori istantanei: pioggia 24h e 36h (al posto delle 48h), livelli attuali."""
    misure = []
    p = tr["piogge"]
    p24 = max([x["mm_24h"] for x in p.values() if x and x["mm_24h"] is not None] or [0])
    misure.append(("Pioggia 24h", p24 / SOGLIA["pioggia24h"]))
    g = p.get("gamberame")
    if g and g["mm_36h"] is not None:
        misure.append(("Pioggia 36h", g["mm_36h"] / SOGLIA["pioggia48h"]))
    for chiave in ("san_piero", "gamberame"):
        l = tr["livelli"].get(chiave)
        if l and l["livello"] is not None:
            misure.append((f"Livello {IDRO[chiave]['nome']}", quota_livello(l["livello"], SOGLIE_LIVELLO[chiave])))
    causa, quota = max(misure, key=lambda x: x[1])
    indice = round(100 * quota)
    return {"indice": indice, "livello": livello_allerta(indice), "causa": causa}


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


def simula(storico_righe, pioggia, livello, pioggia_prima, k=6):
    """Stessa logica del simulatore della pagina: i k giorni dal 1998 più simili."""
    pool = []
    for prima, oggi in zip(storico_righe, storico_righe[1:]):
        if oggi["pioggia_gamberame_mm"] is None or prima["livello_san_piero_m"] is None or oggi["livello_san_piero_m"] is None:
            continue
        pool.append((oggi["pioggia_gamberame_mm"], prima["pioggia_gamberame_mm"] or 0,
                     prima["livello_san_piero_m"], oggi["livello_san_piero_m"]))
    if not pool:
        return None
    pool.sort(key=lambda r: ((r[0] - pioggia) / 15) ** 2 + ((r[2] - livello) / 0.25) ** 2 + ((r[1] - pioggia_prima) / 30) ** 2)
    vicini = sorted(r[3] for r in pool[:k])
    stima = (vicini[k // 2 - 1] + vicini[k // 2]) / 2
    quota = max(pioggia / SOGLIA["pioggia24h"], (pioggia + pioggia_prima) / SOGLIA["pioggia48h"], quota_livello(stima, SOGLIE_LIVELLO["san_piero"]))
    indice = round(100 * quota)
    return {"livello_stimato_m": round(stima, 2), "min_m": vicini[0], "max_m": vicini[-1],
            "indice": indice, "livello": livello_allerta(indice)}


def previsione_domani(adesso, tr, storico_righe, vecchia):
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
        out["simulazione"] = simula(storico_righe, out["pioggia_usata_mm"], sp, p24)
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
    stima = indice_tempo_reale(tr)
    lettura = {
        "ora": adesso.isoformat(timespec="minutes"),
        "san_piero": (tr["livelli"].get("san_piero") or {}).get("livello"),
        "gamberame": (tr["livelli"].get("gamberame") or {}).get("livello"),
        "prato": (tr["livelli"].get("prato") or {}).get("livello"),
        "pioggia_gamberame_1h": (tr["piogge"].get("gamberame") or {}).get("mm_1h"),
        "pioggia_campi_1h": (tr["piogge"].get("campi") or {}).get("mm_1h"),
        "indice": stima["indice"],
    }
    registro = [x for x in vecchio.get("registro", []) if x.get("ora") != lettura["ora"]] + [lettura]
    registro = registro[-ORE_REGISTRO:]

    previsione = previsione_domani(adesso, tr, storico_righe, vecchio.get("previsione"))

    dati = {
        "aggiornato": adesso.isoformat(timespec="minutes"),
        "fonte": "Servizio Idrologico Regionale Toscana - sir.toscana.it (dati in tempo reale non validati)",
        "soglie_indice": {**SOGLIA, "livelli": SOGLIE_LIVELLO},
        "tempo_reale": tr,
        "stima": stima,
        "registro": registro,
        "storico_aggiornato": storico_agg,
        "storico_giorni": n_vecchio,
        "previsione": previsione,
    }
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(dati, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    # Avviso Telegram: cambia il livello di allerta o ci sono soglie superate
    prima = (vecchio.get("stima") or {}).get("livello")
    superate = soglie_superate(tr)
    sim = previsione.get("simulazione") or {}
    sim_prima = ((vecchio.get("previsione") or {}).get("simulazione") or {}).get("livello")
    cambia_domani = sim.get("livello") in ("Arancione", "Rosso") and sim.get("livello") != sim_prima
    if stima["livello"] != prima or superate or cambia_domani:
        sp, ga = tr["livelli"].get("san_piero") or {}, tr["livelli"].get("gamberame") or {}
        pg = tr["piogge"].get("gamberame") or {}
        righe_msg = [
            f"Bisenzio {adesso:%d/%m %H:%M} - allerta stimata: {stima['livello']} (indice {stima['indice']})",
            f"S. Piero a Ponti {fmt(sp.get('livello'), 2, ' m')} | Gamberame {fmt(ga.get('livello'), 2, ' m')}",
            f"Pioggia Gamberame: 1h {fmt(pg.get('mm_1h'), 1, ' mm')}, 24h {fmt(pg.get('mm_24h'), 1, ' mm')}",
        ] + superate
        if sim:
            righe_msg.append(f"Domani ({previsione['data'][8:10]}/{previsione['data'][5:7]}): pioggia prevista {fmt(previsione['pioggia_usata_mm'], 0, ' mm')} "
                             f"({previsione['pioggia_usata_fonte']}) -> S. Piero stimato {fmt(sim['livello_stimato_m'], 2, ' m')}, allerta {sim['livello']}")
        righe_msg.append("Stima indicativa: seguire gli avvisi del CFR Toscana e del Comune.")
        telegram("\n".join(righe_msg))

    print(json.dumps({"stima": stima, "lettura": lettura}, ensure_ascii=False))


if __name__ == "__main__":
    main()
