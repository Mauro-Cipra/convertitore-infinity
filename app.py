import streamlit as st
import pdfplumber
import re
import csv
import io

st.set_page_config(page_title="Convertitore Infinity", page_icon="🚗")

st.title("🚗 Convertitore Fatture -> CSV Infinity")
st.write("Ufficio Amministrazione: Carica i PDF delle fatture. Il sistema riconoscerà in automatico l'agenzia (Punto Pratiche, Elio Gnocchi o Di Lecce Massimiliano).")

file_caricati = st.file_uploader("Trascina qui le fatture in PDF", type="pdf", accept_multiple_files=True)

if file_caricati:
    for file_pdf in file_caricati:
        dati_csv = []
        
        with pdfplumber.open(file_pdf) as pdf:
            testo_completo = ""
            testo_layout = "" 
            for pagina in pdf.pages:
                testo_completo += pagina.extract_text() + "\n"
                testo_layout += pagina.extract_text(layout=True) + "\n"
                
        # --- LOGICA 1: PUNTO PRATICHE (Grafica Standard) ---
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
                if not targa_match or not telaio_match: continue
                    
                targa = targa_match.group(1)
                telaio = telaio_match.group(1)
                
                competenza_lorda, imponibile, iva = ("36,00", imponibile_36, iva_36)
                if "33,00" in pratica:
                    competenza_lorda, imponibile, iva = ("33,00", imponibile_33, iva_33)
                    
                importi_a15 = re.findall(r"a15\s+([\d,]+)", pratica)
                totale_a15 = sum(float(imp.replace(',', '.')) for imp in importi_a15)
                
                dati_csv.append([num_fattura, data_fattura, targa, telaio, "0,00", f"{imponibile:.2f}".replace('.', ','), f"{iva:.2f}".replace('.', ','), ".22", competenza_lorda])
                dati_csv.append([num_fattura, data_fattura, targa, telaio, f"{totale_a15:.2f}".replace('.', ','), "0,00", "0,00", "", "0,00"])
                
            if "2,00" in testo_completo.split("MEF")[1] if "MEF" in testo_completo else "":
                 if len(dati_csv) > 0:
                     dati_csv.append([num_fattura, data_fattura, dati_csv[-1][2], dati_csv[-1][3], "2,00", "0,00", "0,00", "", "0,00"])
        
        # --- LOGICA 2: DI LECCE MASSIMILIANO (XML Tabellare) ---
        elif "DI LECCE MASSIMILIANO" in testo_completo.upper() or "IT01257280774" in testo_completo:
            num_fattura_match = re.search(r"Numero documento.*?(\d+)", testo_completo, re.IGNORECASE | re.DOTALL)
            data_fattura_match = re.search(r"Data documento.*?(\d{2}-\d{2}-\d{4})", testo_completo, re.IGNORECASE | re.DOTALL)
            
            num_fattura = num_fattura_match.group(1)[-4:] if num_fattura_match else "0000"
            data_fattura = data_fattura_match.group(1).replace('-', '/') if data_fattura_match else "01/01/2026"

            # Crea un dizionario associando ogni targa al suo telaio corretto
            mappa_telai = dict(re.findall(r'Targa\s+([A-Z0-9]+)\s+Telaio\s+([A-Z0-9]+)', testo_completo, re.IGNORECASE))

            # Legge riga per riga per non sfalsare le colonne
            righe = testo_layout.split('\n')
            veicolo_corrente = None
            imponibile_corrente = 29.51
            iva_corrente = 6.49
            competenza_lorda = "36,00"
            totale_a15_corrente = 0.0
            
            for riga in righe:
                targa_match = re.search(r'Targa\s+([A-Z0-9]+)', riga)
                if targa_match:
                    if veicolo_corrente:
                        telaio_corrente = mappa_telai.get(veicolo_corrente, "")
                        dati_csv.append([num_fattura, data_fattura, veicolo_corrente, telaio_corrente, "0,00", f"{imponibile_corrente:.2f}".replace('.', ','), f"{iva_corrente:.2f}".replace('.', ','), ".22", competenza_lorda])
                        dati_csv.append([num_fattura, data_fattura, veicolo_corrente, telaio_corrente, f"{totale_a15_corrente:.2f}".replace('.', ','), "0,00", "0,00", "", "0,00"])
                    
                    veicolo_corrente = targa_match.group(1)
                    totale_a15_corrente = 0.0
                    imponibile_corrente = 29.51
                    iva_corrente = 6.49
                    competenza_lorda = "36,00"
                
                # Cerca l'imponibile corretto (29.51 o 18.03) verificando la riga dell'IVA
                if veicolo_corrente and "22,00" in riga:
                    if "18,03" in riga or "18.03" in riga:
                        imponibile_corrente = 18.03
                        iva_corrente = 3.97
                        competenza_lorda = "22,00"
                        
                # Somma i fuori campo IVA solo se sulla riga c'è la sigla N1 o NI
                if veicolo_corrente and re.search(r'\bN[I1]\b', riga, re.IGNORECASE):
                    numeri = re.findall(r'(\d+[\,\.]\d{2})', riga)
                    if numeri:
                        valore = float(numeri[-1].replace(',', '.'))
                        if valore != 2.00:
                            totale_a15_corrente += valore
                            
            if veicolo_corrente:
                telaio_corrente = mappa_telai.get(veicolo_corrente, "")
                dati_csv.append([num_fattura, data_fattura, veicolo_corrente, telaio_corrente, "0,00", f"{imponibile_corrente:.2f}".replace('.', ','), f"{iva_corrente:.2f}".replace('.', ','), ".22", competenza_lorda])
                dati_csv.append([num_fattura, data_fattura, veicolo_corrente, telaio_corrente, f"{totale_a15_corrente:.2f}".replace('.', ','), "0,00", "0,00", "", "0,00"])
                
            # Verifica il bollo di quietanza a fondo fattura
            if re.search(r'Bollo assolto.*?\b2[\,\.]00\b', testo_completo, re.IGNORECASE | re.DOTALL):
                 if len(dati_csv) > 0:
                     ultimo_targa = dati_csv[-1][2]
                     ultimo_telaio = dati_csv[-1][3]
                     dati_csv.append([num_fattura, data_fattura, ultimo_targa, ultimo_telaio, "2,00", "0,00", "0,00", "", "0,00"])

        # --- LOGICA 3: U.P.A. ELIO GNOCCHI ---
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
                     
        else:
            st.warning(f"Fattura {file_pdf.name} non riconosciuta. Assicurati sia di Punto Pratiche, UPA Elio Gnocchi o Di Lecce Massimiliano.")
            continue
            
        output = io.StringIO()
        writer = csv.writer(output, delimiter=';')
        writer.writerows(dati_csv)
        csv_bytes = output.getvalue().encode('utf-8')
        
        nome_csv = file_pdf.name.replace('.pdf', '.csv').replace('.PDF', '.csv')
        
        st.success(f"Fattura elaborata con successo!")
        st.download_button(label=f"⬇️ Scarica {nome_csv}", data=csv_bytes, file_name=nome_csv, mime="text/csv")
