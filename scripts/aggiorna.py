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
    "san_piero": {"id": "TOS01004791", "nome": "S. Piero a Ponti", "soglia_indice": 3.5},
    "gamberame": {"id": "TOS01004779", "nome": "Gamberame", "soglia_indice": 1.5},
    "prato": {"id": "TOS01004782", "nome": "Prato", "soglia_indice": None},
}
PLUVIO = {
    "gamberame": {"id": "TOS01004779", "nome": "Gamberame (Vaiano)"},
    "campi": {"id": "TOS01001225", "nome": "Case Passerini (Campi Bisenzio)"},
}
# Soglie dell'indicatore (100 = rosso)
SOGLIA = {"pioggia24h": 100.0, "pioggia48h": 140.0, "san_piero": 3.5, "gamberame": 1.5}

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
            misure.append(("Livello S. Piero", r["livello_san_piero_m"] / SOGLIA["san_piero"]))
        if r["livello_gamberame_m"] is not None:
            misure.append(("Livello Gamberame", r["livello_gamberame_m"] / SOGLIA["gamberame"]))
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
            misure.append((f"Livello {IDRO[chiave]['nome']}", l["livello"] / SOGLIA[chiave]))
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
    if not STORICO_FILE.exists() or not aggiornato_storico or aggiornato_storico[:10] != adesso.date().isoformat():
        try:
            righe = indice_giornaliero(storico())
            if len(righe) < 0.9 * n_vecchio:
                raise requests.RequestException("archivio incompleto, tengo quello precedente")
            compatto = {"campi": CAMPI_STORICO, "righe": [[r.get(k) for k in CAMPI_STORICO] for r in righe]}
            STORICO_FILE.write_text(json.dumps(compatto, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            storico_agg, n_vecchio = adesso.isoformat(timespec="minutes"), len(righe)
        except requests.RequestException as e:
            print("Archivio non scaricato:", e, file=sys.stderr)

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

    dati = {
        "aggiornato": adesso.isoformat(timespec="minutes"),
        "fonte": "Servizio Idrologico Regionale Toscana - sir.toscana.it (dati in tempo reale non validati)",
        "soglie_indice": SOGLIA,
        "tempo_reale": tr,
        "stima": stima,
        "registro": registro,
        "storico_aggiornato": storico_agg,
        "storico_giorni": n_vecchio,
    }
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(dati, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    # Avviso Telegram: cambia il livello di allerta o ci sono soglie superate
    prima = (vecchio.get("stima") or {}).get("livello")
    superate = soglie_superate(tr)
    if stima["livello"] != prima or superate:
        sp, ga = tr["livelli"].get("san_piero") or {}, tr["livelli"].get("gamberame") or {}
        pg = tr["piogge"].get("gamberame") or {}
        righe_msg = [
            f"Bisenzio {adesso:%d/%m %H:%M} - allerta stimata: {stima['livello']} (indice {stima['indice']})",
            f"S. Piero a Ponti {fmt(sp.get('livello'), 2, ' m')} | Gamberame {fmt(ga.get('livello'), 2, ' m')}",
            f"Pioggia Gamberame: 1h {fmt(pg.get('mm_1h'), 1, ' mm')}, 24h {fmt(pg.get('mm_24h'), 1, ' mm')}",
        ] + superate + ["Stima indicativa: seguire gli avvisi del CFR Toscana e del Comune."]
        telegram("\n".join(righe_msg))

    print(json.dumps({"stima": stima, "lettura": lettura}, ensure_ascii=False))


if __name__ == "__main__":
    main()
