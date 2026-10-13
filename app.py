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
                    # Normalizzazione blocco per non perdere i dati sparsi
                    blocco_flat = blocco.replace('\n', ' ')
                    
                    targa_match = re.search(r'(?:Targa|Targato)\s+([A-Z0-9]+)', blocco_flat, re.IGNORECASE)
                    if not targa_match:
                        continue
                    targa = targa_match.group(1).upper()
                    
                    # LOGICA A PROVA DI DISORDINE: Cerca tutte le righe del blocco originale per accoppiare gli importi
                    totale_a15 = 0.0
                    totale_22 = 0.0
                    
                    for riga in blocco.split('\n'):
                        numeri = re.findall(r'(\d+[\,\.]\d{2})', riga)
                        if non numeri: continue
                        importo_fine_riga = float(numeri[-1].replace(',', '.'))
                        
                        # Se nella riga o nelle vicinanze ci sono i marcatori IVA 22 o COMPETENZA
                        if "22.00" in riga or "22,00" in riga or "COMPETENZA" in riga.upper():
                            if importo_fine_riga not in [22.0, 1.0]: # Evita l'aliquota stessa e le quantità
                                totale_22 = importo_fine_riga
                        
                        # Altrimenti, se è presente NI/N1 o voci tipiche di esenzione Art.15
                        elif any(kw in riga.upper() for kw in ["NI", "N1", "PRA", "BOLLO", "DIRITTI", "PAGOPA", "IPT"]):
                            if importo_fine_riga != 2.00 and importo_fine_riga != 1.00:
                                totale_a15 += importo_fine_riga
                        
                    iva_22 = totale_22 * 0.22
                    lordo_22 = totale_22 + iva_22
                    
                    # Salva sempre i dati estratti per quel veicolo
                    if totale_22 > 0:
                        dati_csv.append([num_fattura, data_fattura, targa, "", "0,00", f"{totale_22:.2f}".replace('.', ','), f"{iva_22:.2f}".replace('.', ','), ".22", f"{lordo_22:.2f}".replace('.', ',')])
                    else:
                        # Se l'OCR non ha letto l'onorario, inserisce riga a 0 per mantenere la targa
                        dati_csv.append([num_fattura, data_fattura, targa, "", "0,00", "0,00", "0,00", ".22", "0,00"])
                        
                    if totale_a15 > 0:
                        dati_csv.append([num_fattura, data_fattura, targa, "", f"{totale_a15:.2f}".replace('.', ','), "0,00", "0,00", "", "0,00"])

            else:
                 dati_csv.append([num_fattura, data_fattura, "", "", "0,00", "0,00", "0,00", "", "0,00"])

            # Gestione della Marca da Bollo finale (se non calcolata all'interno del veicolo)
            if re.search(r'Marca da bollo.*?\b2[\,\.]00\b', testo_completo, re.IGNORECASE | re.DOTALL) or re.search(r'Bollo assolto.*?\b2[\,\.]00\b', testo_completo, re.IGNORECASE | re.DOTALL):
                 if len(dati_csv) > 0:
                     ultimo_veicolo = dati_csv[-1][2]
                     dati_csv.append([num_fattura, data_fattura, ultimo_veicolo, "", "2,00", "0,00", "0,00", "", "0,00"])
