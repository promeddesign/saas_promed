import streamlit as st
import pandas as pd
import json
import os
import re
import datetime
import streamlit.components.v1 as components
from supabase import create_client, Client

st.set_page_config(page_title="OPTIALU", layout="wide")

# ==========================================
# 1. CONNEXION SUPABASE SAAS
# ==========================================
try:
    SUPABASE_URL = st.secrets["supabase"]["url"]
    SUPABASE_KEY = st.secrets["supabase"]["anon_key"]
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    st.sidebar.success("🟢 Connecté à Supabase !")
except Exception as e:
    st.sidebar.error("🔴 Erreur de configuration Supabase. Vérifiez les secrets.")

# ==========================================
# 2. FONCTIONS DE BASE & UTILITAIRES
# ==========================================
def clean_string(s):
    if not s: return ""
    return re.sub(r'\s+', '', str(s)).upper().strip()

def safe_float(val, default=1.0):
    try:
        v = str(val).replace(',', '.').strip()
        if not v or v == '-': return default
        return float(v)
    except: return default

def evaluer_formule(formule, L, H, hC, nom_composant):
    if not formule or str(formule).strip() in ["-", ""]: return 0.0
    f = str(formule).replace('=', '').replace(',', '.').upper().strip()
    f = f.replace('X', '*')
    nom_comp_maj = str(nom_composant).upper()
    if "H" in f:
        if "COUVRE" in nom_comp_maj or "CJ" in nom_comp_maj: f = f.replace("H", str(H))
        else: f = f.replace("H", f"({H} - {hC})")
    if "L" in f: f = f.replace("L", str(L))
    f = re.sub(r'[^0-9\+\-\*\/\(\)\.]', '', f)
    try: return round(float(eval(f)), 1)
    except: return 0.0

def generer_reperes_auto(df):
    c_f = 0; c_p = 0; c_pf = 0; c_o = 0
    new_reperes = []
    for idx, row in df.iterrows():
        ouvr_raw = str(row.get("Ouvrage", "")).strip().upper()
        if not ouvr_raw: new_reperes.append("")
        elif ouvr_raw.startswith("PF"): c_pf += 1; new_reperes.append(f"PF{c_pf}")
        elif ouvr_raw.startswith("P"): c_p += 1; new_reperes.append(f"P{c_p}")
        elif ouvr_raw.startswith("F"): c_f += 1; new_reperes.append(f"F{c_f}")
        else: c_o += 1; new_reperes.append(f"O{c_o}")
    df_out = df.copy()
    df_out["Repère"] = new_reperes
    return df_out

def optimize_cutting_1d_with_ref(cuts_list, bar_length=6000, blade_width=5):
    cuts_sorted = sorted(cuts_list, key=lambda x: x['length'], reverse=True)
    bars = []
    for cut in cuts_sorted:
        placed = False
        for bar in bars:
            occupied = sum(c['length'] for c in bar) + (len(bar) * blade_width)
            if occupied + cut['length'] <= bar_length:
                bar.append(cut)
                placed = True
                break
        if not placed: bars.append([cut])
    return bars

def sanitize_chassis_df(df=None):
    expected_cols = ["Repère", "Gamme", "Série", "Ouvrage", "Largeur (L)", "Hauteur (H)", "Qté", "Volet Roulant", "H Caisson", "Vitrage"]
    if df is None or not isinstance(df, pd.DataFrame):
        df = pd.DataFrame(columns=expected_cols)
    
    for col in expected_cols:
        if col not in df.columns:
            df[col] = None

    df = df[expected_cols].copy()

    df["Repère"] = df["Repère"].fillna("").astype(str)
    df["Gamme"] = df["Gamme"].fillna("-").astype(str)
    df["Série"] = df["Série"].fillna("-").astype(str)
    df["Ouvrage"] = df["Ouvrage"].fillna("-").astype(str)
    df["Largeur (L)"] = pd.to_numeric(df["Largeur (L)"], errors='coerce').fillna(1000.0).astype(float)
    df["Hauteur (H)"] = pd.to_numeric(df["Hauteur (H)"], errors='coerce').fillna(1000.0).astype(float)
    df["Qté"] = pd.to_numeric(df["Qté"], errors='coerce').fillna(1).astype(int)
    
    valid_volets = ["non", "caisson tunnel", "caisson mono-bloc"]
    df["Volet Roulant"] = df["Volet Roulant"].astype(str).apply(lambda x: x if x.lower() in valid_volets else "non")
    df["H Caisson"] = pd.to_numeric(df["H Caisson"], errors='coerce').fillna(0.0).astype(float)
    df["Vitrage"] = df["Vitrage"].fillna("").astype(str)

    return df

def get_default_df():
    return sanitize_chassis_df(pd.DataFrame())

def fetch_entreprise_info(ent_id):
    try:
        ent_res = supabase.table("entreprises").select("nom_entreprise").eq("id", ent_id).execute()
        if ent_res.data and len(ent_res.data) > 0:
            return ent_res.data[0].get("nom_entreprise", "Inconnue")
    except Exception as e:
        print("Erreur fetch_entreprise_info:", e)
    return "Inconnue"

def fetch_project_list():
    try:
        response = supabase.table("projets").select("id, nom_projet").eq("entreprise_id", st.session_state.entreprise_id).execute()
        return response.data  
    except:
        return []

@st.cache_data(ttl=3600) 
def load_app_library():
    try:
        response = supabase.table("bibliotheque_gammes").select("*").execute()
        legacy_data = []
        for item in response.data:
            legacy_data.append({
                "Gamme": item.get("gamme", ""), "Type Ouvrage": item.get("type_ouvrage", ""),
                "Composant": item.get("composant", ""), "Ref": item.get("ref", ""),
                "Formule Long": item.get("formule_long", ""), "Qté": item.get("qte", 1),
                "Unité": item.get("unite", ""), "Type": item.get("type_article", ""),
                "PU": item.get("pu", 0), "Série": item.get("serie", "")
            })
        return legacy_data
    except Exception as e:
        return []

def load_user_prices(entreprise_id):
    prix_dict = {}
    if not entreprise_id:
        return prix_dict
    try:
        res = supabase.table("prix_unitaires").select("ref_composant, prix_unitaire").eq("entreprise_id", entreprise_id).execute()
        for item in res.data:
            ref = str(item.get("ref_composant", "")).strip().upper()
            if ref:
                prix_dict[ref] = float(item.get("prix_unitaire", 0.0))
    except Exception as e:
        print("Erreur chargement prix:", e)
    return prix_dict
def sync_user_prices(entreprise_id, bibliotheque):
    """
    Vérifie la bibliothèque et insère automatiquement les références manquantes 
    dans la table prix_unitaires pour l'entreprise donnée, en nettoyant le nom du composant.
    """
    if not entreprise_id or not bibliotheque:
        return

    try:
        refs_biblio = {}
        for item in bibliotheque:
            ref = str(item.get("Ref", "")).strip().upper()
            
            if ref and ref != "-":
                if ref not in refs_biblio:
                    comp_raw = str(item.get("Composant", "")).strip()
                    comp_clean = re.sub(r'\s+(Largeur|Hauteur|LARGEUR|HAUTEUR)$', '', comp_raw).strip()

                    refs_biblio[ref] = {
                        "gamme": item.get("Gamme", ""),
                        "serie": item.get("Série", ""),
                        "composant": comp_clean, 
                        "type_article": item.get("Type", ""),
                        "unite": item.get("Unité", "U")
                    }

        if not refs_biblio:
            return

        res = supabase.table("prix_unitaires").select("ref_composant").eq("entreprise_id", entreprise_id).execute()
        refs_existantes = {str(row["ref_composant"]).strip().upper() for row in res.data if row.get("ref_composant")}

        lignes_a_inserer = []
        for ref, data in refs_biblio.items():
            if ref not in refs_existantes:
                lignes_a_inserer.append({
                    "entreprise_id": entreprise_id,
                    "ref_composant": ref,
                    "gamme": data["gamme"],
                    "serie": data["serie"],
                    "composant": data["composant"],
                    "type_article": data["type_article"],
                    "unite": data["unite"],
                    "prix_unitaire": 0.0  
                })

        if lignes_a_inserer:
            supabase.table("prix_unitaires").insert(lignes_a_inserer).execute()
            if "prix_entreprise" in st.session_state:
                del st.session_state["prix_entreprise"]

    except Exception as e:
        print("Erreur synchronisation prix_unitaires :", e)
def logout():
    supabase.auth.sign_out()
    for key in ["user", "access_token", "refresh_token", "entreprise_id", "user_nom", "nom_entreprise"]:
        st.session_state[key] = None
    st.cache_data.clear() 
    st.rerun()

# ==========================================
# 3. INITIALISATION DES VARIABLES DE SESSION
# ==========================================
for key in ["user", "access_token", "refresh_token", "entreprise_id", "user_nom", "nom_entreprise"]:
    if key not in st.session_state:
        st.session_state[key] = None

if "chassis_rows_v27" not in st.session_state:
    st.session_state.chassis_rows_v27 = get_default_df()
if "current_project_name" not in st.session_state:
    st.session_state.current_project_name = "Nouveau Projet (Non Sauvegardé)"
if "current_project_id" not in st.session_state:
    st.session_state.current_project_id = None
if "df_garde_corps" not in st.session_state:
    st.session_state.df_garde_corps = pd.DataFrame([
        {"Emplacement / Réf": "Balcon RDC", "Longueur (mm)": 2500, "Quantité": 2},
        {"Emplacement / Réf": "Terrasse Étage", "Longueur (mm)": 1800, "Quantité": 1},
        {"Emplacement / Réf": "Escalier", "Longueur (mm)": 950, "Quantité": 5}
    ])

# --- Variables pour le Devis Global ---
for key in ["total_alu", "total_vitrage", "total_accessoires", "total_volets", "total_gardecorps"]:
    if key not in st.session_state:
        st.session_state[key] = 0.0

# ==========================================
# 4. GESTION DE L'AUTHENTIFICATION
# ==========================================
if st.session_state.access_token and st.session_state.refresh_token:
    try:
        supabase.auth.set_session(st.session_state.access_token, st.session_state.refresh_token)
        if not st.session_state.get('nom_entreprise') or not st.session_state.get('entreprise_id'):
            user_id = st.session_state.user.id if st.session_state.user else None
            if user_id:
                profile_res = supabase.table("profiles").select("entreprise_id", "nom").eq("id", user_id).execute()
                if profile_res.data:
                    st.session_state.entreprise_id = profile_res.data[0].get("entreprise_id")
                    st.session_state.user_nom = profile_res.data[0].get("nom", "Utilisateur")
                    if st.session_state.entreprise_id:
                        st.session_state.nom_entreprise = fetch_entreprise_info(st.session_state.entreprise_id)
    except:
        st.session_state.user = None

if st.session_state.user is None:
    st.markdown('<div class="main-title">🔐 Connexion OPTIALU</div>', unsafe_allow_html=True)
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.info("Veuillez vous connecter pour accéder à l'espace de votre entreprise.")
        email = st.text_input("Adresse E-mail")
        password = st.text_input("Mot de passe", type="password")
        if st.button("Se connecter", type="primary", use_container_width=True):
            try:
                response = supabase.auth.sign_in_with_password({"email": email, "password": password})
                st.session_state.user = response.user
                st.session_state.access_token = response.session.access_token
                st.session_state.refresh_token = response.session.refresh_token
                
                user_id = response.user.id
                profile_res = supabase.table("profiles").select("entreprise_id", "nom").eq("id", user_id).execute()
                
                if profile_res.data:
                    st.session_state.entreprise_id = profile_res.data[0]["entreprise_id"]
                    st.session_state.user_nom = profile_res.data[0].get("nom", "Utilisateur")
                    st.session_state.nom_entreprise = fetch_entreprise_info(st.session_state.entreprise_id)
                    st.cache_data.clear() 
                    st.rerun()
                else:
                    st.error("🔴 Votre compte n'est lié à aucune entreprise.")
                    supabase.auth.sign_out() 
                    st.session_state.user = None
            except Exception as e:
                st.error(f"🔴 Erreur détaillée : {e}")
    st.stop() 

# ==========================================
# 5. UTILISATEUR CONNECTÉ - CHARGEMENT DONNÉES
# ==========================================
st.markdown('<div class="main-title">OPTIALU</div>', unsafe_allow_html=True)

nom_ent_affiche = st.session_state.get('nom_entreprise') or "Inconnue"
nom_usr_affiche = st.session_state.get('user_nom') or "Utilisateur"

st.markdown(
    f"""
    <div style="background-color: #f0f2f6; padding: 12px 20px; border-radius: 8px; margin-bottom: 25px; display: flex; justify-content: space-between; align-items: center; border-left: 5px solid #1E3A8A;">
        <span style="font-size: 16px; color: #333;">🏢 Entreprise : <strong>{nom_ent_affiche}</strong></span>
        <span style="font-size: 16px; color: #333;">👤 Utilisateur : <strong>{nom_usr_affiche}</strong></span>
    </div>
    """, unsafe_allow_html=True
)

st.sidebar.button("🚪 Se déconnecter", on_click=logout, use_container_width=True)
st.sidebar.markdown("---")

BIBLIOTHEQUE = load_app_library()
if "prix_entreprise" not in st.session_state:
    st.session_state.prix_entreprise = load_user_prices(st.session_state.entreprise_id)

PALETTE_COULEURS = ["#1E40AF", "#10B981", "#D97706", "#DC2626", "#7C3AED", "#0891B2", "#EC4899"]

choix_gammes_dynamiques = sorted(list(set([str(x.get("Gamme", "")).strip() for x in BIBLIOTHEQUE if str(x.get("Gamme", "")).strip() != ""])))
choix_series_dynamiques = sorted(list(set([str(x.get("Série", "")).strip() for x in BIBLIOTHEQUE if str(x.get("Série", "")).strip() != ""])))
choix_types_dynamiques = sorted(list(set([str(x.get("Type Ouvrage", "")).strip() for x in BIBLIOTHEQUE if str(x.get("Type Ouvrage", "")).strip() != ""])))

if not choix_gammes_dynamiques: choix_gammes_dynamiques = ["-"]
if not choix_series_dynamiques: choix_series_dynamiques = ["-"]
if not choix_types_dynamiques: choix_types_dynamiques = ["-"]

NOM_PROJET = st.session_state.current_project_name
DATE_DU_JOUR = datetime.datetime.now().strftime("%d-%m-%Y")

# --- CSS ---
st.markdown("""
    <style>
    .main-title { font-size:24px !important; font-weight: bold; color: #1E3A8A; margin-bottom: 20px; text-align: center; border-bottom: 3px solid #1E3A8A; padding-bottom: 10px;}
    .projet-title { font-size: 24px; font-weight: bold; color: #DC2626; text-align: center; margin-bottom: 20px; text-transform: uppercase;}
    .section-header { font-size:18px !important; font-weight: bold; color: #0F172A; margin-top: 20px; margin-bottom: 10px; padding: 6px 0;}
    .excel-head-yellow { background-color: #FEF08A; color: #713F12; padding: 8px; font-weight: bold; border-radius: 4px; margin-bottom: 10px; font-size: 15px;}
    .excel-head-blue { background-color: #DBEAFE; color: #1E40AF; padding: 8px; font-weight: bold; border-radius: 4px; margin-bottom: 10px; font-size: 15px; text-transform: uppercase;}
    .excel-head-green { background-color: #DCFCE7; color: #14532D; padding: 8px; font-weight: bold; border-radius: 4px; margin-bottom: 10px; font-size: 15px;}
    .excel-head-gray { background-color: #F3F4F6; color: #374151; padding: 8px; font-weight: bold; border-radius: 4px; margin-bottom: 10px; font-size: 15px; border: 1px solid #D1D5DB;}
    .badge-serie { background-color: #4B5563; color: white; font-weight: bold; padding: 8px 12px; border-radius: 4px 4px 0 0; display: inline-block; margin-top: 20px; font-size: 14px; -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important;}
    .print-table { width: 100%; border-collapse: collapse; margin-bottom: 20px; font-size: 13px; font-family: sans-serif; }
    .print-table th, .print-table td { border: 1px solid #9CA3AF; padding: 6px; text-align: left; vertical-align: middle; }
    .print-table th { background-color: #F3F4F6; color: #111827; font-weight: bold; -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important;}
    .print-table th.yellow-head { background-color: #FEF08A !important; color: #111827 !important; text-align: center; vertical-align: bottom; }
    .print-table td.center-text { text-align: center; font-weight: 500; }
    .bar-container { display: flex; background-color: #F3F4F6; border: 1px solid #4B5563; border-radius: 2px; height: 32px; width: 100%; overflow: hidden; box-sizing: border-box; margin: 0;}
    .bar-segment { display: flex; align-items: center; justify-content: center; color: white; font-weight: bold; font-size: 12px; height: 100%; border-right: 1px solid #FFFFFF !important; box-sizing: border-box; white-space: nowrap; overflow: hidden; -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important;}
    .bar-chute { display: flex; align-items: center; justify-content: center; background-color: #E5E7EB; color: #6B7280; font-size: 11px; height: 100%; box-sizing: border-box; flex-grow: 1; -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important;}
    .block-spacer { margin-top: 40px; }
    .print-only { display: none; }
    @media print {
        .print-only { display: block !important; margin-bottom: 20px; }
        header, [data-testid="stSidebar"], [role="tablist"], [data-testid="stDataEditor"], iframe, .main-title { display: none !important; }
        .no-print { display: none !important; }
        @page { size: A4; margin: 0mm; }
        .main .block-container { max-width: 100% !important; padding: 10mm !important; margin: 0 !important; }
    }
    </style>
""", unsafe_allow_html=True)

# ==========================================
# 6. MENU LATÉRAL - GESTION PROJETS
# ==========================================
st.sidebar.header("📁 Gestion des Projets")
st.session_state.liste_projets_sauvegardes = fetch_project_list()
projets_existants = st.session_state.liste_projets_sauvegardes

nouveau_projet = st.sidebar.text_input("➕ Créer un nouveau projet :", placeholder="Ex: Villa Dupont")
if st.sidebar.button("Créer ce projet", use_container_width=True):
    if nouveau_projet:
        chassis_json = json.loads(st.session_state.chassis_rows_v27.to_json(orient="records", force_ascii=False))
        gc_json = json.loads(st.session_state.df_garde_corps.to_json(orient="records", force_ascii=False))
        data_json = {"chassis": chassis_json, "garde_corps": gc_json}
        try:
            response = supabase.table("projets").insert({
                "user_id": st.session_state.user.id, "entreprise_id": st.session_state.entreprise_id, 
                "nom_projet": nouveau_projet, "donnees": data_json
            }).execute()
            if response.data:
                st.session_state.current_project_id = response.data[0]["id"]
                st.session_state.current_project_name = nouveau_projet
                st.session_state.chassis_rows_v27 = get_default_df()
                st.session_state.liste_projets_sauvegardes = fetch_project_list()
                st.sidebar.success(f"Projet '{nouveau_projet}' créé !")
                st.rerun()
        except: pass
st.sidebar.markdown("---")

projet_options = {p["nom_projet"]: p["id"] for p in projets_existants}
projet_selectionne = st.sidebar.selectbox("📂 Projets existants :", ["-- Sélectionner --"] + list(projet_options.keys()))

if st.sidebar.button("Charger ce projet", use_container_width=True):
    if projet_selectionne != "-- Sélectionner --":
        target_id = projet_options[projet_selectionne]
        try:
            response = supabase.table("projets").select("donnees").eq("id", target_id).eq("entreprise_id", st.session_state.entreprise_id).execute()
            if response.data:
                raw_data = response.data[0]["donnees"]
                if isinstance(raw_data, dict) and "chassis" in raw_data:
                    df_charge = pd.DataFrame(raw_data["chassis"])
                    df_gc_charge = pd.DataFrame(raw_data.get("garde_corps", []))
                    if not df_gc_charge.empty:
                        st.session_state.df_garde_corps = df_gc_charge
                else:
                    df_charge = pd.DataFrame(raw_data)
                
                df_charge = sanitize_chassis_df(df_charge)
                
                st.session_state.chassis_rows_v27 = df_charge
                st.session_state.current_project_name = projet_selectionne
                st.session_state.current_project_id = target_id
                st.rerun()
        except Exception as e: 
            st.sidebar.error(f"Erreur de chargement: {e}")
st.sidebar.markdown("---")

st.sidebar.info(f"Projet actif : **{st.session_state.current_project_name}**")
if st.sidebar.button("💾 SAUVEGARDER LES MODIFICATIONS", type="primary", use_container_width=True):
    if st.session_state.current_project_id is not None:
        try:
            chassis_json = json.loads(st.session_state.chassis_rows_v27.to_json(orient="records", force_ascii=False))
            gc_json = json.loads(st.session_state.df_garde_corps.to_json(orient="records", force_ascii=False))
            data_json = {"chassis": chassis_json, "garde_corps": gc_json}
            supabase.table("projets").update({"donnees": data_json}).eq("id", st.session_state.current_project_id).eq("entreprise_id", st.session_state.entreprise_id).execute()
            st.sidebar.success("Projet sauvegardé avec succès !")
        except Exception as e: 
            st.sidebar.error(f"Erreur de sauvegarde: {e}")

st.sidebar.markdown("---")
st.sidebar.header("⚙️ Navigation")

menu_selection = st.sidebar.radio(
    "Modules :",
    [
        "📝 Saisie des Ouvrages", 
        "📐 Fiche Atelier & Débit", 
        "🪟 Carnet de Vitrage", 
        "🛒 Quincaillerie & Joints", 
        "🏠 Volets Roulants", 
        "🚧 Garde-corps (Barres 6m)",
        "🛠️ Gestionnaire de Bibliothèque", 
        "💰 Mes Prix Unitaires",
        "📊 Devis Global du Projet"
    ]
)

# ==========================================
# 7. ROUTAGE DES MODULES
# ==========================================
if menu_selection == "📝 Saisie des Ouvrages":
    st.markdown(f'<div class="section-header no-print">📝 Saisie des Ouvrages — {NOM_PROJET}</div>', unsafe_allow_html=True)
    global_gammes = sorted(list(set([str(x.get("Gamme", "")).strip() for x in BIBLIOTHEQUE if str(x.get("Gamme", "")).strip() != ""])))
    global_series = sorted(list(set([str(x.get("Série", "")).strip() for x in BIBLIOTHEQUE if str(x.get("Série", "")).strip() != ""])))
    global_ouvrages = sorted(list(set([str(x.get("Type Ouvrage", "")).strip() for x in BIBLIOTHEQUE if str(x.get("Type Ouvrage", "")).strip() != ""])))

    if not global_gammes: global_gammes = ["-"]
    if not global_series: global_series = ["-"]
    if not global_ouvrages: global_ouvrages = ["-"]

    st.markdown("### ⚙️ 1. Choix du Modèle")
    colA, colB, colC, col_img = st.columns([2, 2, 2, 1]) 
    sel_gamme = colA.selectbox("Gamme", options=global_gammes)
    biblio_gamme = [x for x in BIBLIOTHEQUE if str(x.get("Gamme", "")).strip() == sel_gamme]
    choix_series_dyn = sorted(list(set([str(x.get("Série", "")).strip() for x in biblio_gamme if str(x.get("Série", "")).strip() != ""])))
    if not choix_series_dyn: choix_series_dyn = ["-"]
    sel_serie = colB.selectbox("Série", options=choix_series_dyn)
    biblio_serie = [x for x in biblio_gamme if str(x.get("Série", "")).strip() == sel_serie]
    choix_ouvrages_dyn = sorted(list(set([str(x.get("Type Ouvrage", "")).strip() for x in biblio_serie if str(x.get("Type Ouvrage", "")).strip() != ""])))
    if not choix_ouvrages_dyn: choix_ouvrages_dyn = ["-"]
    sel_ouvrage = colC.selectbox("Type d'Ouvrage", options=choix_ouvrages_dyn)

    with col_img:
        images_ouvrages = {
            "F CM 3V": "images/f_cm3v.png", "F O 1V": "images/f_o1v.png", "F C 1V": "images/f_o1v.png", 
            "F O 2V": "images/f_o2v.png", "F C 2V": "images/f_c2v.png", "F C 3V": "images/f_c3v.png",
            "P O 1V": "images/p_o1v.png", "P O 2V": "images/p_o2v.png", "PF O 2V": "images/pf_o2v.png", "PF C 2V": "images/pf_o2v.png"
        }
        img_par_defaut = "https://cdn-icons-png.flaticon.com/512/1085/1085695.png" 
        url_img = images_ouvrages.get(sel_ouvrage, img_par_defaut)
        st.write("")
        try: st.image(url_img, width=100)
        except: st.image(img_par_defaut, width=100)

    st.markdown("### ⚡ 2. Dimensions & Ajout rapide")

    # JS : Entrée = passer au champ suivant (comme Tab)
    components.html("""
    <script>
    (function() {
        function setupEnterNav() {
            var doc = window.parent.document;
            var inputs = Array.from(doc.querySelectorAll(
                'input[type="number"], input[type="text"], input[aria-label]'
            )).filter(function(el) {
                return el.offsetParent !== null;  // visible seulement
            });

            inputs.forEach(function(input, idx) {
                if (input.dataset.enterNav) return;
                input.dataset.enterNav = "1";
                input.addEventListener("keydown", function(e) {
                    if (e.key === "Enter") {
                        e.preventDefault();
                        e.stopPropagation();
                        var allInputs = Array.from(doc.querySelectorAll(
                            'input[type="number"], input[type="text"]'
                        )).filter(function(el) { return el.offsetParent !== null; });
                        var cur = allInputs.indexOf(document.activeElement !== null ? doc.activeElement : this);
                        if (cur === -1) cur = allInputs.indexOf(this);
                        var next = allInputs[cur + 1];
                        if (next) { next.focus(); next.select(); }
                    }
                });
            });
        }

        // Lancer au chargement et re-lancer périodiquement (Streamlit rerend le DOM)
        setupEnterNav();
        var _interval = setInterval(setupEnterNav, 800);
        setTimeout(function() { clearInterval(_interval); }, 15000);
    })();
    </script>
    """, height=0)

    col1, col2, col3 = st.columns(3)
    with col1:
        n_largeur = st.number_input("Largeur (L) mm", min_value=100.0, value=1000.0, step=10.0, key="inp_largeur")
        n_hauteur = st.number_input("Hauteur (H) mm", min_value=100.0, value=1000.0, step=10.0, key="inp_hauteur")
    with col2:
        n_qte = st.number_input("Quantité", min_value=1, value=1, step=1, key="inp_qte")
        n_vitrage = st.text_input("Vitrage", placeholder="ex: 4/16/4", key="inp_vitrage")
    with col3:
        n_volet = st.selectbox("Volet Roulant", options=["non", "caisson tunnel", "caisson mono-bloc"], key="inp_volet")
        n_h_caisson = st.number_input("H Caisson mm (si applicable)", min_value=0.0, value=0.0, step=10.0, key="inp_h_caisson")

    submit_ajout = st.button("➕ Ajouter ce châssis au projet", type="primary", use_container_width=True)

    if submit_ajout:
        nouvelle_ligne = pd.DataFrame([{
            "Repère": "", "Gamme": sel_gamme, "Série": sel_serie, "Ouvrage": sel_ouvrage,
            "Largeur (L)": float(st.session_state.inp_largeur),
            "Hauteur (H)": float(st.session_state.inp_hauteur),
            "Qté": int(st.session_state.inp_qte),
            "Volet Roulant": st.session_state.inp_volet,
            "H Caisson": float(st.session_state.inp_h_caisson),
            "Vitrage": st.session_state.inp_vitrage
        }])
        if "Gamme" not in st.session_state.chassis_rows_v27.columns:
            st.session_state.chassis_rows_v27["Gamme"] = sel_gamme
            st.session_state.chassis_rows_v27["Série"] = sel_serie
        st.session_state.chassis_rows_v27 = pd.concat([st.session_state.chassis_rows_v27, nouvelle_ligne], ignore_index=True)
        st.session_state.chassis_rows_v27 = generer_reperes_auto(st.session_state.chassis_rows_v27)
        st.rerun()

    st.markdown("---")
    st.markdown("### 📋 Listing des châssis (Modifiable)")
    
    # Sécuriser les options pour st.data_editor (évite les incompatibilités de types ou valeurs absentes des selectbox)
    st.session_state.chassis_rows_v27 = sanitize_chassis_df(st.session_state.chassis_rows_v27)
    
    opts_gammes = sorted(list(set(global_gammes + [x for x in st.session_state.chassis_rows_v27["Gamme"].unique() if x and x != "-"])))
    opts_series = sorted(list(set(global_series + [x for x in st.session_state.chassis_rows_v27["Série"].unique() if x and x != "-"])))
    opts_ouvrages = sorted(list(set(global_ouvrages + [x for x in st.session_state.chassis_rows_v27["Ouvrage"].unique() if x and x != "-"])))

    if not opts_gammes: opts_gammes = ["-"]
    if not opts_series: opts_series = ["-"]
    if not opts_ouvrages: opts_ouvrages = ["-"]

    edited_df = st.data_editor(
        st.session_state.chassis_rows_v27,
        num_rows="dynamic",
        column_config={
            "Repère": st.column_config.TextColumn("N° (Auto)", disabled=True, width="small"),
            "Gamme": st.column_config.SelectboxColumn("Gamme", options=opts_gammes),
            "Série": st.column_config.SelectboxColumn("Série", options=opts_series),
            "Ouvrage": st.column_config.SelectboxColumn("Ouvrage", options=opts_ouvrages),
            "Volet Roulant": st.column_config.SelectboxColumn(options=["non", "caisson tunnel", "caisson mono-bloc"]),
            "Vitrage": st.column_config.TextColumn("Vitrage (ex: 4/16/4)"),
        },
        use_container_width=True, key="project_editor_v27"
    )
    
    df_auto_calcule = generer_reperes_auto(edited_df)
    if len(edited_df) != len(st.session_state.chassis_rows_v27) or not edited_df["Repère"].equals(df_auto_calcule["Repère"]):
        st.session_state.chassis_rows_v27 = df_auto_calcule
        st.rerun()

elif menu_selection == "📐 Fiche Atelier & Débit":
    st.markdown('<div class="section-header no-print">📏 Configuration de Coupe</div>', unsafe_allow_html=True)
    col1, col2 = st.columns(2)
    with col1: LONGUEUR_BRUTE = st.number_input("Longueur brute de barre (mm)", value=6500)
    with col2: EPAISSEUR_SCIE = st.number_input("Trait de scie (mm)", value=5)
    btn_generer = st.button("⚡ GENERER LES PARCELLES COLORÉES", type="primary", use_container_width=True)
    
    if btn_generer:
        st.session_state.afficher_resultats_debit = True

    if st.session_state.get("afficher_resultats_debit", False):
        edited_project = st.session_state.chassis_rows_v27
        dict_global_coupes = {}
        lignes_fiche_atelier = []
        match_trouve = False
        list_reperes = [str(row.get("Repère", "")).strip() for idx, row in edited_project.iterrows() if str(row.get("Repère", "")).strip() != ""]
        reperes_uniques = list(set(list_reperes))
        map_couleurs = {rep: PALETTE_COULEURS[idx % len(PALETTE_COULEURS)] for idx, rep in enumerate(reperes_uniques)}

        for index, row in edited_project.iterrows():
            type_ouvrage = str(row.get("Ouvrage", "")).strip()
            repere = str(row.get("Repère", "")).strip()
            if not type_ouvrage or not repere or row["Qté"] <= 0: continue
            L = float(row["Largeur (L)"])
            H = float(row["Hauteur (H)"])
            qte_ouvrage = int(row["Qté"])
            a_volet = str(row.get("Volet Roulant", "non")).lower()
            h_caisson = float(row.get("H Caisson", 0.0)) if a_volet == "caisson mono-bloc" else 0.0
            repere_qte_display = f"{repere} / {qte_ouvrage}<br><span style='font-size:11px; font-weight:normal; color:#374151;'>{type_ouvrage}</span>"
            
            for comp in BIBLIOTHEQUE:
                if clean_string(comp.get("Type Ouvrage", "")) == clean_string(type_ouvrage):
                    type_article = str(comp.get("Type", "")).strip().lower()
                    if type_article == "barre":
                        formule_brute = str(comp.get("Formule Long", "-")).strip()
                        ref_profil = str(comp.get("Ref", "INCONNU")).strip().upper()
                        la_serie = str(comp.get("Série", "SANS_SERIE")).strip()
                        designation_profil = str(comp.get("Composant", "")).strip()
                        qte_comp = safe_float(comp.get("Qté", 1), 1.0)
                        qte_totale_morceaux = int(qte_comp * qte_ouvrage)
                        longueur_coupe = evaluer_formule(formule_brute, L, H, h_caisson, designation_profil)
                        if longueur_coupe <= 0: continue
                        match_trouve = True
                        f_upper = formule_brute.upper()
                        if "H" in f_upper and "L" not in f_upper: orientation = "H"
                        elif "L" in f_upper and "H" not in f_upper: orientation = "L"
                        else: orientation = "Mix"
                        ref_display = f"{orientation}-{ref_profil}" if orientation != "Mix" else ref_profil
                        col_header_riche = f'<span style="font-size:10px; font-weight:normal; display:block; border-bottom:1px solid #713F12; padding-bottom:2px; margin-bottom:2px;">{designation_profil}</span>{ref_display}'
                        cle_ref = (la_serie, ref_profil)
                        if cle_ref not in dict_global_coupes: dict_global_coupes[cle_ref] = []
                        for _ in range(qte_totale_morceaux):
                            dict_global_coupes[cle_ref].append({"longueur": longueur_coupe, "repere": repere, "composant": designation_profil})
                        lignes_fiche_atelier.append({
                            "Série": la_serie, "Repère/Qté": repere_qte_display,
                            "ColHeader": col_header_riche, "Valeur": f"{int(longueur_coupe)}/{qte_totale_morceaux}"
                        })

        if match_trouve:
            st.markdown('<div class="section-header no-print">📐 Visualisation Documents d\'Atelier</div>', unsafe_allow_html=True)
            titre_pdf = f"{NOM_PROJET}_Fiche_Atelier_{DATE_DU_JOUR}".replace(" ", "_").replace("'", "")
            components.html(f"""
                <button onclick="
                    var oldTitle = window.parent.document.title; 
                    window.parent.document.title = '{titre_pdf}'; 
                    setTimeout(function(){{ window.parent.print(); }}, 100);
                    setTimeout(function(){{ window.parent.document.title = oldTitle; }}, 2000);
                " style="background-color: #EF4444; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; font-size: 14px; width: 100%;">
                🖨️ IMPRIMER LA FICHE D'ATELIER (Enregistrer en PDF)
                </button>
            """, height=60)

            st.markdown(f'<div class="projet-title">PROJET : {NOM_PROJET}</div>', unsafe_allow_html=True)
            st.markdown('<div class="excel-head-yellow">DETAIL PAR REPERE CHASSIS</div>', unsafe_allow_html=True)
            df_lignes = pd.DataFrame(lignes_fiche_atelier)
            if not df_lignes.empty:
                for serie in df_lignes['Série'].unique():
                    df_serie = df_lignes[df_lignes['Série'] == serie]
                    df_pivot = df_serie.pivot_table(index='Repère/Qté', columns='ColHeader', values='Valeur', aggfunc=lambda x: ' + '.join(x)).fillna("")
                    html_pivot = f'<div class="badge-serie">DÉBIT PAR CHÂSSIS :<br>{serie.upper()}</div><table class="print-table" style="margin-top: 0;"><thead><tr><th class="yellow-head" style="width: 120px;">REPÈRE / Qté<br><span style="font-size:10px; font-weight:normal;">Type Ouvrage</span></th>'
                    for col in df_pivot.columns: html_pivot += f'<th class="yellow-head">{col}</th>'
                    html_pivot += "</tr></thead><tbody>"
                    for rep, row_data in df_pivot.iterrows():
                        html_pivot += f'<tr><td style="font-weight: bold; text-align: center;">{rep}</td>'
                        for col in df_pivot.columns: html_pivot += f'<td class="center-text">{row_data[col]}</td>'
                        html_pivot += "</tr>"
                    html_pivot += "</tbody></table>"
                    st.markdown(html_pivot.replace('\n', ''), unsafe_allow_html=True)

            st.markdown('<div class="block-spacer"></div>', unsafe_allow_html=True)
            st.markdown('<div class="excel-head-blue">✂️ OPTIMISATION ET PARCELLES COLORÉES DE COUPE</div>', unsafe_allow_html=True)
            html_coupes = '<table class="print-table" style="width: 100%;"><thead><tr><th style="width: 12%;">Référence</th><th style="width: 58%;">Plan Visualisé (Longueur Utile + Chute)</th><th style="width: 8%;" class="center-text">Qté</th><th style="width: 8%;" class="center-text">Utile</th><th style="width: 7%;" class="center-text">Chute</th><th style="width: 7%;" class="center-text">% Perte</th></tr></thead><tbody>'
            dict_total_barres_achetees = {}
            for (serie, ref), coupes in sorted(dict_global_coupes.items(), key=lambda x: (x[0][0], x[0][1])):
                coupes_triees = sorted(coupes, key=lambda x: x["longueur"], reverse=True)
                barres_brutes = []
                for c in coupes_triees:
                    place_trouvee = False
                    for b in barres_brutes:
                        espace_occupe = sum([m["longueur"] for m in b]) + (len(b) * EPAISSEUR_SCIE)
                        if (c["longueur"] + EPAISSEUR_SCIE) <= (LONGUEUR_BRUTE - espace_occupe):
                            b.append(c); place_trouvee = True; break
                    if not place_trouvee: barres_brutes.append([c])
                dict_total_barres_achetees[(serie, ref)] = len(barres_brutes)
                grouped_bars = []
                for b in barres_brutes:
                    matched = False
                    for gb in grouped_bars:
                        if len(b) == len(gb['pieces']):
                            is_identical = True
                            for p1, p2 in zip(b, gb['pieces']):
                                if p1['longueur'] != p2['longueur'] or p1['repere'] != p2['repere']: is_identical = False; break
                            if is_identical: gb['qty'] += 1; matched = True; break
                    if not matched: grouped_bars.append({'pieces': b, 'qty': 1})
                total_barres_pour_ref = 0
                b_idx = 1
                for gb in grouped_bars:
                    barre = gb['pieces']; qte_barre = gb['qty']; total_barres_pour_ref += qte_barre
                    somme_profils = sum([m["longueur"] for m in barre]); somme_scies = len(barre) * EPAISSEUR_SCIE
                    total_consomme = somme_profils + somme_scies; chute_restante = max(0, LONGUEUR_BRUTE - total_consomme)
                    pct_perte = (chute_restante / LONGUEUR_BRUTE) * 100
                    html_barre_div = '<div class="bar-container">'
                    for morceau in barre:
                        moceau_lg = morceau["longueur"]; rep = morceau["repere"]; comp_name = morceau["composant"]
                        couleur = map_couleurs.get(rep, "#3B82F6")
                        pct_largeur = ((moceau_lg + EPAISSEUR_SCIE) / LONGUEUR_BRUTE) * 100
                        html_barre_div += f'<div class="bar-segment" style="width: {pct_largeur}%; background-color: {couleur};" title="{rep} - {comp_name} ({int(moceau_lg)}mm)">{int(moceau_lg)}</div>'
                    if chute_restante > 0: html_barre_div += f'<div class="bar-chute" style="width: {(chute_restante/LONGUEUR_BRUTE)*100}%;"></div>'
                    html_barre_div += '</div>'
                    html_coupes += f'<tr><td class="center-text">{ref} (B{b_idx})</td><td style="padding: 4px;">{html_barre_div}</td><td class="center-text" style="font-weight: bold;">{qte_barre}</td><td class="center-text">{int(total_consomme)}</td><td class="center-text">{int(chute_restante)}</td><td class="center-text">{pct_perte:.1f}%</td></tr>'
                    b_idx += 1
                html_coupes += f'<tr style="background-color: #F9FAFB; font-weight: bold; border-bottom: 2px solid #D1D5DB;"><td>TOTAL {ref}</td><td colspan="5">{total_barres_pour_ref} Barre(s) ({serie.upper()}) de {int(LONGUEUR_BRUTE)} mm</td></tr>'
            html_coupes += "</tbody></table>"
            st.markdown(html_coupes.replace('\n', ''), unsafe_allow_html=True)
            
            st.markdown('<div class="block-spacer"></div>', unsafe_allow_html=True)
            st.markdown('<div class="excel-head-green">📦 RÉCAPITULATIF DE COMMANDE DES PROFILÉS</div>', unsafe_allow_html=True)
            html_recap = '<table class="print-table" style="width: 100%;"><thead><tr><th>Série</th><th>Référence Alu</th><th class="center-text">Total Barres (' + str(LONGUEUR_BRUTE/1000) + 'm)</th><th class="center-text">Prix Unitaire Barre (DA)</th><th class="center-text">Total HT (DA)</th></tr></thead><tbody>'
            
            # --- CALCUL DU DEVIS ALU ---
            st.session_state.total_alu = 0.0
            list_profils_commande = []
            for (serie, ref), qte_b in dict_total_barres_achetees.items():
                pu_barre = st.session_state.prix_entreprise.get(ref, 0.0)
                tot_profil_ht = qte_b * pu_barre
                st.session_state.total_alu += tot_profil_ht
                list_profils_commande.append({
                    "Série": serie, "Référence": ref, "Quantité Barres": qte_b,
                    "Prix Unitaire (DA)": pu_barre, "Total HT (DA)": tot_profil_ht
                })
                html_recap += f"<tr><td>{serie}</td><td><b>{ref}</b></td><td class='center-text' style='font-weight: bold;'>{qte_b}</td><td class='center-text'>{pu_barre:,.2f} DA</td><td class='center-text' style='font-weight: bold; color:#047857;'>{tot_profil_ht:,.2f} DA</td></tr>"
            
            html_recap += f'<tr style="background-color: #D1FAE5; font-weight: bold;"><td colspan="4" style="text-align: right;">TOTAL COMMANDES PROFILÉS :</td><td class="center-text" style="color:#065F46; font-size:16px;">{st.session_state.total_alu:,.2f} DA</td></tr>'
            html_recap += "</tbody></table>"
            st.session_state.list_profils_commande = list_profils_commande
            st.markdown(html_recap.replace('\n', ''), unsafe_allow_html=True)
        else:
            st.error("⚠️ Aucun profilé de type 'Barre' trouvé pour cet ouvrage.")

elif menu_selection == "🪟 Carnet de Vitrage":
    st.markdown('<div class="section-header no-print">🪟 Carnet de Vitrage (Miroitier)</div>', unsafe_allow_html=True)
    
    # 1. Collecter tous les types de vitrage uniques utilisés dans le projet
    types_vitrages_projet = set()
    for index, row in st.session_state.chassis_rows_v27.iterrows():
        v_saisi = str(row.get("Vitrage", "")).strip()
        if v_saisi and v_saisi != "-":
            types_vitrages_projet.add(v_saisi)
        else:
            types_vitrages_projet.add("Standard")

    st.markdown("### 💰 1. Saisie des Prix Unitaires du Vitrage (par m²)")
    prix_vitrages_saisis = {}
    if types_vitrages_projet:
        cols_v = st.columns(min(len(types_vitrages_projet), 4))
        for idx, t_vitre in enumerate(sorted(list(types_vitrages_projet))):
            col_cible = cols_v[idx % len(cols_v)]
            with col_cible:
                # On récupère un prix par défaut s'il existe déjà en session
                def_p = st.session_state.get(f"pu_vitre_{t_vitre}", 0.0)
                prix_vitrages_saisis[t_vitre] = st.number_input(f"Prix m² — {t_vitre} (DA)", min_value=0.0, value=def_p, step=100.0, key=f"pu_vitre_{t_vitre}")
    else:
        st.info("Aucun type de vitrage spécifique détecté dans les châssis.")

    btn_calculer_vitrage = st.button("🪟 CALCULER LES VITRAGES & PRIX", type="primary", use_container_width=True)

    # 1. On mémorise le clic
    if btn_calculer_vitrage:
        st.session_state.afficher_resultats_vitrage = True
        prix_vitrages_saisis_snapshot = {}
        for t_vitre in prix_vitrages_saisis:
            prix_vitrages_saisis_snapshot[t_vitre] = prix_vitrages_saisis[t_vitre]
        st.session_state.prix_vitrages_saisis_snapshot = prix_vitrages_saisis_snapshot

    # 2. On affiche si la variable est en mémoire (même si on change d'onglet)
    if st.session_state.get("afficher_resultats_vitrage", False):
        prix_vitrages_calc = st.session_state.get("prix_vitrages_saisis_snapshot", prix_vitrages_saisis)
        edited_project = st.session_state.chassis_rows_v27
        list_vitrages = []
        for index, row in edited_project.iterrows():
            type_ouvrage = str(row.get("Ouvrage", "")).strip()
            repere = str(row.get("Repère", "")).strip()
            if not type_ouvrage or not repere or row["Qté"] <= 0: continue

            L = float(row["Largeur (L)"])
            H = float(row["Hauteur (H)"])
            qte_ouvrage = int(row["Qté"])
            type_vitrage_saisi = str(row.get("Vitrage", "")).strip()
            if not type_vitrage_saisi: type_vitrage_saisi = "Standard"
            
            a_volet = str(row.get("Volet Roulant", "non")).lower()
            h_caisson = float(row.get("H Caisson", 0.0)) if a_volet == "caisson mono-bloc" else 0.0
                
            vitrage_rows = [comp for comp in BIBLIOTHEQUE if clean_string(comp.get("Type Ouvrage", "")) == clean_string(type_ouvrage) and str(comp.get("Type", "")).strip().lower() in ["vitrage", "verre"]]
            vitrages_groupes = {}
            for comp in vitrage_rows:
                designation = str(comp.get("Composant", "Vitrage")).strip()
                f_vit = str(comp.get("Formule Long", "")).upper().replace('=', '').replace('X', '*')
                if not f_vit: f_vit = str(comp.get("Formule Coupe", "")).upper().replace('=', '').replace('X', '*')
                qte_comp = safe_float(comp.get("Qté", 1), 1.0)
                qte_totale = int(qte_comp * qte_ouvrage)
                base_des = designation.replace("Largeur", "").replace("largeur", "").replace("Hauteur", "").replace("hauteur", "").replace(" L", "").replace(" H", "").strip()
                if not base_des or base_des == "-": base_des = "Vitrage Ouvrage"
                if base_des not in vitrages_groupes: vitrages_groupes[base_des] = {"L": 0, "H": 0, "qte": qte_totale}
                
                if '*' in f_vit and 'L' in f_vit and 'H' in f_vit:
                    parts = f_vit.split('*')
                    for p in parts:
                        if 'L' in p: vitrages_groupes[base_des]["L"] = evaluer_formule(p, L, H, h_caisson, designation)
                        elif 'H' in p: vitrages_groupes[base_des]["H"] = evaluer_formule(p, L, H, h_caisson, designation)
                    vitrages_groupes[base_des]["qte"] = max(vitrages_groupes[base_des]["qte"], qte_totale)
                else:
                    val = evaluer_formule(f_vit, L, H, h_caisson, designation)
                    if 'L' in f_vit or "LARGEUR" in designation.upper() or designation.upper().endswith(" L"):
                        vitrages_groupes[base_des]["L"] = val
                        vitrages_groupes[base_des]["qte"] = max(vitrages_groupes[base_des]["qte"], qte_totale)
                    elif 'H' in f_vit or "HAUTEUR" in designation.upper() or designation.upper().endswith(" H"):
                        vitrages_groupes[base_des]["H"] = val
                        vitrages_groupes[base_des]["qte"] = max(vitrages_groupes[base_des]["qte"], qte_totale)
                    else:
                        if "HAUTEUR" in designation.upper() or "H" in designation.upper(): vitrages_groupes[base_des]["H"] = val
                        else: vitrages_groupes[base_des]["L"] = val
                        vitrages_groupes[base_des]["qte"] = max(vitrages_groupes[base_des]["qte"], qte_totale)

            for base_des, v_data in vitrages_groupes.items():
                v_L = v_data["L"]
                v_H = v_data["H"]
                if v_L == 0: v_L = v_H
                if v_H == 0: v_H = v_L
                if v_L > 0 and v_H > 0:
                    surf_u = (v_L * v_H) / 1000000.0
                    surf_tot = surf_u * v_data["qte"]
                    list_vitrages.append({
                        "Repère": repere, "Ouvrage": type_ouvrage, "Désignation": base_des,
                        "Type Vitrage": type_vitrage_saisi,
                        "Largeur (mm)": int(v_L), "Hauteur (mm)": int(v_H), "Qté": v_data["qte"],
                        "Surf. U. (m²)": round(surf_u, 2), "Surf. Totale (m²)": round(surf_tot, 2)
                    })

        st.markdown(f'<div class="projet-title">PROJET : {NOM_PROJET}</div>', unsafe_allow_html=True)
        st.markdown('<div class="excel-head-blue">🪟 CARNET DE VITRAGE (COMMANDE MIROITIER & PRIX)</div>', unsafe_allow_html=True)
        
        if list_vitrages:
            st.session_state.total_vitrage = 0.0
            for v in list_vitrages:
                t_vitre = v["Type Vitrage"]
                pu_m2 = prix_vitrages_calc.get(t_vitre, 0.0)
                v["Prix Total (DA)"] = v["Surf. Totale (m²)"] * pu_m2
                st.session_state.total_vitrage += v["Prix Total (DA)"]
            
            df_vitrage = pd.DataFrame(list_vitrages)
            st.session_state.list_vitrages_detail = list_vitrages
            surface_projet_totale = df_vitrage["Surf. Totale (m²)"].sum()
            
            html_vitrage = '<table class="print-table" style="width: 100%;"><thead><tr><th>Repère</th><th>Ouvrage</th><th>Détail Vitre</th><th>Type Vitrage</th><th class="center-text">Largeur (mm)</th><th class="center-text">Hauteur (mm)</th><th class="center-text">Qté</th><th class="center-text">Surf. Totale (m²)</th><th class="center-text">Prix Total (DA)</th></tr></thead><tbody>'
            for idx, v in df_vitrage.iterrows():
                html_vitrage += f'<tr><td><b>{v["Repère"]}</b></td><td>{v["Ouvrage"]}</td><td>{v["Désignation"]}</td><td>{v["Type Vitrage"]}</td><td class="center-text" style="color: #1E40AF; font-weight:bold;">{v["Largeur (mm)"]}</td><td class="center-text" style="color: #DC2626; font-weight:bold;">{v["Hauteur (mm)"]}</td><td class="center-text" style="font-weight:bold; font-size:15px;">{v["Qté"]}</td><td class="center-text">{v["Surf. Totale (m²)"]:.2f}</td><td class="center-text" style="font-weight:bold; color:#047857;">{v["Prix Total (DA)"]:.2f} DA</td></tr>'
            html_vitrage += f'<tr style="background-color: #DBEAFE; font-weight: bold;"><td colspan="7" style="text-align: right;">TOTAL GLOBAL VITRAGE :</td><td class="center-text">{surface_projet_totale:.2f} m²</td><td class="center-text" style="color:#1E3A8A; font-size:16px;">{st.session_state.total_vitrage:.2f} DA</td></tr>'
            html_vitrage += '</tbody></table>'
            st.markdown(html_vitrage.replace('\n', ''), unsafe_allow_html=True)
        else:
            st.info("Aucun vitrage n'a été détecté.")

elif menu_selection == "🛒 Quincaillerie & Joints":
    st.markdown('<div class="section-header no-print">🛒 Quincaillerie & Joints (Accessoires)</div>', unsafe_allow_html=True)
    btn_calculer_acc = st.button("🔄 CALCULER LES BESOINS", type="primary", use_container_width=True)

    # 1. On mémorise le clic
    if btn_calculer_acc:
        st.session_state.afficher_resultats_acc = True

    # 2. On affiche si la variable est en mémoire
    if st.session_state.get("afficher_resultats_acc", False):
        edited_project = st.session_state.chassis_rows_v27
        # ... (la suite de ton code reste exactement pareille à partir d'ici)
        list_recap_chassis = []; list_accessoires = []; list_joints = []
        for index, row in edited_project.iterrows():
            type_ouvrage = str(row.get("Ouvrage", "")).strip()
            repere = str(row.get("Repère", "")).strip()
            if not type_ouvrage or row["Qté"] <= 0: continue
            L = float(row["Largeur (L)"])
            H = float(row["Hauteur (H)"])
            qte_ouvrage = int(row["Qté"])
            a_volet = str(row.get("Volet Roulant", "non")).lower()
            h_caisson = float(row.get("H Caisson", 0.0)) if a_volet == "caisson mono-bloc" else 0.0
            for comp in BIBLIOTHEQUE:
                if clean_string(comp.get("Type Ouvrage", "")) == clean_string(type_ouvrage):
                    type_article = str(comp.get("Type", "")).strip().lower()
                    designation = str(comp.get("Composant", "")).strip()
                    serie = str(comp.get("Série", "")).strip()
                    ref_comp = str(comp.get("Ref", "")).strip()
                    unite = str(comp.get("Unité", "U")).strip()
                    qte_comp = safe_float(comp.get("Qté", 1), 1.0)
                    if "joint" in type_article or "brosse" in type_article:
                        longueur_mm = evaluer_formule(comp.get("Formule Long", ""), L, H, h_caisson, designation)
                        if longueur_mm <= 0: longueur_mm = evaluer_formule(comp.get("Formule Coupe", ""), L, H, h_caisson, designation)
                        total_mm = longueur_mm * qte_comp * qte_ouvrage
                        if total_mm > 0:
                            total_m = total_mm / 1000.0
                            list_recap_chassis.append({
                                "Repère": repere, "Ouvrage": type_ouvrage, "Composant": designation,
                                "Ref": ref_comp, "Qté": f"{total_m:.2f}", "Unité": "m", "Série": serie
                            })
                            list_joints.append({"Série": serie, "Référence": ref_comp, "Désignation": designation, "Quantité Totale (m)": total_m})
                    elif "accessoire" in type_article or "quinc" in type_article:
                        total_qty = qte_comp * qte_ouvrage
                        if total_qty > 0:
                            list_recap_chassis.append({
                                "Repère": repere, "Ouvrage": type_ouvrage, "Composant": designation,
                                "Ref": ref_comp, "Qté": f"{int(total_qty)}", "Unité": unite if unite else "U", "Série": serie
                            })
                            list_accessoires.append({"Série": serie, "Référence": ref_comp, "Désignation": designation, "Quantité": total_qty})

        # --- CALCUL DU DEVIS ACCESSOIRES ---
        st.session_state.total_accessoires = 0.0
        for acc in list_accessoires:
            ref = str(acc["Référence"]).strip().upper()
            pu_acc = st.session_state.prix_entreprise.get(ref, 0.0)
            st.session_state.total_accessoires += acc["Quantité"] * pu_acc
            
        for j in list_joints:
            ref = str(j["Référence"]).strip().upper()
            pu_j = st.session_state.prix_entreprise.get(ref, 0.0)
            st.session_state.total_accessoires += j["Quantité Totale (m)"] * pu_j
        # -----------------------------------

        titre_pdf = f"{NOM_PROJET}_Quincaillerie_{DATE_DU_JOUR}".replace(" ", "_").replace("'", "")
        components.html(f"""
            <button onclick="
                var oldTitle = window.parent.document.title; 
                window.parent.document.title = '{titre_pdf}'; 
                setTimeout(function(){{ window.parent.print(); }}, 100);
                setTimeout(function(){{ window.parent.document.title = oldTitle; }}, 2000);
            " style="background-color: #EF4444; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; font-size: 14px; width: 100%;">
            🖨️ IMPRIMER TOUTE LA COMMANDE QUINCAILLERIE
            </button>
        """, height=60)

        st.markdown(f'<div class="projet-title">PROJET : {NOM_PROJET}</div>', unsafe_allow_html=True)
        st.markdown('<div class="excel-head-yellow">📋 DÉTAILS ACCESSOIRES & JOINTS PAR REPERE (CHASSIS)</div>', unsafe_allow_html=True)
        if list_recap_chassis:
            df_recap_chassis = pd.DataFrame(list_recap_chassis)
            df_recap_chassis["Ref"] = df_recap_chassis["Ref"].fillna("-")
            html_chassis = '<table class="print-table" style="width: 100%;"><thead><tr><th>Repère</th><th>Ouvrage</th><th>Composant</th><th>Réf</th><th class="center-text">Qté</th><th class="center-text">Unité</th></tr></thead><tbody>'
            for idx, row_item in df_recap_chassis.iterrows():
                html_chassis += f'<tr><td><b>{row_item["Repère"]}</b></td><td>{row_item["Ouvrage"]}</td><td>{row_item["Composant"]}</td><td>{row_item["Ref"]}</td><td class="center-text" style="font-weight:bold;">{row_item["Qté"]}</td><td class="center-text">{row_item["Unité"]}</td></tr>'
            html_chassis += '</tbody></table>'
            st.markdown(html_chassis.replace('\n', ''), unsafe_allow_html=True)
        else: st.info("Aucun composant n'a été détecté pour ces ouvrages.")

        st.markdown('<div class="block-spacer"></div>', unsafe_allow_html=True)
        st.markdown('<div class="excel-head-blue">🔗 QUINCAILLERIE & ACCESSOIRES (CUMUL GLOBAL DU PROJET)</div>', unsafe_allow_html=True)
        if list_accessoires:
            st.session_state.list_acc_detail = list_accessoires
            df_acc = pd.DataFrame(list_accessoires)
            df_acc["Référence"] = df_acc["Référence"].fillna(""); df_acc["Série"] = df_acc["Série"].fillna("")
            df_acc_grouped = df_acc.groupby(["Série", "Référence", "Désignation"], dropna=False, as_index=False)["Quantité"].sum()
            html_acc = '<table class="print-table" style="width: 100%;"><thead><tr><th>Gamme / Série</th><th>Référence</th><th>Désignation de l\'Accessoire</th><th class="center-text">Quantité Globale</th></tr></thead><tbody>'
            for idx, a in df_acc_grouped.iterrows():
                html_acc += f'<tr><td>{a["Série"]}</td><td>{a["Référence"]}</td><td>{a["Désignation"]}</td><td class="center-text" style="font-weight:bold;">{int(a["Quantité"])} U</td></tr>'
            html_acc += '</tbody></table>'
            st.markdown(html_acc.replace('\n', ''), unsafe_allow_html=True)

        st.markdown('<div class="block-spacer"></div>', unsafe_allow_html=True)
        st.markdown('<div class="excel-head-gray">〰️ JOINTS & BROSSE (CUMUL GLOBAL DU PROJET)</div>', unsafe_allow_html=True)
        if list_joints:
            st.session_state.list_joints_detail = list_joints
            df_joints = pd.DataFrame(list_joints)
            df_joints["Référence"] = df_joints["Référence"].fillna(""); df_joints["Série"] = df_joints["Série"].fillna("")
            df_joints_grouped = df_joints.groupby(["Série", "Référence", "Désignation"], dropna=False, as_index=False)["Quantité Totale (m)"].sum()
            html_joints = '<table class="print-table" style="width: 100%;"><thead><tr><th>Gamme / Série</th><th>Référence</th><th>Désignation du Joint</th><th class="center-text">Longueur Totale (Mètres)</th></tr></thead><tbody>'
            for idx, j in df_joints_grouped.iterrows():
                html_joints += f'<tr><td>{j["Série"]}</td><td>{j["Référence"]}</td><td>{j["Désignation"]}</td><td class="center-text" style="font-weight:bold;">{j["Quantité Totale (m)"]:.2f} m</td></tr>'
            html_joints += '</tbody></table>'
            st.markdown(html_joints.replace('\n', ''), unsafe_allow_html=True)

elif menu_selection == "🏠 Volets Roulants":
    st.markdown('<div class="section-header no-print">🏠 Module Volets Roulants & Tarification</div>', unsafe_allow_html=True)
    
    st.markdown("### ⚙️ 1. Paramètres du Tablier")
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1: hauteur_lame = st.number_input("Hauteur lame (mm)", value=43.0, step=1.0)
    with col2: type_lame = st.selectbox("Type de lame", ["Injectée", "Extrudée"])
    with col3: jeu_coulisses = st.number_input("Jeu coulisses (mm)", value=0.0, step=1.0)
    with col4: longueur_barre_vr = st.number_input("Lg barre lame (mm)", value=5500, step=100)
    with col5: epaisseur_scie_vr = st.number_input("Trait scie (mm)", value=5, step=1)

    st.markdown("### 💰 2. Saisie des Prix Unitaires (Volets)")
    c_p1, c_p2, c_p3, c_p4, c_p5 = st.columns(5)
    with c_p1:
        pu_lame_inj = st.number_input("Prix Lame Injectée 5.5m (DA)", min_value=0.0, value=st.session_state.get("vr_pu_lame_inj", 0.0), step=100.0, key="vr_pu_lame_inj")
    with c_p2:
        pu_lame_ext = st.number_input("Prix Lame Extrudée 5.5m (DA)", min_value=0.0, value=st.session_state.get("vr_pu_lame_ext", 0.0), step=100.0, key="vr_pu_lame_ext")
    with c_p3:
        pu_acc_mono = st.number_input("Prix Acc. Monobloc (Forfait)", min_value=0.0, value=st.session_state.get("vr_pu_mono", 0.0), step=100.0, key="vr_pu_mono")
    with c_p4:
        pu_acc_tun = st.number_input("Prix Acc. Tunnel (Forfait)", min_value=0.0, value=st.session_state.get("vr_pu_tun", 0.0), step=100.0, key="vr_pu_tun")
    with c_p5:
        pu_moteur = st.number_input("Prix Kit Moteur (Unité)", min_value=0.0, value=st.session_state.get("vr_pu_mot", 0.0), step=100.0, key="vr_pu_mot")

    st.markdown("---")

    # --- Configuration par châssis ---
    df_projet_config = st.session_state.chassis_rows_v27
    df_volets_config = df_projet_config[df_projet_config["Volet Roulant"].str.lower() != "non"].dropna(subset=["Volet Roulant"])

    if not df_volets_config.empty:
        st.markdown("### 🔧 3. Configuration par Châssis")
        st.caption("Définissez le type de lame et si un kit moteur est inclus pour chaque volet.")

        if "vr_config_chassis" not in st.session_state:
            st.session_state.vr_config_chassis = {}

        types_lames_dispo = ["Injectée", "Extrudée"]

        header_cols = st.columns([1, 2, 2, 2])
        header_cols[0].markdown("**Repère**")
        header_cols[1].markdown("**Type Volet**")
        header_cols[2].markdown("**Type de Lame**")
        header_cols[3].markdown("**🔌 Kit Moteur**")

        for idx, row in df_volets_config.iterrows():
            repere = str(row.get("Repère", f"V{idx}"))
            type_vr = str(row.get("Volet Roulant", "")).capitalize()
            if repere not in st.session_state.vr_config_chassis:
                st.session_state.vr_config_chassis[repere] = {
                    "type_lame": type_lame,
                    "kit_moteur": True
                }
            cfg = st.session_state.vr_config_chassis[repere]
            c0, c1, c2, c3 = st.columns([1, 2, 2, 2])
            c0.markdown(f"**{repere}**")
            c1.markdown(f"*{type_vr}*")
            lame_idx = types_lames_dispo.index(cfg["type_lame"]) if cfg["type_lame"] in types_lames_dispo else 0
            new_lame = c2.selectbox("", options=types_lames_dispo, index=lame_idx,
                                    key=f"lame_{repere}_{idx}", label_visibility="collapsed")
            new_moteur = c3.checkbox("Inclure", value=cfg["kit_moteur"],
                                     key=f"moteur_{repere}_{idx}")
            st.session_state.vr_config_chassis[repere] = {"type_lame": new_lame, "kit_moteur": new_moteur}

    st.markdown("---")

    btn_calculer_vr = st.button("🔄 CALCULER LES VOLETS", type="primary", use_container_width=True)
    if btn_calculer_vr:
        st.session_state.afficher_resultats_vr = True

    if st.session_state.get("afficher_resultats_vr", False):
        df_projet = st.session_state.chassis_rows_v27
        df_volets = df_projet[df_projet["Volet Roulant"].str.lower() != "non"]
        df_volets = df_volets.dropna(subset=["Volet Roulant"])

        if df_volets.empty:
            st.info("ℹ️ Aucun volet roulant n'a été détecté dans ce projet.")
        else:
            titre_pdf = f"{NOM_PROJET}_Volets_{DATE_DU_JOUR}".replace(" ", "_").replace("'", "")
            components.html(f"""
                <button onclick="
                    var oldTitle = window.parent.document.title; 
                    window.parent.document.title = '{titre_pdf}'; 
                    setTimeout(function(){{ window.parent.print(); }}, 100);
                    setTimeout(function(){{ window.parent.document.title = oldTitle; }}, 2000);
                " style="background-color: #EF4444; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; font-size: 14px; width: 100%;">
                🖨️ IMPRIMER LE DÉTAIL DES VOLETS
                </button>
            """, height=60)

            st.markdown('<div class="excel-head-blue">📝 Détail des Tabliers & Sous-Détail Financier</div>', unsafe_allow_html=True)
            
            lames_a_couper = []
            details_tabliers = []
            sous_detail_vr = []
            st.session_state.total_volets = 0.0

            for idx, row in df_volets.iterrows():
                repere = str(row.get("Repère", f"V{idx}"))
                type_vr = str(row.get("Volet Roulant", "")).lower()
                L = float(row.get("Largeur (L)", 0))
                H = float(row.get("Hauteur (H)", 0))
                qte_chassis = int(row.get("Qté", 1))
                h_caisson = float(row.get("H Caisson", 0))

                cfg_row = st.session_state.get("vr_config_chassis", {}).get(repere, {"type_lame": type_lame, "kit_moteur": True})
                kit_moteur = cfg_row["kit_moteur"]
                type_lame_chassis = cfg_row["type_lame"]

                largeur_lame = max(0.0, L - jeu_coulisses)
                if "tunnel" in type_vr: h_tablier = H + h_caisson
                else: h_tablier = H
                
                import math
                nb_lames_par_tablier = math.ceil(h_tablier / hauteur_lame)
                nb_lames_total = nb_lames_par_tablier * qte_chassis

                # Calcul financier selon type de lame
                pu_barre_lame = pu_lame_ext if type_lame_chassis == "Extrudée" else pu_lame_inj
                longueur_totale_lames_mm = nb_lames_total * largeur_lame
                nb_barres_lames = longueur_totale_lames_mm / longueur_barre_vr
                cout_lames = nb_barres_lames * pu_barre_lame
                
                cout_moteur_ligne = qte_chassis * pu_moteur if kit_moteur else 0.0
                pu_accessoire_choisi = pu_acc_tun if "tunnel" in type_vr else pu_acc_mono
                cout_acc_ligne = qte_chassis * pu_accessoire_choisi
                
                total_ligne_vr = cout_lames + cout_moteur_ligne + cout_acc_ligne
                st.session_state.total_volets += total_ligne_vr

                details_tabliers.append({
                    "Repère": repere, "Type": type_vr.capitalize(), "Type Lame": type_lame_chassis, "Largeur Lame (mm)": largeur_lame,
                    "Hauteur Tablier (mm)": h_tablier, "Qté Volets": qte_chassis, "Total Lames": nb_lames_total,
                    "Kit Moteur": "Oui" if kit_moteur else "Non"
                })
                
                sous_detail_vr.append({
                    "Repère": repere,
                    "Type Lame": type_lame_chassis,
                    "Coût Lames (DA)": round(cout_lames, 2),
                    "Coût Moteur (DA)": round(cout_moteur_ligne, 2),
                    "Coût Accessoires (DA)": round(cout_acc_ligne, 2),
                    "TOTAL (DA)": round(total_ligne_vr, 2)
                })

                for _ in range(nb_lames_total):
                    lames_a_couper.append({"ref": repere, "length": largeur_lame})

            st.session_state.list_volets_detail = details_tabliers
            st.session_state.list_volets_financier = sous_detail_vr

            st.table(pd.DataFrame(details_tabliers))
            st.markdown("#### 💡 Sous-détail des coûts par volet :")
            st.table(pd.DataFrame(sous_detail_vr))
            st.success(f"**Montant Total Volets Roulants : {st.session_state.total_volets:,.2f} DA**")

            st.markdown("---")
            st.markdown('<div class="excel-head-yellow">✂️ Optimisation de Coupe des Lames (5.5m)</div>', unsafe_allow_html=True)
            
            if lames_a_couper:
                barres_optimisees = optimize_cutting_1d_with_ref(lames_a_couper, longueur_barre_vr, epaisseur_scie_vr)
                grouped_bars_vr = []
                for bar in barres_optimisees:
                    matched = False
                    for gb in grouped_bars_vr:
                        if len(bar) == len(gb['pieces']):
                            is_identical = True
                            for p1, p2 in zip(bar, gb['pieces']):
                                if p1['length'] != p2['length'] or p1['ref'] != p2['ref']:
                                    is_identical = False; break
                            if is_identical: gb['qty'] += 1; matched = True; break
                    if not matched: grouped_bars_vr.append({'pieces': bar, 'qty': 1})

                html_vr = f'<table class="print-table" style="width: 100%;"><thead><tr><th style="width: 15%;">LAMES ({type_lame})</th><th style="width: 55%; text-align: center;">PLAN DE COUPE</th><th style="width: 10%; text-align: center;">QTÉ BARRES</th><th style="width: 10%; text-align: center;">CHUTE (mm)</th><th style="width: 10%; text-align: center;">PERTE %</th></tr></thead><tbody>'
                refs_uniques_vr = list(set([c["ref"] for c in lames_a_couper]))
                map_couleurs_vr = {ref: PALETTE_COULEURS[i % len(PALETTE_COULEURS)] for i, ref in enumerate(refs_uniques_vr)}
                
                total_barres_vr = 0
                for gb in grouped_bars_vr:
                    bar = gb['pieces']; qty = gb['qty']; total_barres_vr += qty
                    used_in_bar = sum(c['length'] for c in bar)
                    bar_blade_loss = (len(bar) - 1) * epaisseur_scie_vr if len(bar) > 1 else 0
                    chute_bar = longueur_barre_vr - used_in_bar - bar_blade_loss
                    chute_pct = (chute_bar / longueur_barre_vr) * 100

                    html_barre_div = '<div class="bar-container">'
                    for cut in bar:
                        pct_largeur = ((cut['length'] + epaisseur_scie_vr) / longueur_barre_vr) * 100
                        couleur = map_couleurs_vr.get(cut['ref'], "#1E40AF")
                        html_barre_div += f'<div class="bar-segment" style="width: {pct_largeur}%; background-color: {couleur};" title="{cut["ref"]} - {cut["length"]} mm">{int(cut["length"])}</div>'
                    if chute_pct > 0: html_barre_div += f'<div class="bar-chute" style="width: {chute_pct}%;"></div>'
                    html_barre_div += '</div>'
                    html_vr += f'<tr><td style="font-weight: bold;">Barre 5.5m</td><td style="padding: 10px;">{html_barre_div}</td><td class="center-text" style="font-weight: bold; font-size: 15px;">{qty}</td><td class="center-text">{int(chute_bar)}</td><td class="center-text">{chute_pct:.1f}%</td></tr>'
                
                html_vr += f'<tr style="background-color: #DBEAFE; font-weight: bold;"><td colspan="2" style="text-align: right;">TOTAL BARRES À COMMANDER :</td><td class="center-text">{total_barres_vr}</td><td colspan="2"></td></tr>'
                html_vr += "</tbody></table>"
                st.markdown(html_vr, unsafe_allow_html=True)
elif menu_selection == "🚧 Garde-corps (Barres 6m)":
    st.markdown('<div class="section-header no-print">🧮 Module Garde-Corps — Optimisation & Tarification</div>', unsafe_allow_html=True)
    
    st.subheader("1. Configuration de la barre brute de profilé")
    col1, col2 = st.columns(2)
    with col1: bar_length = st.number_input("Longueur brute d'une barre (mm)", value=6000, step=100)
    with col2: blade_width = st.number_input("Épaisseur de la lame / Trait de coupe (mm)", value=5, step=1)

    st.write("---")
    st.subheader("2. Saisie des tronçons et verres associés")
    
    if "L Vitrage (mm)" not in st.session_state.df_garde_corps.columns:
        st.session_state.df_garde_corps["L Vitrage (mm)"] = 0.0
        st.session_state.df_garde_corps["H Vitrage (mm)"] = 0.0

    edited_df_gc = st.data_editor(
        st.session_state.df_garde_corps,
        num_rows="dynamic",
        use_container_width=True,
        column_config={
            "Emplacement / Réf": st.column_config.TextColumn("Emplacement", required=True),
            "Longueur (mm)": st.column_config.NumberColumn("Lg Barre Alu (mm)", min_value=1, step=1, required=True),
            "Quantité": st.column_config.NumberColumn("Quantité", min_value=1, step=1, required=True),
            "L Vitrage (mm)": st.column_config.NumberColumn("Lg Verre (mm)"),
            "H Vitrage (mm)": st.column_config.NumberColumn("Ht Verre (mm)")
        }
    )

    st.markdown("### 💰 3. Prix Unitaires du Garde-Corps")
    gc_c1, gc_c2 = st.columns(2)
    with gc_c1:
        pu_barre_gc = st.number_input("Prix de la Barre Profilé 6m (DA)", min_value=0.0, value=st.session_state.get("gc_pu_barre", 0.0), step=100.0, key="gc_pu_barre")
    with gc_c2:
        pu_verre_gc = st.number_input("Prix du Verre de Garde-corps (DA / m²)", min_value=0.0, value=st.session_state.get("gc_pu_verre", 0.0), step=100.0, key="gc_pu_verre")

    st.write("---")

    btn_calculer_gc = st.button("🚀 Générer le plan de coupe & calculer le devis", type="primary", use_container_width=True)
    if btn_calculer_gc:
        st.session_state.afficher_resultats_gc = True

    if st.session_state.get("afficher_resultats_gc", False):
        st.session_state.df_garde_corps = edited_df_gc.copy()
        cuts_list = []
        for idx, row in edited_df_gc.iterrows():
            if pd.isna(row.get("Longueur (mm)")) or pd.isna(row.get("Quantité")): continue 
            ref = str(row.get("Emplacement / Réf", f"Pièce {idx+1}")).strip()
            if not ref or ref.lower() == "none" or ref == "nan": ref = f"Pièce {idx+1}"
            longueur = int(row.get("Longueur (mm)", 0))
            qte = int(row.get("Quantité", 0))
            if longueur > 0 and qte > 0:
                for _ in range(qte): cuts_list.append({"ref": ref, "length": longueur})
        
        if not cuts_list:
            st.error("⚠️ Veuillez saisir au moins une ligne complète (Longueur et Quantité).")
        else:
            max_cut_length = max([c['length'] for c in cuts_list])
            if max_cut_length > bar_length:
                st.error(f"❌ Erreur : Impossible de couper un morceau de {max_cut_length} mm dans une barre de {bar_length} mm.")
            else:
                result_bars = optimize_cutting_1d_with_ref(cuts_list, bar_length, blade_width)
                grouped_bars = []
                for bar in result_bars:
                    matched = False
                    for gb in grouped_bars:
                        if len(bar) == len(gb['pieces']):
                            is_identical = True
                            for p1, p2 in zip(bar, gb['pieces']):
                                if p1['length'] != p2['length'] or p1['ref'] != p2['ref']:
                                    is_identical = False; break
                            if is_identical: gb['qty'] += 1; matched = True; break
                    if not matched: grouped_bars.append({'pieces': bar, 'qty': 1})

                total_bars = len(result_bars)
                total_utile = sum([c['length'] for c in cuts_list])
                total_brut = total_bars * bar_length
                total_blade_loss = sum((len(b) - 1) * blade_width for b in result_bars if len(b) > 0)
                total_chute = total_brut - total_utile - total_blade_loss
                perte_pct = (total_chute / total_brut) * 100
                
                # Calcul financier Garde-Corps
                cout_barres_gc = total_bars * pu_barre_gc
                surface_verre_totale_m2 = 0.0
                for idx, row in edited_df_gc.iterrows():
                    l_v = float(row.get("L Vitrage (mm)", 0))
                    h_v = float(row.get("H Vitrage (mm)", 0))
                    qte_v = int(row.get("Quantité", 0))
                    if l_v > 0 and h_v > 0:
                        surface_verre_totale_m2 += ((l_v * h_v) / 1000000.0) * qte_v
                
                cout_verre_gc = surface_verre_totale_m2 * pu_verre_gc
                st.session_state.total_gardecorps = cout_barres_gc + cout_verre_gc

                st.subheader("📊 Résumé du débit")
                m1, m2, m3 = st.columns(3)
                m1.metric("Barres totales à commander", f"{total_bars} Unités")
                m2.metric("Longueur utile totale", f"{total_utile / 1000:.2f} ml")
                m3.metric("Taux de perte global", f"{perte_pct:.1f} %")

                st.markdown("### 💰 Sous-Détail Financier Garde-Corps")
                st.table(pd.DataFrame([
                    {"Élément": f"Profilés Aluminium ({total_bars} Barres de 6m)", "Montant": f"{cout_barres_gc:,.2f} DA"},
                    {"Élément": f"Vitrages Garde-Corps ({surface_verre_totale_m2:.2f} m²)", "Montant": f"{cout_verre_gc:,.2f} DA"},
                    {"Élément": "TOTAL GARDE-CORPS", "Montant": f"{st.session_state.total_gardecorps:,.2f} DA"}
                ]))
                st.write("---")

                # Palette de couleurs pour les coupes garde-corps
                couleurs_palette_gc = ["#3B82F6", "#EF4444", "#10B981", "#F59E0B", "#8B5CF6", "#EC4899", "#6366F1", "#14B8A6"]
                reps_uniques_gc = list(set([c['ref'] for c in cuts_list]))
                map_couleurs_gc = {rep: couleurs_palette_gc[i % len(couleurs_palette_gc)] for i, rep in enumerate(reps_uniques_gc)}

                html_garde_corps = '<table class="print-table" style="width: 100%;"><thead><tr><th style="width: 10%; text-align: center;">TYPE BARRE</th><th style="width: 50%; text-align: center;">PLAN DE COUPE VISUEL</th><th style="width: 8%; text-align: center;">QTÉ</th><th style="width: 10%; text-align: center;">UTILE (mm)</th><th style="width: 10%; text-align: center;">CHUTE (mm)</th><th style="width: 12%; text-align: center;">% PERTE</th></tr></thead><tbody>'
                b_idx = 1
                for gb in grouped_bars:
                    bar = gb['pieces']; qty = gb['qty']
                    used_in_bar = sum(c['length'] for c in bar)
                    bar_blade_loss = (len(bar) - 1) * blade_width if len(bar) > 1 else 0
                    chute_bar = bar_length - used_in_bar - bar_blade_loss
                    chute_pct = (chute_bar / bar_length) * 100
                    html_barre_div = '<div class="bar-container">'
                    for cut in bar:
                        pct_largeur = ((cut['length'] + blade_width) / bar_length) * 100
                        couleur = map_couleurs_gc.get(cut['ref'], "#1E40AF")
                        html_barre_div += f'<div class="bar-segment" style="width: {pct_largeur}%; background-color: {couleur};" title="{cut["ref"]} - {cut["length"]} mm">{cut["length"]}</div>'
                    if chute_pct > 0: html_barre_div += f'<div class="bar-chute" style="width: {chute_pct}%;"></div>'
                    html_barre_div += '</div>'
                    html_garde_corps += f'<tr><td class="center-text" style="font-weight: bold;">Type {b_idx}</td><td style="padding: 10px;">{html_barre_div}</td><td class="center-text" style="font-weight: bold; font-size: 15px;">{qty}</td><td class="center-text">{int(used_in_bar)}</td><td class="center-text">{int(chute_bar)}</td><td class="center-text">{chute_pct:.1f}%</td></tr>'
                    b_idx += 1
                html_garde_corps += "</tbody></table>"
                
                legende_html = "<div style='margin-bottom: 20px; font-size: 13px;'><b>Légende des emplacements : </b>"
                for ref, color in map_couleurs_gc.items():
                    legende_html += f"<span style='display:inline-block; margin-right: 15px;'><span style='display:inline-block; width:12px; height:12px; background-color:{color}; margin-right:5px; border-radius:2px;'></span>{ref}</span>"
                legende_html += "</div>"
                
                st.markdown(legende_html, unsafe_allow_html=True)
                st.markdown(html_garde_corps, unsafe_allow_html=True)

elif menu_selection == "🛠️ Gestionnaire de Bibliothèque":
    st.markdown('<div class="section-header no-print">🛠️ Catalogue Actuel Emporté</div>', unsafe_allow_html=True)
    st.dataframe(pd.DataFrame(BIBLIOTHEQUE), use_container_width=True)

elif menu_selection == "💰 Mes Prix Unitaires":
    st.markdown('<div class="section-header no-print">💰 Gestion de mes Prix Unitaires</div>', unsafe_allow_html=True)
    st.info("Modifiez ici les prix spécifiques à votre entreprise. Les nouvelles références de la bibliothèque sont ajoutées automatiquement.")

    # 🔄 Synchronisation automatique des nouvelles références
    sync_user_prices(st.session_state.entreprise_id, BIBLIOTHEQUE)

    # Charger les prix à jour pour le reste de la session
    st.session_state.prix_entreprise = load_user_prices(st.session_state.entreprise_id)

    # Récupérer directement les lignes de la table prix_unitaires
    res_prix = supabase.table("prix_unitaires").select("*").eq("entreprise_id", st.session_state.entreprise_id).execute()
    
    if res_prix.data:
        df_prix = pd.DataFrame(res_prix.data)
        
        df_prix = df_prix.rename(columns={
            "gamme": "Gamme",
            "serie": "Série",
            "type_article": "Type Article",
            "composant": "Composant",
            "ref_composant": "Réf",
            "unite": "Unité",
            "prix_unitaire": "Prix Unitaire"
        })
        
        cols_display = ["Gamme", "Série", "Type Article", "Composant", "Réf", "Unité", "Prix Unitaire"]
        cols_existantes = [c for c in cols_display if c in df_prix.columns]
        df_prix_display = df_prix[cols_existantes]

        # Ajout manuel des références virtuelles (s'ils ne sont pas en base)
        refs_virtuelles = [
            {"Gamme": "Général", "Série": "-", "Type Article": "VR", "Composant": "Lame Volet (Mètre)", "Réf": "LAME", "Unité": "m", "Prix Unitaire": st.session_state.prix_entreprise.get("LAME", 0.0)},
            {"Gamme": "Général", "Série": "-", "Type Article": "VR", "Composant": "Kit Moteur", "Réf": "MOTEUR", "Unité": "U", "Prix Unitaire": st.session_state.prix_entreprise.get("MOTEUR", 0.0)},
            {"Gamme": "Général", "Série": "-", "Type Article": "VR", "Composant": "Forfait Acc. VR", "Réf": "ACC_VR", "Unité": "U", "Prix Unitaire": st.session_state.prix_entreprise.get("ACC_VR", 0.0)},
            {"Gamme": "Général", "Série": "-", "Type Article": "Barre", "Composant": "Profilé Garde-Corps", "Réf": "PROFIL_GC", "Unité": "Barre", "Prix Unitaire": st.session_state.prix_entreprise.get("PROFIL_GC", 0.0)}
        ]
        
        df_virtuelles = pd.DataFrame(refs_virtuelles)
        df_prix_display = pd.concat([df_prix_display, df_virtuelles], ignore_index=True)

        edited_df_prix = st.data_editor(
            df_prix_display,
            use_container_width=True,
            disabled=["Gamme", "Série", "Type Article", "Composant", "Réf", "Unité"],
            column_config={
                "Prix Unitaire": st.column_config.NumberColumn("Prix Unitaire", min_value=0.0, format="%.2f DA")
            },
            height=600
        )

        if st.button("💾 Enregistrer ma grille tarifaire", type="primary", use_container_width=True):
            with st.spinner("Mise à jour des prix..."):
                try:
                    for idx, row in edited_df_prix.iterrows():
                        ref = row["Réf"]
                        pu = float(row["Prix Unitaire"])
                        
                        if ref in ["LAME", "MOTEUR", "ACC_VR", "PROFIL_GC"]:
                            res_virtuel = supabase.table("prix_unitaires").select("id").eq("entreprise_id", st.session_state.entreprise_id).eq("ref_composant", ref).execute()
                            if res_virtuel.data:
                                supabase.table("prix_unitaires").update({"prix_unitaire": pu}).eq("entreprise_id", st.session_state.entreprise_id).eq("ref_composant", ref).execute()
                            else:
                                supabase.table("prix_unitaires").insert({
                                    "entreprise_id": st.session_state.entreprise_id, "ref_composant": ref, "composant": row["Composant"],
                                    "gamme": row["Gamme"], "serie": row["Série"], "type_article": row["Type Article"], "unite": row["Unité"], "prix_unitaire": pu
                                }).execute()
                        else:
                            supabase.table("prix_unitaires")\
                                .update({"prix_unitaire": pu})\
                                .eq("entreprise_id", st.session_state.entreprise_id)\
                                .eq("ref_composant", ref)\
                                .execute()

                    st.session_state.prix_entreprise = load_user_prices(st.session_state.entreprise_id)
                    st.success("✅ Vos prix ont été sauvegardés avec succès !")
                    st.rerun()
                except Exception as e:
                    st.error(f"🔴 Erreur lors de la sauvegarde : {e}")
    else:
        st.warning("Aucune référence trouvée.")

# ==========================================
# 📊 NOUVEAU MODULE : DEVIS GLOBAL
# ==========================================
elif menu_selection == "📊 Devis Global du Projet":
    st.markdown('<div class="section-header no-print">📊 Synthèse Financière & Devis Global</div>', unsafe_allow_html=True)
    
    # 1. Champs de saisie pour les frais annexes avec bouton de réinitialisation/suppression
    st.markdown("### ⚙️ Frais Annexes & Taxes")
    col_mo, col_frais_desc, col_frais_cout, col_tva, col_reset = st.columns([2, 2, 2, 1, 1.2])
    with col_mo:
        main_oeuvre = st.number_input("Main d'Œuvre (DA)", min_value=0.0, value=st.session_state.get("devis_mo", 0.0), step=1000.0, key="devis_mo")
    with col_frais_desc:
        frais_desc = st.text_input("Désignation Frais Sup.", placeholder="Ex: Transport, Grue...", value=st.session_state.get("devis_frais_desc", ""), key="devis_frais_desc")
    with col_frais_cout:
        frais_cout = st.number_input("Coût Frais Sup. (DA)", min_value=0.0, value=st.session_state.get("devis_frais_cout", 0.0), step=500.0, key="devis_frais_cout")
    with col_tva:
        tva_pct = st.number_input("TVA (%)", min_value=0.0, value=st.session_state.get("devis_tva", 19.0), step=1.0, key="devis_tva")
    with col_reset:
        st.write("")
        st.write("")
        if st.button("🗑️ Effacer Frais", use_container_width=True, help="Réinitialiser la main d'œuvre et les frais annexes à 0"):
            st.session_state.devis_mo = 0.0
            st.session_state.devis_frais_desc = ""
            st.session_state.devis_frais_cout = 0.0
            st.rerun()

    st.markdown("---")
    
    # 2. Bouton d'impression
    titre_pdf_devis = f"Devis_{NOM_PROJET}_{DATE_DU_JOUR}".replace(" ", "_").replace("'", "")
    components.html(f"""
        <button onclick="
            var oldTitle = window.parent.document.title; 
            window.parent.document.title = '{titre_pdf_devis}'; 
            setTimeout(function(){{ window.parent.print(); }}, 100);
            setTimeout(function(){{ window.parent.document.title = oldTitle; }}, 2000);
        " style="background-color: #10B981; color: white; padding: 12px 20px; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; font-size: 16px; width: 100%; margin-bottom: 20px;">
        🖨️ IMPRIMER LE DEVIS GLOBAL
        </button>
    """, height=70)

    # 3. Calculs
    total_materiel_ht = (st.session_state.total_alu + st.session_state.total_vitrage + 
                         st.session_state.total_accessoires + st.session_state.total_volets + 
                         st.session_state.total_gardecorps)
    
    total_ht = total_materiel_ht + main_oeuvre + frais_cout
    montant_tva = total_ht * (tva_pct / 100.0)
    total_ttc = total_ht + montant_tva

    # 4. Affichage du document imprimable (HTML)
    st.markdown(f'<div class="projet-title">DEVIS OFFICIEL : {NOM_PROJET}</div>', unsafe_allow_html=True)
    
    html_devis = f"""
    <table class="print-table" style="width: 100%; margin-bottom: 30px;">
        <thead>
            <tr>
                <th style="background-color: #1E3A8A; color: white; padding: 10px; font-size: 16px;">DÉSIGNATION / RUBRIQUE</th>
                <th class="center-text" style="background-color: #1E3A8A; color: white; padding: 10px; font-size: 16px; width: 30%;">MONTANT HT (DA)</th>
            </tr>
        </thead>
        <tbody>
            <tr><td><b>1. Profilés Aluminium</b> (Toutes séries confondues)</td><td class="center-text">{st.session_state.total_alu:,.2f}</td></tr>
            <tr><td><b>2. Vitrages & Miroiterie</b></td><td class="center-text">{st.session_state.total_vitrage:,.2f}</td></tr>
            <tr><td><b>3. Quincaillerie, Accessoires & Joints</b></td><td class="center-text">{st.session_state.total_accessoires:,.2f}</td></tr>
            <tr><td><b>4. Volets Roulants</b> (Lames, moteurs, accessoires)</td><td class="center-text">{st.session_state.total_volets:,.2f}</td></tr>
            <tr><td><b>5. Garde-corps</b> (Profilés & Vitrage)</td><td class="center-text">{st.session_state.total_gardecorps:,.2f}</td></tr>
            <tr style="background-color: #F3F4F6;">
                <td style="text-align: right; font-weight: bold; padding-right: 15px;">SOUS-TOTAL MATÉRIEL HT :</td>
                <td class="center-text" style="font-weight: bold; color: #1E40AF;">{total_materiel_ht:,.2f} DA</td>
            </tr>
            <tr><td><b>6. Main d'Œuvre & Pose</b></td><td class="center-text">{main_oeuvre:,.2f}</td></tr>
    """
    
    if frais_cout > 0:
        designation = frais_desc if frais_desc else "Frais Supplémentaires"
        html_devis += f'<tr><td><b>7. {designation}</b></td><td class="center-text">{frais_cout:,.2f}</td></tr>'

    html_devis += f"""
        </tbody>
    </table>
    
    <table class="print-table" style="width: 50%; float: right; font-size: 16px;">
        <tbody>
            <tr>
                <td style="font-weight: bold; text-align: right; padding: 10px;">TOTAL HT :</td>
                <td class="center-text" style="font-weight: bold; width: 45%;">{total_ht:,.2f} DA</td>
            </tr>
            <tr>
                <td style="text-align: right; padding: 10px;">TVA ({tva_pct}%) :</td>
                <td class="center-text">{montant_tva:,.2f} DA</td>
            </tr>
            <tr style="background-color: #10B981; color: white;">
                <td style="font-weight: bold; text-align: right; padding: 15px; font-size: 18px;">NET À PAYER TTC :</td>
                <td class="center-text" style="font-weight: bold; font-size: 18px;">{total_ttc:,.2f} DA</td>
            </tr>
        </tbody>
    </table>
    <div style="clear: both;"></div>
    """
    st.markdown(html_devis, unsafe_allow_html=True)

    # 5. Détail complet des postes avec Prix & Impression par section
    st.markdown('<div class="block-spacer"></div>', unsafe_allow_html=True)
    st.markdown('<div class="excel-head-blue">📜 DÉTAIL MATIÈRE AVEC PRIX ET IMPRESSION PAR POSTE D\'OUVRAGE</div>', unsafe_allow_html=True)

    # -------------------------------------------------------------
    # 1. DÉTAIL MENUISERIE ALUMINIUM (PROFILÉS)
    # -------------------------------------------------------------
    st.markdown('<div style="page-break-before: always; margin-top: 30px;"></div>', unsafe_allow_html=True)
    st.markdown("### 🪟 1. Détail Menuiserie Aluminium & Profilés")
    
    titre_pdf_alu = f"Devis_Detail_Aluminium_{NOM_PROJET}_{DATE_DU_JOUR}".replace(" ", "_").replace("'", "")
    components.html(f"""
        <button onclick="
            var oldTitle = window.parent.document.title; 
            window.parent.document.title = '{titre_pdf_alu}'; 
            setTimeout(function(){{ window.parent.print(); }}, 100);
            setTimeout(function(){{ window.parent.document.title = oldTitle; }}, 2000);
        " style="background-color: #1E3A8A; color: white; padding: 10px 18px; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; font-size: 14px; width: 100%; margin-bottom: 10px;">
        🖨️ IMPRIMER CE DÉTAIL : DEVIS PROFILÉS ALUMINIUM (PDF)
        </button>
    """, height=55)

    list_prof_det = st.session_state.get("list_profils_commande", [])
    if list_prof_det:
        html_prof_det = '<table class="print-table" style="width: 100%;"><thead><tr><th>Série</th><th>Référence Profilé</th><th class="center-text">Barres (6m)</th><th class="center-text">Prix Unitaire Barre (DA)</th><th class="center-text">Total HT (DA)</th></tr></thead><tbody>'
        tot_alu_sec = 0.0
        for item in list_prof_det:
            s_serie = item.get("Série", "-")
            s_ref = item.get("Référence", "-")
            s_qte = item.get("Quantité Barres", 0)
            s_pu = item.get("Prix Unitaire (DA)", 0.0)
            s_tot = item.get("Total HT (DA)", 0.0)
            tot_alu_sec += s_tot
            html_prof_det += f'<tr><td>{s_serie}</td><td><b>{s_ref}</b></td><td class="center-text" style="font-weight:bold;">{s_qte}</td><td class="center-text">{s_pu:,.2f} DA</td><td class="center-text" style="font-weight:bold; color:#047857;">{s_tot:,.2f} DA</td></tr>'
        html_prof_det += f'<tr style="background-color: #D1FAE5; font-weight: bold;"><td colspan="4" style="text-align: right;">TOTAL HT PROFILÉS ALUMINIUM :</td><td class="center-text" style="color:#065F46; font-size:16px;">{tot_alu_sec:,.2f} DA</td></tr></tbody></table>'
        st.markdown(html_prof_det, unsafe_allow_html=True)
    elif not st.session_state.chassis_rows_v27.empty:
        st.dataframe(st.session_state.chassis_rows_v27, use_container_width=True)
        st.info("💡 Remarque : Pour obtenir la liste exacte des barres alu à commander avec prix, générez d'abord le débit dans le module '📐 Fiche Atelier & Débit'.")
    else:
        st.info("Aucun châssis enregistré.")

    # -------------------------------------------------------------
    # 2. DÉTAIL VITRAGE & MIROITERIE
    # -------------------------------------------------------------
    st.markdown('<div style="page-break-before: always; margin-top: 30px;"></div>', unsafe_allow_html=True)
    st.markdown("### 💎 2. Détail Vitrage & Miroiterie")

    titre_pdf_vit = f"Devis_Detail_Vitrage_{NOM_PROJET}_{DATE_DU_JOUR}".replace(" ", "_").replace("'", "")
    components.html(f"""
        <button onclick="
            var oldTitle = window.parent.document.title; 
            window.parent.document.title = '{titre_pdf_vit}'; 
            setTimeout(function(){{ window.parent.print(); }}, 100);
            setTimeout(function(){{ window.parent.document.title = oldTitle; }}, 2000);
        " style="background-color: #2563EB; color: white; padding: 10px 18px; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; font-size: 14px; width: 100%; margin-bottom: 10px;">
        🖨️ IMPRIMER CE DÉTAIL : DEVIS VITRAGE & MIROITERIE (PDF)
        </button>
    """, height=55)

    list_vit_det = st.session_state.get("list_vitrages_detail", [])
    if list_vit_det:
        html_vit_det = '<table class="print-table" style="width: 100%;"><thead><tr><th>Repère</th><th>Ouvrage</th><th>Désignation</th><th>Type Vitrage</th><th class="center-text">Dimensions (mm)</th><th class="center-text">Qté</th><th class="center-text">Surf. Totale (m²)</th><th class="center-text">Total HT (DA)</th></tr></thead><tbody>'
        tot_vit_sec = 0.0
        for item in list_vit_det:
            rep = item.get("Repère", "-")
            ouvr = item.get("Ouvrage", "-")
            desig = item.get("Désignation", "-")
            t_vit = item.get("Type Vitrage", "-")
            l_v = item.get("Largeur (mm)", 0)
            h_v = item.get("Hauteur (mm)", 0)
            q_v = item.get("Qté", 1)
            s_tot = item.get("Surf. Totale (m²)", 0.0)
            p_tot = item.get("Prix Total (DA)", 0.0)
            tot_vit_sec += p_tot
            html_vit_det += f'<tr><td><b>{rep}</b></td><td>{ouvr}</td><td>{desig}</td><td>{t_vit}</td><td class="center-text">{l_v} × {h_v}</td><td class="center-text" style="font-weight:bold;">{q_v}</td><td class="center-text">{s_tot:.2f}</td><td class="center-text" style="font-weight:bold; color:#047857;">{p_tot:,.2f} DA</td></tr>'
        html_vit_det += f'<tr style="background-color: #DBEAFE; font-weight: bold;"><td colspan="7" style="text-align: right;">TOTAL HT VITRAGE :</td><td class="center-text" style="color:#1E3A8A; font-size:16px;">{tot_vit_sec:,.2f} DA</td></tr></tbody></table>'
        st.markdown(html_vit_det, unsafe_allow_html=True)
    else:
        st.info("Aucun calcul de vitrage n'a été effectué pour le moment (rendez-vous dans le module '🪟 Carnet de Vitrage').")

    # -------------------------------------------------------------
    # 3. DÉTAIL QUINCAILLERIE, ACCESSOIRES & JOINTS
    # -------------------------------------------------------------
    st.markdown('<div style="page-break-before: always; margin-top: 30px;"></div>', unsafe_allow_html=True)
    st.markdown("### 🛒 3. Détail Quincaillerie, Accessoires & Joints")

    titre_pdf_quinc = f"Devis_Detail_Quincaillerie_{NOM_PROJET}_{DATE_DU_JOUR}".replace(" ", "_").replace("'", "")
    components.html(f"""
        <button onclick="
            var oldTitle = window.parent.document.title; 
            window.parent.document.title = '{titre_pdf_quinc}'; 
            setTimeout(function(){{ window.parent.print(); }}, 100);
            setTimeout(function(){{ window.parent.document.title = oldTitle; }}, 2000);
        " style="background-color: #D97706; color: white; padding: 10px 18px; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; font-size: 14px; width: 100%; margin-bottom: 10px;">
        🖨️ IMPRIMER CE DÉTAIL : DEVIS QUINCAILLERIE & JOINTS (PDF)
        </button>
    """, height=55)

    list_acc_det = st.session_state.get("list_acc_detail", [])
    list_jnt_det = st.session_state.get("list_joints_detail", [])

    if list_acc_det or list_jnt_det:
        if list_acc_det:
            st.markdown("#### Accessoires & Serrures :")
            html_acc_det = '<table class="print-table" style="width: 100%;"><thead><tr><th>Gamme / Série</th><th>Référence Article</th><th>Désignation Accessoire</th><th class="center-text">Quantité</th><th class="center-text">Prix Unitaire (DA)</th><th class="center-text">Total HT (DA)</th></tr></thead><tbody>'
            tot_acc_sec = 0.0
            for item in list_acc_det:
                s_ser = item.get("Série", "-")
                s_ref = item.get("Référence Article", item.get("Article", "-"))
                s_des = item.get("Désignation", "-")
                s_qte = item.get("Quantité", 0)
                s_pu = item.get("Prix Unitaire (DA)", 0.0)
                s_tot = item.get("Total HT (DA)", 0.0)
                tot_acc_sec += s_tot
                html_acc_det += f'<tr><td>{s_ser}</td><td><b>{s_ref}</b></td><td>{s_des}</td><td class="center-text" style="font-weight:bold;">{s_qte}</td><td class="center-text">{s_pu:,.2f} DA</td><td class="center-text" style="font-weight:bold; color:#047857;">{s_tot:,.2f} DA</td></tr>'
            html_acc_det += f'<tr style="background-color: #FEF3C7; font-weight: bold;"><td colspan="5" style="text-align: right;">SOUS-TOTAL ACCESSOIRES :</td><td class="center-text" style="color:#B45309;">{tot_acc_sec:,.2f} DA</td></tr></tbody></table>'
            st.markdown(html_acc_det, unsafe_allow_html=True)

        if list_jnt_det:
            st.markdown("#### Joints d'Étanchéité & Brosse :")
            html_jnt_det = '<table class="print-table" style="width: 100%;"><thead><tr><th>Série</th><th>Référence Joint</th><th>Désignation</th><th class="center-text">Métrage Total (m)</th><th class="center-text">Prix / m (DA)</th><th class="center-text">Total HT (DA)</th></tr></thead><tbody>'
            tot_jnt_sec = 0.0
            for item in list_jnt_det:
                s_ser = item.get("Série", "-")
                s_ref = item.get("Référence", item.get("Joint / Brosse", "-"))
                s_des = item.get("Désignation", "-")
                s_qte = item.get("Métrage Total (m)", item.get("Quantité Totale (m)", 0.0))
                s_pu = item.get("Prix Unitaire /m (DA)", item.get("Prix Unitaire (DA)", 0.0))
                s_tot = item.get("Total HT (DA)", 0.0)
                tot_jnt_sec += s_tot
                html_jnt_det += f'<tr><td>{s_ser}</td><td><b>{s_ref}</b></td><td>{s_des}</td><td class="center-text" style="font-weight:bold;">{s_qte:.2f} m</td><td class="center-text">{s_pu:,.2f} DA</td><td class="center-text" style="font-weight:bold; color:#047857;">{s_tot:,.2f} DA</td></tr>'
            html_jnt_det += f'<tr style="background-color: #FEF3C7; font-weight: bold;"><td colspan="5" style="text-align: right;">SOUS-TOTAL JOINTS :</td><td class="center-text" style="color:#B45309;">{tot_jnt_sec:,.2f} DA</td></tr></tbody></table>'
            st.markdown(html_jnt_det, unsafe_allow_html=True)

        st.markdown(f'<div style="text-align:right; font-weight:bold; font-size:16px; color:#B45309; padding: 8px; background-color:#FFFBEB; border:1px solid #FCD34D; border-radius:4px;">TOTAL HT QUINCAILLERIE & JOINTS : {st.session_state.total_accessoires:,.2f} DA</div>', unsafe_allow_html=True)
    else:
        st.info("Aucun calcul d'accessoires ou de joints n'a été effectué pour le moment (rendez-vous dans le module '🛒 Quincaillerie & Joints').")

    # -------------------------------------------------------------
    # 4. DÉTAIL VOLETS ROULANTS
    # -------------------------------------------------------------
    st.markdown('<div style="page-break-before: always; margin-top: 30px;"></div>', unsafe_allow_html=True)
    st.markdown("### 🏠 4. Détail Volets Roulants")

    titre_pdf_vr = f"Devis_Detail_Volets_{NOM_PROJET}_{DATE_DU_JOUR}".replace(" ", "_").replace("'", "")
    components.html(f"""
        <button onclick="
            var oldTitle = window.parent.document.title; 
            window.parent.document.title = '{titre_pdf_vr}'; 
            setTimeout(function(){{ window.parent.print(); }}, 100);
            setTimeout(function(){{ window.parent.document.title = oldTitle; }}, 2000);
        " style="background-color: #059669; color: white; padding: 10px 18px; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; font-size: 14px; width: 100%; margin-bottom: 10px;">
        🖨️ IMPRIMER CE DÉTAIL : DEVIS VOLETS ROULANTS (PDF)
        </button>
    """, height=55)

    list_vol_fin = st.session_state.get("list_volets_financier", [])
    if list_vol_fin:
        html_vol_det = '<table class="print-table" style="width: 100%;"><thead><tr><th>Catégorie / Poste</th><th>Description & Détail</th><th class="center-text">Quantité</th><th class="center-text">Prix Unitaire (DA)</th><th class="center-text">Total HT (DA)</th></tr></thead><tbody>'
        tot_vol_sec = 0.0
        for item in list_vol_fin:
            v_typ = item.get("Type", item.get("Élément", "-"))
            v_des = item.get("Description", item.get("Détail", "-"))
            v_qte = item.get("Quantité", 1)
            v_pu = item.get("Prix Unitaire (DA)", 0.0)
            v_tot = item.get("Total HT (DA)", item.get("Montant HT (DA)", 0.0))
            tot_vol_sec += v_tot
            html_vol_det += f'<tr><td><b>{v_typ}</b></td><td>{v_des}</td><td class="center-text" style="font-weight:bold;">{v_qte}</td><td class="center-text">{v_pu:,.2f} DA</td><td class="center-text" style="font-weight:bold; color:#047857;">{v_tot:,.2f} DA</td></tr>'
        html_vol_det += f'<tr style="background-color: #D1FAE5; font-weight: bold;"><td colspan="4" style="text-align: right;">TOTAL HT VOLETS ROULANTS :</td><td class="center-text" style="color:#065F46; font-size:16px;">{st.session_state.total_volets:,.2f} DA</td></tr></tbody></table>'
        st.markdown(html_vol_det, unsafe_allow_html=True)
    else:
        st.info("Aucun calcul de volet n'a été effectué pour le moment (rendez-vous dans le module '🏠 Volets Roulants').")

    # -------------------------------------------------------------
    # 5. DÉTAIL GARDE-CORPS
    # -------------------------------------------------------------
    st.markdown('<div style="page-break-before: always; margin-top: 30px;"></div>', unsafe_allow_html=True)
    st.markdown("### 🚧 5. Détail Garde-corps")

    titre_pdf_gc = f"Devis_Detail_Gardecorps_{NOM_PROJET}_{DATE_DU_JOUR}".replace(" ", "_").replace("'", "")
    components.html(f"""
        <button onclick="
            var oldTitle = window.parent.document.title; 
            window.parent.document.title = '{titre_pdf_gc}'; 
            setTimeout(function(){{ window.parent.print(); }}, 100);
            setTimeout(function(){{ window.parent.document.title = oldTitle; }}, 2000);
        " style="background-color: #7C3AED; color: white; padding: 10px 18px; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; font-size: 14px; width: 100%; margin-bottom: 10px;">
        🖨️ IMPRIMER CE DÉTAIL : DEVIS GARDE-CORPS (PDF)
        </button>
    """, height=55)

    if st.session_state.total_gardecorps > 0 or not st.session_state.df_garde_corps.empty:
        html_gc_det = '<table class="print-table" style="width: 100%;"><thead><tr><th>Composant Garde-corps</th><th>Détail / Dimensions</th><th class="center-text">Montant HT (DA)</th></tr></thead><tbody>'
        html_gc_det += f'<tr><td><b>Profilés Aluminium Garde-Corps</b></td><td>Débit barres 6m</td><td class="center-text" style="font-weight:bold; color:#047857;">{st.session_state.total_gardecorps:,.2f} DA</td></tr>'
        html_gc_det += f'<tr style="background-color: #EDE9FE; font-weight: bold;"><td colspan="2" style="text-align: right;">TOTAL HT GARDE-CORPS :</td><td class="center-text" style="color:#5B21B6; font-size:16px;">{st.session_state.total_gardecorps:,.2f} DA</td></tr></tbody></table>'
        st.markdown(html_gc_det, unsafe_allow_html=True)
    else:
        st.info("Aucun garde-corps renseigné ou calculé.")