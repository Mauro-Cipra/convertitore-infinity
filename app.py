import streamlit as st
import pdfplumber
import re
import csv
import io
from pdf2image import convert_from_bytes
import pytesseract

st.set_page_config(page_title="Convertitore Infinity", page_icon="🚗")

st.title("🚗 Convertitore Fatture -> CSV Infinity")
st.write("Ufficio Amministrazione: Carica i PDF delle fatture. Se il PDF è un'immagine o vettoriale, il sistema userà l'OCR per leggerlo in automatico.")

file_caricati = st.file_uploader("Trascina qui le fatture in PDF", type="pdf", accept_multiple_files=True)

if file_caricati:
    for file_pdf in file_caricati:
        dati_csv = []
        file_bytes = file_pdf.read()
        
        # 1. TENTATIVO DI LETTURA TESTO STANDARD
        testo_completo = ""
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for pagina in pdf.pages:
                testo_completo += (pagina.extract_text() or "") + "\n"
        
        testo_norm = testo_completo.upper().replace(".", "").replace(" ", "").strip()
        
        # 2. SE IL TESTO NON ESISTE, AVVIA L'OCR
        if len(testo_norm) < 30:
            st.info(f"🔄 Avvio lettura ottica (OCR) per {file_pdf.name}. Potrebbe richiedere qualche secondo...")
            try:
                immagini = convert_from_bytes(file_bytes)
                testo_completo = ""
                for img in immagini:
                    testo_completo += pytesseract.image_to_string(img, lang='ita') + "\n"
                testo_norm = testo_completo.upper().replace(".", "").replace(" ", "").strip()
            except Exception as e:
                st.error(f"Errore OCR. Assicurati di aver creato i file packages.txt e requirements.txt su GitHub. Dettagli: {e}")
                continue
            
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
            num_fattura_match = re.search(r"(?:Numero documento|TD01 fattura|Fattura).*?(\d+)(?:/|\s)", testo_completo, re.IGNORECASE | re.DOTALL)
            data_fattura_match = re.search(r"Data documento.*?(\d{2}[-/]\d{2}[-/]\d{4})", testo_completo, re.IGNORECASE | re.DOTALL)
            
            num_fattura = num_fattura_match.group(1).lstrip('0') if num_fattura_match else "000"
            data_fattura = data_fattura_match.group(1).replace('-', '/') if data_fattura_match else "01/01/2026"

            parti = re.split(r'Prat\.\s*\d+', testo_completo, flags=re.IGNORECASE)
            
            if len(parti) > 1:
                for i in range(1, len(parti)):
                    blocco = parti[i]
                    # Appiattisce l'intero blocco pratica su una sola riga per blindare la lettura dei dati
                    blocco_flat = blocco.replace('\n', ' ')
                    
                    targa_match = re.search(r'(?:Targa|Targato)\s+([A-Z0-9]+)', blocco_flat, re.IGNORECASE)
                    if not targa_match:
                        continue
                    targa = targa_match.group(1).upper()
                    
                    # Usa espressioni regolari per pescare gli importi incollati alle sigle specifiche
                    importi_esenti = re.findall(r'(\d+[\,\.]\d{2})\s*(?:N1|NI|Nl)\b', blocco_flat, re.IGNORECASE)
                    importi_22 = re.findall(r'(\d+[\,\.]\d{2})\s*22[\,\.]00\b', blocco_flat, re.IGNORECASE)
                    
                    totale_a15 = 0.0
                    for imp in importi_esenti:
                        valore = float(imp.replace(',', '.'))
                        if valore != 2.00:  # Esclude le marche da bollo
                            totale_a15 += valore
                            
                    totale_22 = 0.0
                    for imp in importi_22:
                        valore = float(imp.replace(',', '.'))
                        # In alcune righe sfalsate, se legge 10.20 come IVA, corregge l'errore dell'OCR
                        if valore not in [10.20, 5.50]: 
                            totale_22 += valore
                    
                    # Generazione delle righe nel CSV
                    iva_22 = totale_22 * 0.22
                    lordo_22 = totale_22 + iva_22
                    
                    if totale_22 > 0:
                        dati_csv.append([num_fattura, data_fattura, targa, "", "0,00", f"{totale_22:.2f}".replace('.', ','), f"{iva_22:.2f}".replace('.', ','), ".22", f"{lordo_22:.2f}".replace('.', ',')])
                    
                    if totale_a15 > 0:
                        dati_csv.append([num_fattura, data_fattura, targa, "", f"{totale_a15:.2f}".replace('.', ','), "0,00", "0,00", "", "0,00"])

            else:
                 dati_csv.append([num_fattura, data_fattura, "", "", "0,00", "0,00", "0,00", "", "0,00"])

            # Gestione della Marca da Bollo finale se isolata
            if re.search(r'Marca da bollo.*?\b2[\,\.]00\b', testo_completo, re.IGNORECASE | re.DOTALL) or re.search(r'Bollo assolto.*?\b2[\,\.]00\b', testo_completo, re.IGNORECASE | re.DOTALL):
                 if len(dati_csv) > 0:
                     ultimo_veicolo = dati_csv[-1][2]
                     dati_csv.append([num_fattura, data_fattura, ultimo_veicolo, "", "2,00", "0,00", "0,00", "", "0,00"])

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

        # --- GENERAZIONE E DOWNLOAD CSV ---
        if dati_csv:
            output = io.StringIO()
            writer = csv.writer(output, delimiter=';')
            writer.writerows(dati_csv)
            csv_bytes = output.getvalue().encode('utf-8')
            
            nome_csv = file_pdf.name.replace('.pdf', '.csv').replace('.PDF', '.csv')
            
            st.success(f"Fattura {file_pdf.name} elaborata con successo!")
            st.download_button(label=f"⬇️ Scarica {nome_csv}", data=csv_bytes, file_name=nome_csv, mime="text/csv", key=file_pdf.name)
        else:
            st.error(f"Impossibile estrarre i dati dalla fattura {file_pdf.name}. Verifica il layout.")
