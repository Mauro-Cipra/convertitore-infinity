import streamlit as st
import pdfplumber
import re
import csv
import io

st.set_page_config(page_title="Convertitore Infinity", page_icon="🚗")

st.title("🚗 Convertitore Fatture -> CSV Infinity")
st.write("Ufficio Amministrazione: Carica i PDF dell'agenzia Punto Pratiche per generare i CSV.")

file_caricati = st.file_uploader("Trascina qui le fatture in PDF", type="pdf", accept_multiple_files=True)

if file_caricati:
    for file_pdf in file_caricati:
        imponibile_36 = 29.51
        iva_36 = 6.49
        imponibile_33 = 27.05
        iva_33 = 5.95
        dati_csv = []
        
        with pdfplumber.open(file_pdf) as pdf:
            testo_completo = ""
            for pagina in pdf.pages:
                testo_completo += pagina.extract_text() + "\n"
                
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
                 
        output = io.StringIO()
        writer = csv.writer(output, delimiter=';')
        writer.writerows(dati_csv)
        csv_bytes = output.getvalue().encode('utf-8')
        
        nome_csv = file_pdf.name.replace('.pdf', '.csv').replace('.PDF', '.csv')
        
        st.success(f"Fattura {num_fattura} elaborata con successo!")
        st.download_button(label=f"⬇️ Scarica {nome_csv}", data=csv_bytes, file_name=nome_csv, mime="text/csv")
