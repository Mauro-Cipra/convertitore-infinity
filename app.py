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
    """Riga 'TD01 fattura 20260001151 09-10-2026' della fattura elettronica."""
    m = re.search(r"\bTD\d{2}\b\D*?(\d{3,})\s+(\d{2})[-/](\d{2})[-/](\d{4})", testo)
    if not m:
        return None, None
    numero, g, mese, anno = m.groups()
    if numero.startswith(anno) and len(numero) > 4:   # 20260001151 -> 0001151
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
    """Totali del 'RIEPILOGHI IVA E TOTALI' per la quadratura."""
    tot = {}
    for l in testo.splitlines():
        m = re.match(rf"^I \(esigibilit.*?\)\s+(N\d(?:\.\d)?|\d{{1,2}},\d{{2}})\s+{IMP}\s+{IMP}\s+({IMP})\s+({IMP}|0)$", l.strip())
        if m:
            tot[m.group(1)] = dec(m.group(2))
    return tot

def fmt_eur(x):
    """Decimal -> '24,40'"""
    return str(cent(x)).replace(".", ",")


# =====================================================================
# ESTRAZIONE: dal PDF alle righe "base" (formato veicoli nuovi, 9 colonne)
# [numero, data, targa, telaio, esente, imponibile, iva, codice_iva, lordo]
# =====================================================================
def estrai_righe(file_pdf):
    """Legge un PDF e restituisce le righe base. Logiche per agenzia invariate."""
    dati_csv = []
    file_bytes = file_pdf.getvalue()
    
    # 1. TENTATIVO DI LETTURA TESTO STANDARD
    testo_completo = ""
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for pagina in pdf.pages:
            testo_completo += (pagina.extract_text() or "") + "\n"
    
    testo_norm = testo_completo.upper().replace(".", "").replace(" ", "").strip()
    
    # 2. SE IL TESTO NON ESISTE O E' CORROTTO, AVVIA L'OCR
    if len(testo_norm) < 30:
        st.info(f"🔄 Avvio lettura ottica (OCR) per {file_pdf.name}. Potrebbe richiedere qualche secondo...")
        try:
            immagini = convert_from_bytes(file_bytes)
            testo_completo = ""
            for img in immagini:
                testo_completo += pytesseract.image_to_string(img, lang='ita') + "\n"
            testo_norm = testo_completo.upper().replace(".", "").replace(" ", "").strip()
        except Exception as e:
            st.error(f"{file_pdf.name}: errore OCR. Dettagli: {e}")
            return []
        
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
            st.warning(f"{file_pdf.name}: {avviso}")

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

    # --- LOGICA 5: ALTRI FORNITORI GENERICI ---
    else:
        num_fattura_match = re.search(r"(?:Numero documento|Fattura|n\.|Numero)\s*:?\s*(\d+)", testo_completo, re.IGNORECASE)
        data_fattura_match = re.search(r"(\d{2}[/-]\d{2}[/-]\d{4})", testo_completo)
        
        num_fattura = num_fattura_match.group(1).lstrip('0') if num_fattura_match else "000"
        data_fattura = data_fattura_match.group(1).replace('-', '/') if data_fattura_match else "01/01/2026"
        
        targhe = re.findall(r"(?:Targa|Targato)\s+([A-Z0-9]+)", testo_completo, re.IGNORECASE)
        if targhe:
            targhe_uniche = list(dict.fromkeys(targhe))
            for t in targhe_uniche:
                dati_csv.append([num_fattura, data_fattura, t.upper(), "", "0,00", "0,00", "0,00", "", "0,00"])
        else:
            dati_csv.append([num_fattura, data_fattura, "", "", "0,00", "0,00", "0,00", "", "0,00"])

    # --- NUMERO/DATA DA RIGA SDI (vale per tutte le agenzie con fattura elettronica) ---
    # Prima il numero veniva preso da "TD01" -> "1". Se la riga SDI c'è, ha la precedenza.
    num_sdi, data_sdi = numero_data_sdi(testo_completo)
    if num_sdi:
        for r in dati_csv:
            r[0], r[1] = num_sdi, data_sdi

    # --- QUADRATURA CON IL RIEPILOGO IVA DELLA FATTURA ---
    riepilogo = riepilogo_sdi(testo_completo)
    if riepilogo and dati_csv:
        somma = lambda col, cond: sum((dec(r[col]) for r in dati_csv if cond(r[7])), Decimal("0"))
        controlli = [
            ("Esente Art.15 (N1)", somma(4, lambda c: True),
             sum((v for k, v in riepilogo.items() if k.startswith("N1")), Decimal("0"))),
            ("Imponibile 22%", somma(5, lambda c: c == ".22"), riepilogo.get("22,00", Decimal("0"))),
            ("Imponibile N2.2", somma(5, lambda c: c == "N2.2"), riepilogo.get("N2.2", Decimal("0"))),
        ]
        errori = [f"{nome}: CSV {fmt_eur(csv_v)} / fattura {fmt_eur(fatt)}"
                  for nome, csv_v, fatt in controlli if csv_v != fatt]
        if errori:
            st.warning(f"⚠️ {file_pdf.name} NON quadra con il riepilogo IVA: " + " · ".join(errori))
        else:
            st.info(f"✅ {file_pdf.name}: totali quadrati con il riepilogo IVA della fattura.")

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
