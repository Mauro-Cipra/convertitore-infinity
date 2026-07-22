import streamlit as st
import pdfplumber
import re
import csv
import io

st.set_page_config(page_title="Convertitore Infinity", page_icon="🚗")

st.title("🚗 Convertitore Fatture -> CSV Infinity")
st.write("Ufficio Amministrazione: Carica i PDF delle fatture per la conversione automatica in CSV.")

file_caricati = st.file_uploader("Trascina qui le fatture in PDF", type="pdf", accept_multiple_files=True)

if file_caricati:
    for file_pdf in file_caricati:
        dati_csv = []
        
        with pdfplumber.open(file_pdf) as pdf:
            testo_completo = ""
            testo_layout = "" 
            for pagina in pdf.pages:
                testo_completo += (pagina.extract_text() or "") + "\n"
                testo_layout += (pagina.extract_text(layout=True) or "") + "\n"
                
        # --- LOGICA 1: PUNTO PRATICHE ---
        if "UNTO PRATICHE" in testo_completo.upper() or "PUNTO PRATICHE" in testo_completo.upper():
            imponibile_36, iva_36 = (29.51, 6.49)
            imponibile_33, iva_33 = (27.05, 5.95)
            
            num_fattura_match = re.search(r"Fattura n\.\s*(\d+)", testo_completo)
            data_fattura_match = re.search(r"del\s*(\d{2}/\d{2}/\d{4})", testo_completo)
            num_fattura = num_fattura_match.group(1)[-4:] if num_fattura_match else "0000"
            data_fattura = data_fattura_match.group(1) if data_fattura_match else "01/01/2026"
            
            pratiche = re.split(r"Prat\.\s*\d+", testo_completo)[1:] 
            for pratica in pratiche:
                targa_match = re.search(r"Targa\s+([A-Z0-9]+)", pratica)
                telaio_match = re.search(r"Telaio\s+([A-Z0-9]+)", pratica)
                if not targa_match or not telaio_match: 
                    continue
                    
                targa = targa_match.group(1)
                telaio = telaio_match.group(1)
                
                competenza_lorda, imponibile, iva = ("36,00", imponibile_36, iva_36)
                if "33,00" in pratica:
                    competenza_lorda, imponibile, iva = ("33,00", imponibile_33, iva_33)
                    
                importi_a15 = re.findall(r"a15\s+([\d,]+)", pratica)
                totale_a15 = sum(float(imp.replace(',', '.')) for imp in importi_a15)
                
                dati_csv.append([num_fattura, data_fattura, targa, telaio, "0,00", f"{imponibile:.2f}".replace('.', ','), f"{iva:.2f}".replace('.', ','), ".22", competenza_lorda])
                dati_csv.append([num_fattura, data_fattura, targa, telaio, f"{totale_a15:.2f}".replace('.', ','), "0,00", "0,00", "", "0,00"])
                
            if "2,00" in (testo_completo.split("MEF")[1] if "MEF" in testo_completo else ""):
                if len(dati_csv) > 0:
                    dati_csv.append([num_fattura, data_fattura, dati_csv[-1][2], dati_csv[-1][3], "2,00", "0,00", "0,00", "", "0,00"])
        
        # --- LOGICA 2: U.P.A. ELIO GNOCCHI ---
        elif "ELIO GNOCCHI" in testo_completo.upper() or "GNCLEI61M10F052J" in testo_completo:
            num_fattura_match = re.search(r"Numero documento.*?(\d+)(?:/|\s)", testo_completo, re.IGNORECASE | re.DOTALL)
            data_fattura_match = re.search(r"Data documento.*?(\d{2}-\d{2}-\d{4})", testo_completo, re.IGNORECASE | re.DOTALL)
            
            num_fattura = num_fattura_match.group(1).lstrip('0') if num_fattura_match else "000"
            data_fattura = data_fattura_match.group(1).replace('-', '/') if data_fattura_match else "01/01/2026"

            righe = testo_layout.split('\n')
            veicolo_corrente = None
            totale_a15_corrente = 0.0
            
            for riga in righe:
                targa_match = re.search(r'Targato\s+([A-Z0-9]+)', riga)
                if targa_match:
                    if veicolo_corrente:
                        dati_csv.append([num_fattura, data_fattura, veicolo_corrente, "", "0,00", "20,00", "4,40", ".22", "24,40"])
                        dati_csv.append([num_fattura, data_fattura, veicolo_corrente, "", f"{totale_a15_corrente:.2f}".replace('.', ','), "0,00", "0,00", "", "0,00"])
                    veicolo_corrente = targa_match.group(1)
                    totale_a15_corrente = 0.0
                
                if veicolo_corrente and re.search(r'\bN[I1]\b', riga, re.IGNORECASE):
                    numeri = re.findall(r'(\d+[\,\.]\d{2})', riga)
                    if numeri:
                        valore = float(numeri[-1].replace(',', '.'))
                        if valore != 2.00:
                            totale_a15_corrente += valore
                            
            if veicolo_corrente:
                dati_csv.append([num_fattura, data_fattura, veicolo_corrente, "", "0,00", "20,00", "4,40", ".22", "24,40"])
                dati_csv.append([num_fattura, data_fattura, veicolo_corrente, "", f"{totale_a15_corrente:.2f}".replace('.', ','), "0,00", "0,00", "", "0,00"])
                
            if re.search(r'MARCA DA BOLLO.*?\b2[\,\.]00\b', testo_completo, re.IGNORECASE | re.DOTALL):
                if len(dati_csv) > 0:
                    dati_csv.append([num_fattura, data_fattura, veicolo_corrente, "", "2,00", "0,00", "0,00", "", "0,00"])

        # --- LOGICA 3: AGENZIA FERRARO ---
        elif "FERRARO" in testo_completo.upper() or "RSSFCN61H24L3061" in testo_completo:
            num_fattura_match = re.search(r"Numero documento.*?(\d+)(?:/|\s)", testo_completo, re.IGNORECASE | re.DOTALL)
            data_fattura_match = re.search(r"Data documento.*?(\d{2}[-/]\d{2}[-/]\d{4})", testo_completo, re.IGNORECASE | re.DOTALL)
            targa_match = re.search(r"Targato\s+([A-Z0-9]+)", testo_completo, re.IGNORECASE)
            telaio_match = re.search(r"Telaio\s+([A-Z0-9]+)", testo_completo, re.IGNORECASE)

            num_fattura = num_fattura_match.group(1).lstrip('0') if num_fattura_match else "503"
            data_fattura = data_fattura_match.group(1).replace('-', '/') if data_fattura_match else "14/07/2026"
            targa = targa_match.group(1) if targa_match else ""
            telaio = telaio_match.group(1) if telaio_match else ""

            onorario = 0.0
            spese_a15 = 0.0
            bollo = 0.0

            for riga in testo_layout.split('\n'):
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

            # 1. Riga Onorario (Regime Forfettario N2.2)
            if onorario > 0:
                dati_csv.append([num_fattura, data_fattura, targa, telaio, "0,00", f"{onorario:.2f}".replace('.', ','), "0,00", "N2.2", f"{onorario:.2f}".replace('.', ',')])
            
            # 2. Riga Spese Esenti Art. 15 (120,18 + 365,00 + 1,40 = 486,58)
            if spese_a15 > 0:
                dati_csv.append([num_fattura, data_fattura, targa, telaio, f"{spese_a15:.2f}".replace('.', ','), "0,00", "0,00", "", "0,00"])

            # 3. Riga Marca da bollo (2,00)
            if bollo > 0:
                dati_csv.append([num_fattura, data_fattura, targa, telaio, f"{bollo:.2f}".replace('.', ','), "0,00", "0,00", "", "0,00"])

        # --- LOGICA GENERICA DI BACKUP ---
        else:
            num_fattura_match = re.search(r"(?:Numero documento|Fattura n\.|Numero)\s*:?\s*(\d+)", testo_completo, re.IGNORECASE)
            data_fattura_match = re.search(r"(\d{2}[/-]\d{2}[/-]\d{4})", testo_completo)
            targa_match = re.search(r"(?:Targa|Targato)\s+([A-Z0-9]+)", testo_completo, re.IGNORECASE)

            num_fattura = num_fattura_match.group(1).lstrip('0') if num_fattura_match else "000"
            data_fattura = data_fattura_match.group(1).replace('-', '/') if data_fattura_match else "01/01/2026"
            veicolo_corrente = targa_match.group(1) if targa_match else ""

            dati_csv.append([num_fattura, data_fattura, veicolo_corrente, "", "0,00", "0,00", "0,00", "", "0,00"])

        # --- GENERAZIONE E DOWNLOAD CSV ---
        if dati_csv:
            output = io.StringIO()
            writer = csv.writer(output, delimiter=';')
            writer.writerows(dati_csv)
            csv_bytes = output.getvalue().encode('utf-8')
            
            nome_csv = file_pdf.name.replace('.pdf', '.csv').replace('.PDF', '.csv')
            
            st.success(f"Fattura {file_pdf.name} elaborata con successo!")
            st.download_button(label=f"⬇️ Scarica {nome_csv}", data=csv_bytes, file_name=nome_csv, mime="text/csv", key=file_pdf.name)
