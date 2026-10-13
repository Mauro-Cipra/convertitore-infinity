import streamlit as st
import pdfplumber
import re
import csv
import io
from pdf2image import convert_from_bytes
import pytesseract
from decimal import Decimal, ROUND_HALF_UP

# =====================================================================
# FUNZIONI PER IL FORMATO "FATTURA ELETTRONICA SDI"
# (PDF di cortesia con "Tipo dato: DESCR. / Rif. testo: ..." sotto le righe)
# =====================================================================
IMP = r"\d{1,3}(?:\.\d{3})*,\d{2}"
# Riga articolo della fattura SDI: Descrizione  Quantità  Prezzo  %IVA (22,00 o N1…)  Totale
RE_RIGA_ARTICOLO = re.compile(rf"^(.*?)\s+({IMP})\s+(-?{IMP})\s+(\d{{1,2}},\d{{2}}|N\d(?:\.\d)?)\s+(-?{IMP})$")
RE_TARGA_SDI = re.compile(r"Rif\. testo:\s*Targa\s+([A-Z0-9]{5,8})\b", re.I)
RE_TELAIO = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b")

def dec(s):
    return Decimal(s.replace(".", "").replace(",", "."))

def cent(x):
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def numero_data_sdi(testo):
    """Riga 'TD01 fattura 20260001151 09-10-2026' (o '1/276', '825') della fattura elettronica."""
    m = re.search(r"\bTD\d{2}\b\D*?([\w/\-]*\d)\s+(\d{2})[-/](\d{2})[-/](\d{4})", testo)
    if not m:
        return None, None
    codice, g, mese, anno = m.groups()
    numero = re.findall(r"\d+", codice)[-1]           # '1/276' -> '276'
    if numero.startswith(anno) and len(numero) > 4:   # '20260001151' -> '0001151'
        numero = numero[4:]
    return (numero.lstrip("0") or "0"), f"{g}/{mese}/{anno}"

def pratiche_sdi(testo):
    """
    Restituisce (pratiche, bollo_virtuale, avvisi).
    Regola del formato SDI: i 'Rif. testo' (Prat., Targa…) sono stampati SOTTO la riga
    articolo a cui sono agganciati. Quindi la riga articolo subito prima di 'Prat.' è la
    prima riga della NUOVA pratica; le righe successive appartengono a quella pratica
    fino alla riga che precede la 'Prat.' seguente.
    """
    linee = [l.strip() for l in testo.splitlines()]
    pratiche, avvisi = [], []
    bollo = Decimal("0")
    ultima_riga = None          # ultima riga articolo letta (candidata a 'testata' della prossima pratica)
    attesa_telaio = False
    for i, l in enumerate(linee):
        if l.upper().startswith("RIEPILOGHI IVA"):
            break
        if "Rif. testo:" in l and re.search(r"Rif\. testo:\s*Prat\.", l, re.I):
            nuova = {"targa": "", "telaio": "", "imponibile": [], "esente": []}
            # la riga articolo appena letta appartiene a questa pratica, non alla precedente
            if ultima_riga is not None and pratiche:
                pratiche[-1]["_righe"].remove(ultima_riga)
            nuova["_righe"] = [ultima_riga] if ultima_riga is not None else []
            pratiche.append(nuova)
            ultima_riga = None
            continue
        m = RE_TARGA_SDI.search(l)
        if m and pratiche:
            pratiche[-1]["targa"] = m.group(1).upper()
            t = RE_TELAIO.search(l[m.end():])
            if t:
                pratiche[-1]["telaio"] = t.group(1)
            else:
                attesa_telaio = True
            continue
        if attesa_telaio and pratiche:
            t = RE_TELAIO.search(l)
            if t:
                pratiche[-1]["telaio"] = t.group(1)
                attesa_telaio = False
                continue
        m = RE_RIGA_ARTICOLO.match(l)
        if m:
            desc, _q, _p, aliq, tot = m.groups()
            riga = (desc, aliq, dec(tot))
            if "BOLLO ASSOLTO" in desc.upper():        # bollo virtuale MEF della fattura: riga a parte
                bollo += dec(tot)
                ultima_riga = None
                continue
            if pratiche:
                pratiche[-1]["_righe"].append(riga)
            elif ultima_riga is not None:
                avvisi.append(f"Riga senza pratica ignorata: {ultima_riga[0]}")
            ultima_riga = riga
    for p in pratiche:
        for desc, aliq, tot in p.pop("_righe"):
            (p["esente"] if aliq.startswith("N") else p["imponibile"]).append(tot)
        p["esente"] = sum(p["esente"], Decimal("0"))
        p["imponibile"] = sum(p["imponibile"], Decimal("0"))
        if not p["targa"]:
            avvisi.append("Pratica senza targa trovata")
    return pratiche, bollo, avvisi

def riepilogo_sdi(testo):
    """
    Totali del 'RIEPILOGHI IVA E TOTALI' per la quadratura: {aliquota/natura: totale imponibile}.
    Gestisce sia il layout lungo (Di Lecce: ... 0,00 0,00 307,36 67,62) sia quello corto
    (Centro Servizi: 22,00 51,49 11,33 / N1 487,18 0): l'imponibile è il penultimo importo.
    """
    tot = {}
    for l in testo.splitlines():
        m = re.match(r"^(?:I \(esigibilit|Esigib).*?\s(N\d(?:\.\d)?|\d{1,2},\d{2})\s+(.*)$", l.strip())
        if m:
            valori = re.findall(r"-?\d{1,3}(?:\.\d{3})*,\d{2}\b|(?<![\d,])0(?![\d,])", m.group(2))
            if len(valori) >= 2:
                valore = dec(valori[-2]) if valori[-2] != "0" else Decimal("0")
                tot[m.group(1)] = tot.get(m.group(1), Decimal("0")) + valore
    return tot


# ---------------------------------------------------------------------
# CENTRO SERVIZI AUTOMOBILISTICI (P.IVA 02743500783)
# Layout: per ogni veicolo una riga "TARGA: XX000XX" seguita dalle spese N1;
# le righe al 22% (COMPETENZE, STAMPATI/ACCESSI) sono UNA volta sola in fondo
# per tutte le targhe e vanno ripartite in parti uguali fra i veicoli.
# ---------------------------------------------------------------------
RE_RIGA_CENTRO = re.compile(r"^(.*?)\s+(\d+,\d+)\s+(-?[\d.]*\d,\d+)\s+(\d{1,2},\d{2}|N\d(?:\.\d)?)\s+(-?[\d.]*\d,\d+)$")
RE_TARGA_CENTRO = re.compile(r"TARGA\s*:\s*([A-Z]{2}\s?\d{3}\s?[A-Z]{2}|[A-Z0-9]{5,8})\b")

def veicoli_centro_servizi(testo):
    veicoli, avvisi = [], []
    imponibile_comune = Decimal("0")
    for l in (x.strip() for x in testo.splitlines()):
        if l.upper().startswith("RIEPILOGHI IVA"):
            break
        t = RE_TARGA_CENTRO.search(l.upper())
        if t:
            veicoli.append({"targa": t.group(1).replace(" ", ""), "esente": Decimal("0")})
            continue
        m = RE_RIGA_CENTRO.match(l)
        if not m:
            continue
        desc, _q, _p, aliq, tot = m.groups()
        importo = dec(tot)
        if aliq.startswith("N"):
            if veicoli:
                veicoli[-1]["esente"] += importo
            else:
                avvisi.append(f"Spesa esente senza targa: {desc} {tot}")
        else:
            imponibile_comune += importo
    # ripartizione in parti uguali; l'ultimo veicolo assorbe i centesimi di arrotondamento
    if veicoli:
        tot_imp = cent(imponibile_comune)
        tot_iva = cent(imponibile_comune * Decimal("0.22"))
        quota_imp = cent(tot_imp / len(veicoli))
        quota_iva = cent(tot_iva / len(veicoli))
        for i, v in enumerate(veicoli):
            ultimo = i == len(veicoli) - 1
            v["imponibile"] = tot_imp - quota_imp * (len(veicoli) - 1) if ultimo else quota_imp
            v["iva"] = tot_iva - quota_iva * (len(veicoli) - 1) if ultimo else quota_iva
    return veicoli, avvisi

# ---------------------------------------------------------------------
# MSERVICE SRL (P.IVA 02000390761)
# Layout SDI: sotto OGNI riga articolo c'è il suo riferimento
# "Tipo dato: <n. pratica>" + "Rif. testo: TARGA - TELAIO".
# Si produce una riga CSV per ogni riga articolo, come nei CSV forniti da MSERVICE.
# ---------------------------------------------------------------------
RE_RIF_TARGA_TELAIO = re.compile(r"Rif\. testo:\s*([A-Z0-9]*)\s*-\s*([A-HJ-NPR-Z0-9]{17})\b")
RE_TIPO_DATO = re.compile(r"^Tipo dato:\s*(\d+)")

def pratiche_mservice(testo):
    """Restituisce (pratiche in ordine, bollo fattura, avvisi). Raggruppa per n. pratica,
    perché la stessa targa può comparire in due pratiche diverse."""
    pratiche, avvisi = {}, []
    bollo = Decimal("0")
    in_attesa, pratica_corrente = [], None
    for l in (x.strip() for x in testo.splitlines()):
        if l.upper().startswith("RIEPILOGHI IVA"):
            break
        m = RE_RIGA_ARTICOLO.match(l)
        if m:
            desc, _q, _p, aliq, tot = m.groups()
            if desc.strip().upper() == "BOLLO" and aliq.startswith("N"):
                bollo += dec(tot)          # bollo della fattura: riga a parte in fondo
            else:
                in_attesa.append((aliq, dec(tot)))
            continue
        m = RE_TIPO_DATO.match(l)
        if m:
            pratica_corrente = m.group(1)
            continue
        m = RE_RIF_TARGA_TELAIO.search(l)
        if m and in_attesa:
            chiave = pratica_corrente or f"{m.group(1)}-{m.group(2)}"
            p = pratiche.setdefault(chiave, {"targa": m.group(1), "telaio": m.group(2), "righe": []})
            p["righe"].extend(in_attesa)
            in_attesa = []
    if in_attesa:
        avvisi.append(f"{len(in_attesa)} righe senza targa/telaio ignorate")
    return list(pratiche.values()), bollo, avvisi


# ---------------------------------------------------------------------
# LETTORE UNIVERSALE per fornitori non configurati (fattura elettronica SDI)
# ---------------------------------------------------------------------
# Riga articolo: Descrizione  Quantità  Prezzo (anche 5 decimali)  %IVA o Natura  Totale
RE_RIGA_UNIV = re.compile(
    r"^(.*?)\s+(-?\d+(?:\.\d{3})*,\d+)\s+(-?\d+(?:\.\d{3})*,\d+)\s+(\d{1,2},\d{2}|N\d(?:\.\d)?)\s+(-?\d{1,3}(?:\.\d{3})*,\d+)$")
# Targa: auto/moto AA000AA, oppure dopo "Targa"/"TARGA:", oppure "TARGA - TELAIO" (stile MSERVICE)
RE_TARGA_UNIV = [
    re.compile(r"\bTARGA(?:TO)?\s*:?\s*([A-Z0-9]{5,8})\b"),
    re.compile(r"RIF\. TESTO:\s*([A-Z0-9]{5,8})\s*-\s*[A-HJ-NPR-Z0-9]{17}\b"),
    re.compile(r"\b([A-Z]{2}\d{3}[A-Z]{2})\b"),
]
RE_VIN_UNIV = re.compile(r"\b(?=[A-HJ-NPR-Z0-9]*\d)(?=[A-HJ-NPR-Z0-9]*[A-HJ-NPR-Z])([A-HJ-NPR-Z0-9]{17})\b")

def _targa_in(testo):
    su = testo.upper()
    for r in RE_TARGA_UNIV:
        m = r.search(su)
        if m and re.search(r"\d", m.group(1)) and re.search(r"[A-Z]", m.group(1)):
            return m.group(1)
    return None

def lettura_universale(testo):
    """
    Restituisce (veicoli, bollo, avvisi); veicoli = [{targa, telaio, esente, imponibili{codice: importo}}].
    Due convenzioni di impaginazione, scelte in automatico:
    - 'Rif. testo' (SDI standard): i riferimenti con la targa sono stampati SOTTO la riga a cui
      appartengono -> una riga seguita da un blocco con targa va a QUELLA targa; le altre restano
      sulla targa corrente.
    - descrizioni libere (es. 'TARGA: XX000XX' sopra le spese): le righe vanno all'ultima targa vista sopra.
    """
    linee = [l.strip() for l in testo.splitlines()]
    # taglia il riepilogo finale
    for i, l in enumerate(linee):
        if l.upper().startswith("RIEPILOGHI IVA"):
            linee = linee[:i]
            break
    modo_sotto = any(l.startswith("Rif. testo:") and _targa_in(l) for l in linee)

    # sequenza di elementi: ('riga', desc, aliq, importo) oppure ('blocco', testo delle righe non-articolo)
    elementi, blocco = [], []
    for l in linee:
        m = RE_RIGA_UNIV.match(l)
        if m:
            if blocco:
                elementi.append(("blocco", "\n".join(blocco))); blocco = []
            elementi.append(("riga", m.group(1), m.group(4), dec(m.group(5))))
        else:
            blocco.append(l)
    if blocco:
        elementi.append(("blocco", "\n".join(blocco)))

    veicoli, avvisi = {}, []
    bollo = Decimal("0")
    corrente = None           # (targa, telaio)
    senza_targa = []

    def info_blocco(txt):
        t = _targa_in(txt)
        if not t: return None
        v = RE_VIN_UNIV.search(txt.upper().replace(t, ""))
        return (t, v.group(1) if v else "")

    for i, el in enumerate(elementi):
        if el[0] == "blocco":
            if not modo_sotto:
                info = info_blocco(el[1])
                if info: corrente = info
            continue
        _, desc, aliq, importo = el
        du = desc.upper()
        if aliq.startswith("N") and ("BOLLO ASSOLTO" in du or du.strip() == "BOLLO") and importo == Decimal("2.00"):
            bollo += importo
            continue
        dest = corrente
        if modo_sotto:
            succ = elementi[i + 1] if i + 1 < len(elementi) else None
            info = info_blocco(succ[1]) if succ and succ[0] == "blocco" else None
            if info:
                dest = corrente = info
        if dest is None:
            senza_targa.append((aliq, importo)); continue
        v = veicoli.setdefault(dest[0], {"targa": dest[0], "telaio": dest[1], "esente": Decimal("0"), "imponibili": {}})
        if not v["telaio"] and dest[1]: v["telaio"] = dest[1]
        if aliq == "N1":
            v["esente"] += importo
        else:
            v["imponibili"][aliq] = v["imponibili"].get(aliq, Decimal("0")) + importo
    if senza_targa:
        avvisi.append(f"{len(senza_targa)} righe senza targa: messe su una riga senza targa da completare a mano")
        v = veicoli.setdefault("", {"targa": "", "telaio": "", "esente": Decimal("0"), "imponibili": {}})
        for aliq, importo in senza_targa:
            if aliq == "N1": v["esente"] += importo
            else: v["imponibili"][aliq] = v["imponibili"].get(aliq, Decimal("0")) + importo
    elenco = list(veicoli.values())
    # Impaginazione a descrizioni libere con competenze scritte una volta sola in fondo
    # (es. 2 targhe, competenze solo sull'ultima): si dividono in parti uguali fra le targhe.
    con_targa = [v for v in elenco if v["targa"]]
    if not modo_sotto and len(con_targa) > 1 and any(not v["imponibili"] for v in con_targa):
        totali = {}
        for v in con_targa:
            for k, w in v["imponibili"].items():
                totali[k] = totali.get(k, Decimal("0")) + w
            v["imponibili"] = {}
        n = len(con_targa)
        for k, tot in totali.items():
            tot = cent(tot)
            quota = cent(tot / n)
            for i, v in enumerate(con_targa):
                v["imponibili"][k] = tot - quota * (n - 1) if i == n - 1 else quota
        avvisi.append("competenze indicate una sola volta per tutte le targhe: divise in parti uguali, verifica la ripartizione")
    return elenco, bollo, avvisi


def fmt_eur(x):
    """Decimal -> '24,40'"""
    return str(cent(x)).replace(".", ",")


# =====================================================================
# ESTRAZIONE: dal PDF alle righe "base" (formato veicoli nuovi, 9 colonne)
# [numero, data, targa, telaio, esente, imponibile, iva, codice_iva, lordo]
# =====================================================================
def leggi_testo_nativo(file_bytes):
    """Testo contenuto nel PDF (esatto al centesimo, quando c'è)."""
    testo = ""
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for pagina in pdf.pages:
            testo += (pagina.extract_text() or "") + "\n"
    return testo


def leggi_testo_ocr(file_bytes):
    """Lettura ottica della pagina (per PDF immagine o con testo illeggibile)."""
    immagini = convert_from_bytes(file_bytes, dpi=300)
    return "\n".join(pytesseract.image_to_string(img, lang="ita") for img in immagini)


def caratteri_utili(testo):
    """
    Conta solo lettere e cifre vere. I PDF con caratteri non mappati restituiscono
    sequenze tipo "(cid:12)(cid:45)...": sembrano testo lungo ma non contengono nulla.
    """
    pulito = re.sub(r"\(cid:\d+\)", "", testo)
    return len(re.findall(r"[A-Za-z0-9]", pulito))


def righe_da_testo(testo_completo, avvisi):
    """Applica la logica dell'agenzia riconosciuta e restituisce le righe base."""
    dati_csv = []
    testo_norm = testo_completo.upper().replace(".", "").replace(" ", "").strip()

    # --- LOGICA 1: PUNTO PRATICHE ---
    if "UNTOPRATICHE" in testo_norm:
        imponibile_36, iva_36 = (29.51, 6.49)
        imponibile_33, iva_33 = (27.05, 5.95)
        
        num_fattura_match = re.search(r"Fattura n[\.\s]*(\d+)", testo_completo, re.IGNORECASE)
        data_fattura_match = re.search(r"del\s*(\d{2}[-/]\d{2}[-/]\d{4})", testo_completo)
        num_fattura = num_fattura_match.group(1)[-4:] if num_fattura_match else "0000"
        data_fattura = data_fattura_match.group(1).replace('-', '/') if data_fattura_match else "01/01/2026"
        
        pratiche = re.split(r"Prat\.\s*\d+", testo_completo, flags=re.IGNORECASE)[1:] 
        for pratica in pratiche:
            targa_match = re.search(r"Targa\s+([A-Z0-9]+)", pratica, re.IGNORECASE)
            telaio_match = re.search(r"Telaio\s+([A-Z0-9]+)", pratica, re.IGNORECASE)
            if not targa_match or not telaio_match: 
                continue
                
            targa = targa_match.group(1)
            telaio = telaio_match.group(1)
            
            competenza_lorda, imponibile, iva = ("36,00", imponibile_36, iva_36)
            if "33,00" in pratica:
                competenza_lorda, imponibile, iva = ("33,00", imponibile_33, iva_33)
                
            importi_a15 = re.findall(r"a15\s+([\d,]+)", pratica, re.IGNORECASE)
            totale_a15 = sum(float(imp.replace(',', '.')) for imp in importi_a15)
            
            dati_csv.append([num_fattura, data_fattura, targa, telaio, "0,00", f"{imponibile:.2f}".replace('.', ','), f"{iva:.2f}".replace('.', ','), ".22", competenza_lorda])
            dati_csv.append([num_fattura, data_fattura, targa, telaio, f"{totale_a15:.2f}".replace('.', ','), "0,00", "0,00", "", "0,00"])
            
        if "2,00" in (testo_completo.split("MEF")[1] if "MEF" in testo_completo else ""):
            if len(dati_csv) > 0:
                dati_csv.append([num_fattura, data_fattura, dati_csv[-1][2], dati_csv[-1][3], "2,00", "0,00", "0,00", "", "0,00"])
    
    # --- LOGICA 2: U.P.A. ELIO GNOCCHI ---
    elif "ELIOGNOCCHI" in testo_norm or "GNCLEI" in testo_norm:
        num_fattura_match = re.search(r"(?:Numero documento|Fattura|TD01).*?(\d+)(?:/|\s)", testo_completo, re.IGNORECASE | re.DOTALL)
        data_fattura_match = re.search(r"(?:Data documento|Data).*?(\d{2}[-/]\d{2}[-/]\d{4})", testo_completo, re.IGNORECASE | re.DOTALL)
        
        num_fattura = num_fattura_match.group(1).lstrip('0') if num_fattura_match else "000"
        data_fattura = data_fattura_match.group(1).replace('-', '/') if data_fattura_match else "01/01/2026"

        parti = re.split(r'(?:Targato|Targa)\s+([A-Z0-9]+)', testo_completo, flags=re.IGNORECASE)
        
        if len(parti) > 1:
            for i in range(1, len(parti), 2):
                targa = parti[i].upper()
                blocco = parti[i+1].replace('\n', ' ')
                
                importi_esenti = re.findall(r'(\d+[\,\.]\d{2})\s*(?:N1|NI|Nl)\b', blocco, re.IGNORECASE)
                
                totale_a15 = 0.0
                for imp in importi_esenti:
                    valore = float(imp.replace(',', '.'))
                    if valore != 2.00:
                        totale_a15 += valore
                        
                dati_csv.append([num_fattura, data_fattura, targa, "", "0,00", "20,00", "4,40", ".22", "24,40"])
                if totale_a15 > 0:
                    dati_csv.append([num_fattura, data_fattura, targa, "", f"{totale_a15:.2f}".replace('.', ','), "0,00", "0,00", "", "0,00"])
        else:
             dati_csv.append([num_fattura, data_fattura, "", "", "0,00", "0,00", "0,00", "", "0,00"])

        if re.search(r'MARCA DA BOLLO.*?\b2[\,\.]00\b', testo_completo, re.IGNORECASE | re.DOTALL):
            if len(dati_csv) > 0:
                ultimo_veicolo = dati_csv[-1][2]
                dati_csv.append([num_fattura, data_fattura, ultimo_veicolo, "", "2,00", "0,00", "0,00", "", "0,00"])

    # --- LOGICA 3: AGENZIA FERRARO ---
    elif "FERRARO" in testo_norm or "RSSFCN" in testo_norm:
        num_fattura_match = re.search(r"Numero documento.*?(\d+)(?:/|\s)", testo_completo, re.IGNORECASE | re.DOTALL)
        data_fattura_match = re.search(r"Data documento.*?(\d{2}[-/]\d{2}[-/]\d{4})", testo_completo, re.IGNORECASE | re.DOTALL)
        targa_match = re.search(r"Targato\s+([A-Z0-9]+)", testo_completo, re.IGNORECASE)
        telaio_match = re.search(r"Telaio\s+([A-Z0-9]+)", testo_completo, re.IGNORECASE)

        num_fattura = num_fattura_match.group(1).lstrip('0') if num_fattura_match else "000"
        data_fattura = data_fattura_match.group(1).replace('-', '/') if data_fattura_match else "01/01/2026"
        targa = targa_match.group(1) if targa_match else ""
        telaio = telaio_match.group(1) if telaio_match else ""

        onorario = 0.0
        spese_a15 = 0.0
        bollo = 0.0

        for riga in testo_completo.split('\n'):
            numeri = re.findall(r'(\d+[\,\.]\d{2})', riga)
            if not numeri:
                continue
            importo = float(numeri[-1].replace(',', '.'))

            if "NOSTRO ONORARIO" in riga.upper() or "ONORARIO" in riga.upper():
                onorario += importo
            elif "MARCA DA BOLLO" in riga.upper():
                bollo += importo
            elif any(kw in riga.upper() for kw in ["D.T.T.", "P.R.A.", "PAGOPA", "IMMATRICOLAZIONE", "EMOLUMENTI", "COMMISSIONI"]):
                spese_a15 += importo

        if onorario > 0:
            dati_csv.append([num_fattura, data_fattura, targa, telaio, "0,00", f"{onorario:.2f}".replace('.', ','), "0,00", "N2.2", f"{onorario:.2f}".replace('.', ',')])
        if spese_a15 > 0:
            dati_csv.append([num_fattura, data_fattura, targa, telaio, f"{spese_a15:.2f}".replace('.', ','), "0,00", "0,00", "", "0,00"])
        if bollo > 0:
            dati_csv.append([num_fattura, data_fattura, targa, telaio, f"{bollo:.2f}".replace('.', ','), "0,00", "0,00", "", "0,00"])

    # --- LOGICA 4: DI LECCE MASSIMILIANO ---
    elif "DILECCEMASSIMILIANO" in testo_norm or "DLCMSM" in testo_norm:
        # Fattura elettronica SDI: i "Rif. testo: Prat./Targa" sono stampati SOTTO la riga
        # a cui appartengono, quindi NON si può tagliare il testo sulla parola "Prat.".
        # Le righe sono classificate dalla colonna %IVA (22,00 = competenza, N1 = Art.15).
        num_fattura, data_fattura = numero_data_sdi(testo_completo)
        num_fattura = num_fattura or "000"
        data_fattura = data_fattura or "01/01/2026"

        pratiche, bollo_mef, avvisi_dl = pratiche_sdi(testo_completo)
        for avviso in avvisi_dl:
            avvisi.append(avviso)

        for p in pratiche:
            if p["imponibile"] > 0:
                iva = cent(p["imponibile"] * Decimal("0.22"))
                dati_csv.append([num_fattura, data_fattura, p["targa"], p["telaio"], "0,00",
                                 fmt_eur(p["imponibile"]), fmt_eur(iva), ".22",
                                 fmt_eur(p["imponibile"] + iva)])
            if p["esente"] > 0:
                dati_csv.append([num_fattura, data_fattura, p["targa"], p["telaio"],
                                 fmt_eur(p["esente"]), "0,00", "0,00", "", "0,00"])

        # Bollo virtuale MEF (2,00) su riga separata, assegnato all'ultima targa
        if bollo_mef > 0 and pratiche:
            dati_csv.append([num_fattura, data_fattura, pratiche[-1]["targa"], pratiche[-1]["telaio"],
                             fmt_eur(bollo_mef), "0,00", "0,00", "", "0,00"])

    # --- LOGICA 4b: CENTRO SERVIZI AUTOMOBILISTICI ---
    elif "CENTROSERVIZIAUTOMOBILISTICI" in testo_norm or "02743500783" in testo_norm:
        num_fattura, data_fattura = numero_data_sdi(testo_completo)
        num_fattura = num_fattura or "000"
        data_fattura = data_fattura or "01/01/2026"
        veicoli, avvisi_cs = veicoli_centro_servizi(testo_completo)
        for avviso in avvisi_cs:
            avvisi.append(avviso)
        for v in veicoli:
            if v["imponibile"] > 0:
                dati_csv.append([num_fattura, data_fattura, v["targa"], "", "0,00",
                                 fmt_eur(v["imponibile"]), fmt_eur(v["iva"]), ".22",
                                 fmt_eur(v["imponibile"] + v["iva"])])
            if v["esente"] > 0:
                dati_csv.append([num_fattura, data_fattura, v["targa"], "",
                                 fmt_eur(v["esente"]), "0,00", "0,00", "", "0,00"])

    # --- LOGICA 4c: MSERVICE ---
    elif "MSERVICE" in testo_norm or "02000390761" in testo_norm:
        num_fattura, data_fattura = numero_data_sdi(testo_completo)
        num_fattura = num_fattura or "000"
        data_fattura = data_fattura or "01/01/2026"
        pratiche_ms, bollo_ms, avvisi_ms = pratiche_mservice(testo_completo)
        avvisi.extend(avvisi_ms)
        for p in pratiche_ms:
            # prima le competenze al 22%, poi le spese esenti (stesso ordine dei CSV MSERVICE)
            for aliq, importo in p["righe"]:
                if not aliq.startswith("N"):
                    iva = cent(importo * Decimal("0.22"))
                    dati_csv.append([num_fattura, data_fattura, p["targa"], p["telaio"], "0,00",
                                     fmt_eur(importo), fmt_eur(iva), ".22", fmt_eur(importo + iva)])
            for aliq, importo in reversed(p["righe"]):
                if aliq.startswith("N"):
                    dati_csv.append([num_fattura, data_fattura, p["targa"], p["telaio"],
                                     fmt_eur(importo), "0,00", "0,00", "", "0,00"])
        if bollo_ms > 0 and pratiche_ms:
            dati_csv.append([num_fattura, data_fattura, pratiche_ms[-1]["targa"], pratiche_ms[-1]["telaio"],
                             fmt_eur(bollo_ms), "0,00", "0,00", "", "0,00"])

    # --- LOGICA 5: FORNITORI NON CONFIGURATI -> LETTORE UNIVERSALE ---
    # L'app non si blocca: legge la fattura elettronica dalla colonna %IVA (22,00 / N1 ...)
    # e trova le targhe da sola. Il controllo di quadratura dice se i totali tornano.
    else:
        num_fattura, data_fattura = numero_data_sdi(testo_completo)
        if not num_fattura:
            m = re.search(r"(?:Numero documento|Fattura|n\.|Numero)\s*:?\s*(\d+)", testo_completo, re.IGNORECASE)
            num_fattura = m.group(1).lstrip("0") if m else "000"
        if not data_fattura:
            m = re.search(r"(\d{2}[/-]\d{2}[/-]\d{4})", testo_completo)
            data_fattura = m.group(1).replace("-", "/") if m else "01/01/2026"

        veicoli_u, bollo_u, avvisi_u = lettura_universale(testo_completo)
        avvisi.append("fornitore non configurato: dati letti con il lettore universale, verificali prima di registrare")
        avvisi.extend(avvisi_u)
        for v in veicoli_u:
            for codice, imponibile in v["imponibili"].items():
                imponibile = cent(imponibile)
                if codice[0].isdigit():                       # aliquota IVA, es. '22,00' -> '.22'
                    aliquota = dec(codice)
                    cod_iva = "." + str(int(aliquota))
                    iva = cent(imponibile * aliquota / 100)
                else:                                         # natura (es. N2.2 forfettario): niente IVA
                    cod_iva, iva = codice, Decimal("0")
                dati_csv.append([num_fattura, data_fattura, v["targa"], v["telaio"], "0,00",
                                 fmt_eur(imponibile), fmt_eur(iva), cod_iva, fmt_eur(imponibile + iva)])
            if v["esente"] > 0:
                dati_csv.append([num_fattura, data_fattura, v["targa"], v["telaio"],
                                 fmt_eur(v["esente"]), "0,00", "0,00", "", "0,00"])
        if bollo_u > 0:
            ultimo = veicoli_u[-1] if veicoli_u else {"targa": "", "telaio": ""}
            dati_csv.append([num_fattura, data_fattura, ultimo["targa"], ultimo["telaio"],
                             fmt_eur(bollo_u), "0,00", "0,00", "", "0,00"])
        if not dati_csv:
            # impaginazione non riconosciuta: almeno l'intelaiatura con le targhe, importi a zero
            avvisi.append("impaginazione non riconosciuta: righe con importi a 0,00 da compilare a mano")
            targhe = re.findall(r"(?:Targa|Targato)\s*:?\s*([A-Z0-9]+)", testo_completo, re.IGNORECASE)
            for t in (list(dict.fromkeys(targhe)) or [""]):
                dati_csv.append([num_fattura, data_fattura, t.upper(), "", "0,00", "0,00", "0,00", "", "0,00"])

    # --- NUMERO/DATA DA RIGA SDI (vale per tutte le agenzie con fattura elettronica) ---
    # Prima il numero veniva preso da "TD01" -> "1". Se la riga SDI c'è, ha la precedenza.
    num_sdi, data_sdi = numero_data_sdi(testo_completo)
    if num_sdi:
        for r in dati_csv:
            r[0], r[1] = num_sdi, data_sdi

    # --- NUMERO/DATA DA RIGA SDI (vale per tutte le agenzie con fattura elettronica) ---
    num_sdi, data_sdi = numero_data_sdi(testo_completo)
    if num_sdi:
        for r in dati_csv:
            r[0], r[1] = num_sdi, data_sdi
    return dati_csv


def controlla_quadratura(testo_completo, dati_csv):
    """
    Confronta i totali del CSV con il riepilogo IVA della fattura.
    Restituisce None se la fattura non ha un riepilogo leggibile,
    altrimenti la lista delle differenze (vuota = tutto quadra).
    """
    riepilogo = riepilogo_sdi(testo_completo)
    if not riepilogo or not dati_csv:
        return None
    somma = lambda col, cond: sum((dec(r[col]) for r in dati_csv if cond(r[7])), Decimal("0"))
    controlli = [
        ("Esente Art.15 (N1)", somma(4, lambda c: True),
         sum((v for k, v in riepilogo.items() if k.startswith("N1")), Decimal("0"))),
        ("Imponibile 22%", somma(5, lambda c: c == ".22"), riepilogo.get("22,00", Decimal("0"))),
        ("Imponibile N2.2", somma(5, lambda c: c == "N2.2"), riepilogo.get("N2.2", Decimal("0"))),
    ]
    return [f"{nome}: CSV {fmt_eur(csv_v)} / fattura {fmt_eur(fatt)}"
            for nome, csv_v, fatt in controlli if csv_v != fatt]


def estrai_righe(file_pdf):
    """
    Legge un PDF e restituisce le righe base.
    1. Usa il testo nativo del PDF (esatto). Se è vuoto o illeggibile -> OCR.
    2. Se con il testo nativo non esce nessuna riga o i totali NON quadrano col riepilogo IVA,
       riprova automaticamente con l'OCR e ne usa il risultato SOLO se con l'OCR i totali quadrano.
    """
    nome = file_pdf.name
    file_bytes = file_pdf.getvalue()

    # --- 1. LETTURA ---
    try:
        testo = leggi_testo_nativo(file_bytes)
    except Exception as e:
        st.warning(f"{nome}: testo nativo non leggibile ({e}), provo con l'OCR.")
        testo = ""
    fonte = "testo del PDF"
    if caratteri_utili(testo) < 30:
        st.info(f"🔄 {nome}: il PDF non contiene testo leggibile, avvio la lettura ottica (OCR)...")
        try:
            testo = leggi_testo_ocr(file_bytes)
            fonte = "OCR"
        except Exception as e:
            st.error(f"{nome}: errore OCR. Dettagli: {e}")
            return []

    avvisi = []
    dati_csv = righe_da_testo(testo, avvisi)
    errori = controlla_quadratura(testo, dati_csv)

    # --- 2. OCR DI RISERVA SE IL TESTO NATIVO NON HA FUNZIONATO ---
    if fonte == "testo del PDF" and (not dati_csv or errori):
        st.info(f"🔄 {nome}: i dati letti non tornano, riprovo con la lettura ottica (OCR)...")
        try:
            testo_ocr = leggi_testo_ocr(file_bytes)
            avvisi_ocr = []
            righe_ocr = righe_da_testo(testo_ocr, avvisi_ocr)
            errori_ocr = controlla_quadratura(testo_ocr, righe_ocr)
            if righe_ocr and errori_ocr == []:
                testo, dati_csv, errori, avvisi, fonte = testo_ocr, righe_ocr, errori_ocr, avvisi_ocr, "OCR"
                st.info(f"{nome}: con l'OCR i totali quadrano, uso il risultato dell'OCR.")
            elif not dati_csv and righe_ocr:
                testo, dati_csv, errori, avvisi, fonte = testo_ocr, righe_ocr, errori_ocr, avvisi_ocr, "OCR"
                st.warning(f"{nome}: righe ricavate solo con l'OCR e non verificabili: controllale una per una.")
            else:
                st.warning(f"{nome}: neanche l'OCR risolve, tengo il testo del PDF. Controlla la fattura a mano.")
        except Exception as e:
            st.warning(f"{nome}: OCR di riserva non disponibile ({e}).")

    # --- 3. ESITO ---
    for avviso in dict.fromkeys(avvisi):
        st.warning(f"{nome}: {avviso}")
    if errori:
        st.warning(f"⚠️ {nome} NON quadra con il riepilogo IVA: " + " · ".join(errori))
    elif errori == []:
        st.info(f"✅ {nome}: totali quadrati con il riepilogo IVA della fattura (lettura: {fonte}).")
    return dati_csv


# =====================================================================
# I TRE FORMATI DI CSV PER INFINITY
# (ricavati dai CSV MSERVICE: Infi2026276 = margine, Infi2026275_MODIFICATO = usati IVA)
# =====================================================================
def _imp(s):
    return dec(s) if s else Decimal("0")

def formatta_nuovi(r):
    # 9 colonne: num;data;targa;telaio;ESENTE;IMPONIBILE;IVA;cod;LORDO (= imponibile + IVA)
    return list(r)

def formatta_usati_iva(r):
    # 8 colonne: num;data;targa;telaio;IMPONIBILE;ESENTE;IVA;cod   (esente e imponibile scambiati, niente lordo)
    num, data, targa, telaio, esente, imponibile, iva, cod = r[:8]
    return [num, data, targa, telaio, imponibile, esente, iva, cod]

def formatta_margine(r):
    # 9 colonne: num;data;targa;telaio;ESENTE;IMPONIBILE;IVA;cod;TOTALE (= esente + imponibile)
    num, data, targa, telaio, esente, imponibile, iva, cod = r[:8]
    return [num, data, targa, telaio, esente, imponibile, iva, cod, fmt_eur(_imp(esente) + _imp(imponibile))]

TIPI = [
    ("nuovi",   "🆕 Veicoli nuovi",          "Immatricolazioni di veicoli nuovi",       formatta_nuovi,     "NUOVI"),
    ("iva",     "🔄 Veicoli usati IVA",      "Passaggi di proprietà di usato in regime IVA", formatta_usati_iva, "USATI_IVA"),
    ("margine", "⚖️ Veicoli usati a margine", "Passaggi di proprietà di usato a margine",  formatta_margine,   "MARGINE"),
]


def genera_csv(righe):
    output = io.StringIO()
    csv.writer(output, delimiter=";", lineterminator="\r\n").writerows(righe)
    return output.getvalue().encode("utf-8")


# =====================================================================
# INTERFACCIA: tre riquadri di caricamento distinti
# =====================================================================
st.set_page_config(page_title="Convertitore Infinity", page_icon="🚗", layout="wide")
st.title("🚗 Convertitore Fatture -> CSV Infinity")
st.write("Ufficio Amministrazione: carica le fatture d'agenzia nel riquadro giusto. "
         "Ogni riquadro produce il CSV nel formato richiesto da Infinity per quel tipo di pratica. "
         "Se il PDF è un'immagine, il sistema userà l'OCR in automatico.")

for chiave, titolo, sottotitolo, formatta, suffisso in TIPI:
    with st.container(border=True):
        st.subheader(titolo)
        st.caption(sottotitolo)
        file_caricati = st.file_uploader(f"Fatture PDF — {titolo}", type="pdf",
                                         accept_multiple_files=True, key=f"upload_{chiave}")
        for n, file_pdf in enumerate(file_caricati or []):
            dati_csv = estrai_righe(file_pdf)
            if not dati_csv:
                st.error(f"Impossibile estrarre i dati dalla fattura {file_pdf.name}. Verifica il layout.")
                continue
            righe_out = [formatta(r) for r in dati_csv]
            nome_csv = file_pdf.name.rsplit(".", 1)[0] + f"_{suffisso}.csv"
            st.success(f"Fattura {file_pdf.name} elaborata: {len(righe_out)} righe ({titolo}).")
            st.download_button(label=f"⬇️ Scarica {nome_csv}", data=genera_csv(righe_out),
                               file_name=nome_csv, mime="text/csv",
                               key=f"download_{chiave}_{n}_{file_pdf.name}")
