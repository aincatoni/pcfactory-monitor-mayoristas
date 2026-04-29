#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pc Factory Mayorista Monitor (Ingram Micro)
Lee el price file de Ingram, cruza con la API de productos pc Factory,
y genera un dashboard HTML con los productos potenciales para publicar.
"""
import glob
import json
import time
import random
import argparse
import concurrent.futures as cf
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==============================================================================
# CONFIGURACION
# ==============================================================================

PRODUCT_API_BASE = "https://api.pcfactory.cl/pcfactory-services-catalogo/v1/catalogo/productos"
MAYORISTA_DIR = "mayorista"
PRICE_FILE_PATTERN = "CLPriceFile*.xlsx"

# Google Sheets
GOOGLE_SHEET_ID = "1mgGjhEmcE_c1q2xfJ4wgGpkcSD7A0jVCqD43h2382gc"
GOOGLE_SHEET_GID = "1606322296"          # pestaña: Todos los Productos Mayorista
PCF_CATALOG_GID  = "98635074"            # pestaña: Catalogo PCF
GOOGLE_SHEET_CSV_URL = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/export?format=csv&gid={GOOGLE_SHEET_GID}"
SEGUIMIENTO_SHEET_ID = "15V28Vnz_YFDECj_JEzWWp6snMlaMUgV6PVWROHioheM"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 15_6_1) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

# Columnas clave del XLSX (por nombre, no por indice)
COL_PCF_ID = "ID PRODUCTO PCF"
COL_INGRAM_PART = "SKU PRODUCTO"
COL_DESCRIPTION = "NOMBRE"
COL_VENDOR_NAME = "MARCA"
COL_VENDOR_PART = "PARTNO"
COL_CUSTOMER_PRICE = "COSTO"
COL_AVAILABLE_QTY = "STOCK"
COL_CATEGORY = "CATEGORIA"
COL_SUBCATEGORY = "TIPO"
COL_EAN = "EAN/UPC CODE"

# ==============================================================================
# FUNCIONES DE FECHA/HORA CHILE
# ==============================================================================

try:
    CHILE_TZ = ZoneInfo("America/Santiago")
except Exception:
    # Fallback defensivo si zoneinfo no esta disponible en el entorno.
    CHILE_TZ = timezone(timedelta(hours=-3))


def utc_to_chile(dt_utc):
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(CHILE_TZ)

def format_chile_timestamp(iso_timestamp):
    try:
        dt = datetime.fromisoformat(iso_timestamp.replace('Z', '+00:00'))
        dt_chile = utc_to_chile(dt)
        return dt_chile.strftime('%d/%m/%Y %H:%M:%S') + ' Chile'
    except:
        return iso_timestamp[:19] if iso_timestamp else 'N/A'

def get_chile_now():
    return utc_to_chile(datetime.now(timezone.utc))

# ==============================================================================
# SESION HTTP
# ==============================================================================

def create_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-CL,es;q=0.9,en-US;q=0.8,en;q=0.7",
    })
    retry = Retry(
        total=5,
        backoff_factor=0.8,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=15, pool_maxsize=15)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

def polite_pause(min_s: float = 0.2, max_s: float = 0.5):
    time.sleep(random.uniform(min_s, max_s))

def fetch_usd_clp() -> Optional[float]:
    """Obtiene el dolar observado. Intenta mindicador.cl, con fallback a exchangerate-api.com."""
    # Fuente 1: mindicador.cl
    try:
        resp = requests.get("https://mindicador.cl/api/dolar", timeout=10, verify=True)
        resp.raise_for_status()
        serie = resp.json().get("serie", [])
        if serie:
            return float(serie[0]["valor"])
    except Exception as e:
        print(f"[!] No se pudo obtener tipo de cambio USD (mindicador.cl): {e}")

    # Fuente 2: exchangerate-api.com (sin autenticacion, limite generoso)
    try:
        resp = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10)
        resp.raise_for_status()
        clp = resp.json().get("rates", {}).get("CLP")
        if clp:
            print(f"[+] Tipo de cambio obtenido desde exchangerate-api.com")
            return float(clp)
    except Exception as e:
        print(f"[!] No se pudo obtener tipo de cambio USD (exchangerate-api.com): {e}")

    return None

# ==============================================================================
# LECTURA DEL PRICE FILE
# ==============================================================================

def find_latest_price_file(mayorista_dir: str) -> Optional[str]:
    pattern = str(Path(mayorista_dir) / PRICE_FILE_PATTERN)
    files = glob.glob(pattern)
    if not files:
        return None
    return max(files, key=lambda f: Path(f).stat().st_mtime)

def read_price_file(filepath: str) -> pd.DataFrame:
    df = pd.read_excel(filepath, header=3)
    print(f"[+] Price file cargado: {filepath}")
    print(f"    {len(df)} productos, {len(df.columns)} columnas")
    return df

def read_google_sheet(sheet_id: str = GOOGLE_SHEET_ID, gid: str = GOOGLE_SHEET_GID) -> pd.DataFrame:
    """Lee un Google Sheet publico usando el endpoint gviz (mas confiable)."""
    import io
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet=Todos%20los%20Productos%20Mayorista"
    print(f"[*] Descargando Google Sheet...")
    try:
        session = requests.Session()
        session.headers.update({"User-Agent": UA})
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text), decimal=",", thousands=".")
        print(f"[+] Google Sheet cargado correctamente")
        print(f"    {len(df)} productos, {len(df.columns)} columnas")
        return df
    except Exception as e:
        print(f"[!] Error al leer Google Sheet: {e}")
        print(f"    Verifica que el sheet este compartido como 'Cualquiera con el enlace'")
        raise

def read_pcf_catalog_sheet(sheet_id: str = GOOGLE_SHEET_ID, gid: str = PCF_CATALOG_GID) -> pd.DataFrame:
    """Lee la pestaña Catalogo PCF desde Google Sheets."""
    import io
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet=sphinx"
    print(f"[*] Descargando Catalogo PCF desde Google Sheets...")
    try:
        session = requests.Session()
        session.headers.update({"User-Agent": UA})
        resp = session.get(url, timeout=60)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        df.columns = df.columns.str.strip()
        print(f"[+] Catalogo PCF cargado: {len(df)} productos, {len(df.columns)} columnas")
        return df
    except Exception as e:
        print(f"[!] Error al leer Catalogo PCF: {e}")
        raise


SOLOTODO_API = "https://api.solotodo.com"
SOLOTODO_PCF_STORE_ID = 12

def fetch_solotodo_prices(session: requests.Session, vendor_part: str) -> Dict[str, Any]:
    """Busca un producto en SoloTodo por part number y retorna precios de PCFactory, mínimo mercado y moda."""
    empty = {"solotodo_id": None, "pcf_price": None, "min_price": None, "mode_price": None, "mean_price": None}
    if not vendor_part or str(vendor_part).strip() in ("", "nan"):
        return empty
    try:
        # 1. Buscar producto por part number
        resp = session.get(
            f"{SOLOTODO_API}/products/",
            params={"part_number": str(vendor_part).strip(), "page_size": 1},
            timeout=15,
        )
        if not resp.ok:
            return empty
        data = resp.json()
        results = data.get("results", [])
        if not results:
            return empty
        product_id = results[0]["id"]

        # 2. Obtener todas las entidades (precios por tienda)
        resp2 = session.get(
            f"{SOLOTODO_API}/products/{product_id}/entities/",
            timeout=15,
        )
        if not resp2.ok:
            return {**empty, "solotodo_id": product_id}
        raw = resp2.json()
        entities = raw if isinstance(raw, list) else raw.get("results", [])

        pcf_price = None
        min_price = None
        all_prices = []

        for ent in entities:
            registry = ent.get("active_registry")
            if not registry or not registry.get("is_available"):
                continue
            try:
                offer = float(registry.get("offer_price") or registry.get("normal_price") or 0)
            except (ValueError, TypeError):
                continue
            if offer <= 0:
                continue
            price_int = int(offer)
            all_prices.append(price_int)
            store_url = str(ent.get("store", ""))
            if f"/stores/{SOLOTODO_PCF_STORE_ID}/" in store_url:
                pcf_price = price_int
            if min_price is None or price_int < min_price:
                min_price = price_int

        # Moda: precio más frecuente entre las tiendas (None si todos son únicos)
        mode_price = None
        if all_prices:
            counts = {}
            for p in all_prices:
                counts[p] = counts.get(p, 0) + 1
            max_count = max(counts.values())
            if max_count > 1:
                mode_price = max(p for p, c in counts.items() if c == max_count)

        # Media: promedio de precios disponibles
        mean_price = int(sum(all_prices) / len(all_prices)) if all_prices else None

        return {
            "solotodo_id": product_id,
            "pcf_price": pcf_price,
            "min_price": min_price,
            "mode_price": mode_price,
            "mean_price": mean_price,
        }
    except Exception:
        return empty

def enrich_with_solotodo(products: List[Dict], session: requests.Session, max_workers: int = 4) -> None:
    """Agrega campos solotodo_* a cada producto in-place. Solo para listas con vendor_part."""
    tasks = [(i, p) for i, p in enumerate(products) if p.get("vendor_part")]
    if not tasks:
        return
    print(f"[*] Consultando SoloTodo para {len(tasks)} productos...")
    completed = 0
    with cf.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(fetch_solotodo_prices, session, p["vendor_part"]): i
            for i, p in tasks
        }
        for future in cf.as_completed(futures):
            idx = futures[future]
            completed += 1
            if completed % 50 == 0 or completed == len(tasks):
                print(f"    [{completed}/{len(tasks)}] SoloTodo consultados...")
            try:
                result = future.result()
            except Exception:
                result = {"solotodo_id": None, "pcf_price": None, "min_price": None, "mode_price": None, "mean_price": None}
            products[idx].update(result)

def read_seguimiento_sheet(sheet_id: str = SEGUIMIENTO_SHEET_ID) -> Dict[str, str]:
    """
    Lee el sheet de seguimiento de Fichas/OC y devuelve un dict de lookup.
    Claves: str(pcf_id) y str(ingram_sku) → valor: status (OK, Pendiente, etc.)
    """
    import io
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&gid=0"
    print(f"[*] Descargando sheet de seguimiento Fichas/OC...")
    try:
        session = requests.Session()
        session.headers.update({"User-Agent": UA})
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        lookup: Dict[str, str] = {}
        for _, row in df.iterrows():
            status = str(row.get("Status", "")).strip()
            if not status or status == "nan":
                continue
            # Indexar por PCF ID (prefijo "pcf:" para evitar colisiones con SKU Ingram)
            pcf_id_raw = row.get("ID", "")
            if pd.notna(pcf_id_raw):
                try:
                    lookup[f"pcf:{int(float(pcf_id_raw))}"] = status
                except (ValueError, TypeError):
                    pass
            # Indexar por SKU Ingram (prefijo "sku:" para evitar colisiones con PCF ID)
            sku_raw = row.get("SKU Ingram", "")
            if pd.notna(sku_raw):
                try:
                    lookup[f"sku:{int(float(sku_raw))}"] = status
                except (ValueError, TypeError):
                    pass
        print(f"[+] Seguimiento cargado: {len(df)} entradas, {len(lookup)} claves indexadas")
        return lookup
    except Exception as e:
        print(f"[!] No se pudo cargar el sheet de seguimiento: {e}")
        return {}

def get_seguimiento_status(lookup: Dict[str, str], pcf_id, ingram_part) -> str:
    """Busca el status de seguimiento por PCF ID primero, luego por SKU Ingram."""
    if pcf_id is not None:
        try:
            key = f"pcf:{int(float(pcf_id))}"
            if key in lookup:
                return lookup[key]
        except (ValueError, TypeError):
            pass
    if ingram_part:
        try:
            key = f"sku:{int(float(str(ingram_part)))}"
            if key in lookup:
                return lookup[key]
        except (ValueError, TypeError):
            pass
    return ""

# ==============================================================================
# FILTROS XLSX
# ==============================================================================

# ==============================================================================
# CRUCE CON CATALOGO PCF
# ==============================================================================

def load_pcf_catalog(filepath: str) -> pd.DataFrame:
    """Carga el catálogo interno de PCFactory (XLS exportado del ERP, header en fila 6)."""
    df = pd.read_excel(filepath, header=5)
    df.columns = df.columns.str.strip()
    print(f"[+] Catálogo PCF cargado: {filepath}")
    print(f"    {len(df)} productos, {len(df.columns)} columnas")
    return df


def enrich_with_pcf_catalog(df_no_pcf: pd.DataFrame, catalog_df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Busca IDs PCF para los productos sin mapear usando PARTNO y EAN/UPC CODE.
    Solo usa matches no ambiguos (PARTNO o GTIN únicos en el catálogo).
    Retorna (df_con_ids_nuevos, df_aun_sin_id).
    """
    df = df_no_pcf.copy()

    cat = catalog_df.copy()
    cat['_partno_norm'] = cat['PARTNO'].astype(str).str.strip().str.upper()
    cat['_gtin_norm'] = cat['GTIN'].astype(str).str.strip().str.replace('.0', '', regex=False)

    df['_partno_norm'] = df[COL_VENDOR_PART].astype(str).str.strip().str.upper()
    df['_ean_norm'] = df[COL_EAN].astype(str).str.strip().str.replace('.0', '', regex=False)

    # Match 1: PARTNO único en catálogo
    partno_counts = cat['_partno_norm'].value_counts()
    partno_unique_set = set(partno_counts[partno_counts == 1].index) - {'NAN', ''}
    cat_by_partno = (
        cat[cat['_partno_norm'].isin(partno_unique_set)][['CODIGO', '_partno_norm']]
        .drop_duplicates('_partno_norm')
    )
    matched_partno = df.merge(cat_by_partno, on='_partno_norm', how='inner')
    matched_partno = matched_partno.copy()
    matched_partno[COL_PCF_ID] = matched_partno['CODIGO']

    unmatched = df[~df[COL_INGRAM_PART].isin(matched_partno[COL_INGRAM_PART])].copy()

    # Match 2: EAN/UPC único en catálogo
    gtin_counts = cat['_gtin_norm'].value_counts()
    gtin_unique_set = set(gtin_counts[gtin_counts == 1].index) - {'nan', ''}
    cat_by_gtin = (
        cat[cat['_gtin_norm'].isin(gtin_unique_set)][['CODIGO', '_gtin_norm']]
        .drop_duplicates('_gtin_norm')
    )
    has_ean = unmatched[unmatched['_ean_norm'].isin(gtin_unique_set)]
    matched_ean = has_ean.merge(cat_by_gtin, left_on='_ean_norm', right_on='_gtin_norm', how='inner')
    matched_ean = matched_ean.copy()
    matched_ean[COL_PCF_ID] = matched_ean['CODIGO']

    still_unmatched = unmatched[~unmatched[COL_INGRAM_PART].isin(matched_ean[COL_INGRAM_PART])].copy()

    # Limpiar columnas temporales
    tmp_cols = ['_partno_norm', '_ean_norm', '_gtin_norm', 'CODIGO']
    for col in tmp_cols:
        for frame in [matched_partno, matched_ean, still_unmatched]:
            if col in frame.columns:
                frame.drop(columns=[col], inplace=True, errors='ignore')

    n_partno = len(matched_partno)
    n_ean = len(matched_ean)
    print(f"[+] Cruce catálogo PCF: {n_partno} matches por PARTNO, {n_ean} por EAN/UPC")
    print(f"    {len(still_unmatched)} productos sin mapear (no existen en catálogo PCF)")

    all_matched = pd.concat([matched_partno, matched_ean], ignore_index=True)
    return all_matched, still_unmatched


def apply_xlsx_filters(df: pd.DataFrame) -> Dict[str, Any]:
    total = len(df)

    # Filtro 1: Stock Ingram > 0
    has_stock = df[df[COL_AVAILABLE_QTY].fillna(0) > 0].copy()
    sin_stock = total - len(has_stock)

    # Filtro 2: No elegibles (BAD BOX / OPEN BOX / CAJA DAÑADA / CAJA ABIERTA /
    # CAJA DETERIORADA / REF / REFURBISHED en el nombre)
    import unicodedata
    def _normalize(s):
        return unicodedata.normalize('NFD', str(s)).encode('ascii', 'ignore').decode('ascii').upper()
    desc_norm = has_stock[COL_DESCRIPTION].apply(_normalize)
    is_no_eligible = desc_norm.str.contains(
        r'BAD BOX|OPEN BOX|CAJA DANADA|CAJA ABIERTA|CAJA DETERIORADA|\bREF\b|REFURBISHED',
        na=False,
    )
    no_eligible_df = has_stock[is_no_eligible].copy()
    eligible_xlsx = has_stock[~is_no_eligible].copy()

    # Separar por si tienen PCF ID o no
    # El campo puede contener "Sin ID" u otros textos no numericos
    def is_valid_pcf_id(val):
        if pd.isna(val) or str(val).strip() == '':
            return False
        s = str(val).strip().lower()
        if s in ('sin id', 'n/a', 'na', '-', 'none'):
            return False
        try:
            int(float(s))
            return True
        except (ValueError, TypeError):
            return False

    mask_valid_id = eligible_xlsx[COL_PCF_ID].apply(is_valid_pcf_id)
    has_pcf_id = eligible_xlsx[mask_valid_id].copy()
    no_pcf_id = eligible_xlsx[~mask_valid_id].copy()

    sin_stock_df = df[df[COL_AVAILABLE_QTY].fillna(0) <= 0].copy()

    return {
        "total": total,
        "sin_stock_ingram": sin_stock,
        "no_eligible": len(no_eligible_df),
        "no_eligible_df": no_eligible_df,
        "has_stock": eligible_xlsx,
        "sin_stock_df": sin_stock_df,
        "eligible_xlsx": eligible_xlsx,
        "has_pcf_id": has_pcf_id,
        "no_pcf_id": no_pcf_id,
    }

# ==============================================================================
# CONSULTA API pcFACTORY
# ==============================================================================

def is_description_empty(description: Any) -> bool:
    if not description:
        return True
    s = str(description).strip()
    if not s:
        return True
    # Considerar vacia si solo tiene tags HTML sin contenido real
    import re
    text_only = re.sub(r'<[^>]+>', '', s).strip()
    return len(text_only) < 20

def parse_stock_aproximado(stock_data: Any) -> int:
    if stock_data is None:
        return 0
    if isinstance(stock_data, dict):
        aprox = stock_data.get("aproximado", "0")
    else:
        aprox = str(stock_data)
    aprox = str(aprox).strip()
    if aprox.startswith("+"):
        try:
            return int(aprox[1:])
        except ValueError:
            return 0
    try:
        return int(aprox)
    except ValueError:
        return 0

def check_product_api(session: requests.Session, pcf_id: int) -> Dict[str, Any]:
    polite_pause()
    try:
        url = f"{PRODUCT_API_BASE}/{int(pcf_id)}"
        resp = session.get(url, timeout=20)

        if resp.status_code == 429 and "Retry-After" in resp.headers:
            try:
                wait = int(resp.headers["Retry-After"])
                time.sleep(min(wait, 20))
            except:
                pass

        if resp.ok:
            data = resp.json()
            mayorista = data.get("mayorista", False)
            lista = str(data.get("lista", "0"))
            stock_data = data.get("stock", {})
            stock_aprox = parse_stock_aproximado(stock_data)
            nombre_pcf = data.get("nombre", "")
            precio_normal = data.get("precioNormal", 0)
            precio_oferta = data.get("precioOferta", 0)
            description_pcf = data.get("descripcion", "")
            marca_id = data.get("marca", {}).get("id")
            # HP (id 73) usa contenido indexado por la marca; no se requiere descripcion propia
            HP_MARCA_ID = 73
            ficha_vacia = False if marca_id == HP_MARCA_ID else is_description_empty(description_pcf)
            stock_detail = {}
            if isinstance(stock_data, dict):
                stock_detail = {k: v for k, v in stock_data.items() if k != "aproximado"}
            return {
                "api_status": "ok",
                "mayorista": mayorista,
                "lista": lista,
                "stock_pcf": stock_aprox,
                "stock_raw": stock_data.get("aproximado", "0") if isinstance(stock_data, dict) else str(stock_data),
                "nombre_pcf": nombre_pcf,
                "precio_normal": precio_normal,
                "precio_oferta": precio_oferta,
                "ficha_vacia": ficha_vacia,
                "stock_detail": stock_detail,
                "error": "",
            }
        elif resp.status_code == 404:
            return {
                "api_status": "not_found",
                "mayorista": False,
                "lista": "0",
                "stock_pcf": 0,
                "stock_raw": "0",
                "nombre_pcf": "",
                "precio_normal": 0,
                "precio_oferta": 0,
                "ficha_vacia": True,
                "stock_detail": {},
                "error": "",
            }
        else:
            return {
                "api_status": "error",
                "mayorista": None,
                "lista": None,
                "stock_pcf": None,
                "stock_raw": "",
                "nombre_pcf": "",
                "precio_normal": 0,
                "precio_oferta": 0,
                "ficha_vacia": None,
                "stock_detail": {},
                "error": f"HTTP {resp.status_code}",
            }
    except requests.RequestException as e:
        return {
            "api_status": "error",
            "mayorista": None,
            "lista": None,
            "stock_pcf": None,
            "stock_raw": "",
            "nombre_pcf": "",
            "precio_normal": 0,
            "precio_oferta": 0,
            "ficha_vacia": None,
            "stock_detail": {},
            "error": str(e),
        }

def check_products_batch(session: requests.Session, df_with_ids: pd.DataFrame, max_workers: int = 5) -> List[Dict]:
    results = []
    tasks = []
    for _, row in df_with_ids.iterrows():
        pcf_id = row[COL_PCF_ID]
        try:
            pcf_id_int = int(float(pcf_id))
        except (ValueError, TypeError):
            continue
        tasks.append((pcf_id_int, row))

    print(f"[*] Consultando API para {len(tasks)} productos con PCF ID...")
    completed = 0

    with cf.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(check_product_api, session, pid): (pid, row)
            for pid, row in tasks
        }
        for future in cf.as_completed(futures):
            pid, row = futures[future]
            completed += 1
            if completed % 50 == 0 or completed == len(tasks):
                print(f"    [{completed}/{len(tasks)}] consultados...")
            try:
                api_result = future.result()
            except Exception as e:
                api_result = {
                    "api_status": "error", "mayorista": None, "lista": None,
                    "stock_pcf": None, "stock_raw": "", "nombre_pcf": "",
                    "precio_normal": 0, "precio_oferta": 0,
                    "ficha_vacia": None, "stock_detail": {}, "error": str(e),
                }
            results.append({
                "pcf_id": pid,
                "ingram_part": row.get(COL_INGRAM_PART, ""),
                "description": row.get(COL_DESCRIPTION, ""),
                "vendor_name": row.get(COL_VENDOR_NAME, ""),
                "vendor_part": row.get(COL_VENDOR_PART, ""),
                "customer_price": row.get(COL_CUSTOMER_PRICE, 0),
                "available_qty": row.get(COL_AVAILABLE_QTY, 0),
                "category": row.get(COL_CATEGORY, ""),
                "subcategory": row.get(COL_SUBCATEGORY, ""),
                **api_result,
            })

    return results

# ==============================================================================
# CLASIFICACION FINAL
# ==============================================================================

def classify_products(api_results: List[Dict], df_no_pcf_id: pd.DataFrame, seguimiento: Dict = None) -> Dict[str, Any]:
    seg = seguimiento or {}
    # Con ficha listos para publicar (PCF ID + mayorista=false + stock_pcf=0 + tiene ficha)
    publish_ready = []
    # Ficha vacía pero ya solicitada en seguimiento (esperando que eCommerce la publique)
    pending_ficha = []
    # Seguimiento dice "OK" pero la API aún muestra ficha vacía (posible desincronización)
    ficha_ok = []
    # Ficha vacía y no solicitada (acción requerida: pedir ficha)
    missing_ficha = []
    # Publicados - ya mayorista en lista 1
    already_mayorista = []
    # Con stock PCF (excluidos)
    has_pcf_stock = []
    # Requieren creacion (API 404 - id no existe en PCFactory)
    need_creation = []
    # Errores API
    api_errors = []

    for item in api_results:
        if item["api_status"] == "error":
            api_errors.append(item)
            continue
        if item["api_status"] == "not_found":
            need_creation.append(item)
            continue
        if item["mayorista"] is True and item.get("lista") == "1":
            already_mayorista.append(item)
            continue
        if item["stock_pcf"] is not None and item["stock_pcf"] > 0:
            has_pcf_stock.append(item)
            continue
        # Aquí solo llegan productos potenciales: lista "0", sin stock PCF
        if item.get("ficha_vacia", False):
            status = get_seguimiento_status(seg, item.get("pcf_id"), item.get("ingram_part"))
            if status and status.upper() == "OK":
                ficha_ok.append(item)       # OK en seguimiento pero API aún la muestra vacía
            elif status:
                pending_ficha.append(item)  # Pendiente / Ficha Básica / Ficha Antigua
            else:
                missing_ficha.append(item)  # Sin registro en seguimiento
        else:
            publish_ready.append(item)

    # Tambien requieren creacion: productos sin PCF ID en el price file
    for _, row in df_no_pcf_id.iterrows():
        need_creation.append({
            "pcf_id": None,
            "ingram_part": row.get(COL_INGRAM_PART, ""),
            "description": row.get(COL_DESCRIPTION, ""),
            "vendor_name": row.get(COL_VENDOR_NAME, ""),
            "vendor_part": row.get(COL_VENDOR_PART, ""),
            "customer_price": row.get(COL_CUSTOMER_PRICE, 0),
            "available_qty": row.get(COL_AVAILABLE_QTY, 0),
            "category": row.get(COL_CATEGORY, ""),
            "subcategory": row.get(COL_SUBCATEGORY, ""),
        })

    return {
        "publish_ready": publish_ready,
        "pending_ficha": pending_ficha,
        "ficha_ok": ficha_ok,
        "missing_ficha": missing_ficha,
        "need_creation": need_creation,
        "already_mayorista": already_mayorista,
        "has_pcf_stock": has_pcf_stock,
        "api_errors": api_errors,
    }

# ==============================================================================
# EXPORTACION EXCEL
# ==============================================================================

def generate_excel_report(
    classification: Dict,
    usd_clp: Optional[float],
    seguimiento: Optional[Dict[str, str]],
    output_path: str,
) -> None:
    """Genera un .xlsx con los datos procesados en hojas separadas."""
    _seg = seguimiento or {}

    def clp_value(price):
        if usd_clp is None:
            return None
        try:
            return int((usd_clp + 5) * float(price))
        except (ValueError, TypeError):
            return None

    def seg_status(pcf_id, ingram_part):
        return get_seguimiento_status(_seg, pcf_id, ingram_part)

    def build_rows(products, grupo):
        rows = []
        for p in products:
            price = p.get("customer_price", 0)
            rows.append({
                "Estado":            grupo,
                "PCF ID":            p.get("pcf_id", ""),
                "Ingram Part":       p.get("ingram_part", ""),
                "Descripcion":       p.get("description", ""),
                "Vendor":            p.get("vendor_name", ""),
                "Part Number":       p.get("vendor_part", ""),
                "Stock Ingram":      p.get("available_qty", 0),
                "Costo USD":         price,
                "Costo CLP":         clp_value(price),
                "PCF SoloTodo":      p.get("pcf_price"),
                "Min. Mercado":      p.get("min_price"),
                "Media Mercado":     p.get("mean_price"),
                "Moda Mercado":      p.get("mode_price"),
                "Solicitud Ficha":   seg_status(p.get("pcf_id"), p.get("ingram_part")),
                "Categoria":         p.get("category", ""),
            })
        return rows

    publish_rows  = build_rows(classification.get("publish_ready", []),   "Con Ficha Listo")
    pending_rows  = build_rows(classification.get("pending_ficha", []),   "Ficha Solicitada")
    ficha_ok_rows = build_rows(classification.get("ficha_ok", []),         "Ficha necesita revisión")
    ficha_rows    = build_rows(classification.get("missing_ficha", []),    "Sin Ficha Solicitada")
    creation_rows = build_rows(classification.get("need_creation", []),    "ID No Existe")
    mayorista_rows = build_rows(classification.get("already_mayorista", []), "Publicado Lista 1")

    all_potenciales = publish_rows + pending_rows + ficha_ok_rows + ficha_rows + creation_rows

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        if all_potenciales:
            pd.DataFrame(all_potenciales).to_excel(writer, sheet_name="Potenciales", index=False)
        if publish_rows:
            pd.DataFrame(publish_rows).to_excel(writer, sheet_name="Con Ficha Listos", index=False)
        if pending_rows:
            pd.DataFrame(pending_rows).to_excel(writer, sheet_name="Ficha Solicitada", index=False)
        if ficha_ok_rows:
            pd.DataFrame(ficha_ok_rows).to_excel(writer, sheet_name="Ficha necesita revisión", index=False)
        if ficha_rows:
            pd.DataFrame(ficha_rows).to_excel(writer, sheet_name="Sin Ficha", index=False)
        if creation_rows:
            pd.DataFrame(creation_rows).to_excel(writer, sheet_name="ID No Existe", index=False)
        if mayorista_rows:
            pd.DataFrame(mayorista_rows).to_excel(writer, sheet_name="Publicados", index=False)

    print(f"[+] Excel generado: {output_path}")

# ==============================================================================
# GENERADOR DE DASHBOARD HTML
# ==============================================================================

def generate_html_dashboard(
    xlsx_stats: Dict,
    classification: Dict,
    price_file_name: str,
    timestamp: str,
    df_original: pd.DataFrame = None,
    usd_clp: Optional[float] = None,
    seguimiento: Optional[Dict[str, str]] = None,
    price_file_url: str = None,
) -> str:
    if df_original is None:
        df_original = pd.DataFrame()
    timestamp_display = format_chile_timestamp(timestamp)

    # Construir el nombre a mostrar con URL si está disponible
    if price_file_url:
        price_file_name_display = f'<a href="{price_file_url}" target="_blank" style="color:#2563eb;text-decoration:underline;">{price_file_name}</a>'
    else:
        price_file_name_display = price_file_name

    # Lookup vendor_part -> SoloTodo data (from all enriched classified products)
    _all_classified = []
    for bucket in classification.values():
        if isinstance(bucket, list):
            _all_classified.extend(bucket)
    solotodo_lookup: Dict[str, Dict] = {
        p["vendor_part"]: {"pcf_price": p.get("pcf_price"), "min_price": p.get("min_price"), "mean_price": p.get("mean_price"), "mode_price": p.get("mode_price"), "solotodo_id": p.get("solotodo_id")}
        for p in _all_classified if p.get("vendor_part") and (p.get("pcf_price") is not None or p.get("min_price") is not None or p.get("solotodo_id") is not None)
    }

    def fmt_usd(price) -> str:
        try:
            return f"$ {float(price):,.2f}"
        except (ValueError, TypeError):
            return "—"

    def fmt_clp(price) -> str:
        if usd_clp is None:
            return "—"
        try:
            clp = int((usd_clp + 5) * float(price))
            return f"$ {clp:,}".replace(",", ".")
        except (ValueError, TypeError):
            return "—"

    usd_info_html = (
        f'<div class="file-info">💱 USD observado: <strong>${usd_clp:,.0f} CLP</strong> &nbsp;·&nbsp; Precio CLP = (USD observado + $5) × Costo</div>'
        if usd_clp else ""
    )

    _seg = seguimiento or {}
    _STATUS_BADGE = {
        "OK":            ("badge-green",  "OK"),
        "Pendiente":     ("badge-yellow", "Pendiente"),
        "Ficha Básica":  ("badge-yellow", "Ficha Básica"),
        "Ficha Antigua": ("badge-red",    "Ficha Antigua"),
    }

    _SEGUIMIENTO_URL = f"https://docs.google.com/spreadsheets/d/{SEGUIMIENTO_SHEET_ID}"

    def fmt_seguimiento(pcf_id, ingram_part) -> str:
        status = get_seguimiento_status(_seg, pcf_id, ingram_part)
        if not status:
            return '<span style="color: var(--text-muted);">No Solicitada</span>'
        cls, label = _STATUS_BADGE.get(status, ("badge-blue", status))
        return f'<a href="{_SEGUIMIENTO_URL}" target="_blank" style="text-decoration:none;"><span class="table-badge {cls}">{label} ↗</span></a>'

    def fmt_clp_price(price_clp) -> str:
        if price_clp is None:
            return '<span style="color: var(--text-muted);">—</span>'
        try:
            return f"$ {int(price_clp):,}".replace(",", ".")
        except (ValueError, TypeError):
            return '<span style="color: var(--text-muted);">—</span>'

    def fmt_pcf_price_colored(pcf_price, mode_price) -> str:
        if pcf_price is None:
            return '<span style="color: var(--text-muted);">—</span>'
        try:
            val = int(pcf_price)
            formatted = f"$ {val:,}".replace(",", ".")
            if mode_price is None:
                return formatted
            mode_val = int(mode_price)
            if val > mode_val:
                color = "#ef4444"   # rojo — sobre la moda
            elif val == mode_val:
                color = "#eab308"   # amarillo — en la moda
            else:
                color = "#22c55e"   # verde — bajo la moda
            return f'<span style="color: {color}; font-weight: 600;">{formatted}</span>'
        except (ValueError, TypeError):
            return '<span style="color: var(--text-muted);">—</span>'

    def fmt_solotodo_link(solotodo_id) -> str:
        if solotodo_id is None:
            return '<span style="color: var(--text-muted);">—</span>'
        url = f"https://www.solotodo.cl/products/{solotodo_id}"
        return f'<a href="{url}" target="_blank" style="color: var(--accent-cyan); text-decoration: none; font-size: 0.8rem;">Ver ↗</a>'

    total = xlsx_stats["total"]
    sin_stock = xlsx_stats["sin_stock_ingram"]
    publish_ready = classification["publish_ready"]
    pending_ficha = classification.get("pending_ficha", [])
    ficha_ok      = classification.get("ficha_ok", [])
    missing_ficha = classification.get("missing_ficha", [])
    need_creation = classification["need_creation"]
    already_mayorista = classification["already_mayorista"]
    has_pcf_stock = classification["has_pcf_stock"]
    api_errors = classification["api_errors"]

    no_eligible = xlsx_stats.get("no_eligible", 0)
    no_eligible_df = xlsx_stats.get("no_eligible_df", pd.DataFrame())
    has_stock_df = xlsx_stats.get("has_stock", pd.DataFrame())
    sin_stock_df = xlsx_stats.get("sin_stock_df", pd.DataFrame())

    total_potencial = len(publish_ready) + len(pending_ficha) + len(ficha_ok) + len(missing_ficha) + len(need_creation)

    # Status
    if total_potencial > 100:
        status_class = "healthy"
        status_text = f"{total_potencial} productos potenciales detectados"
        status_color = "#10b981"
    elif total_potencial > 0:
        status_class = "warning"
        status_text = f"{total_potencial} productos potenciales detectados"
        status_color = "#f59e0b"
    else:
        status_class = "critical"
        status_text = "No se detectaron productos potenciales"
        status_color = "#ef4444"

    # Funnel data
    with_stock = total - sin_stock  # stock > 0
    after_api_filters = total_potencial

    # Verificación de coherencia del funnel
    with_stock_check = (
        len(already_mayorista) +
        total_potencial +
        len(has_pcf_stock) +
        no_eligible +
        len(api_errors)
    )
    if with_stock != with_stock_check:
        print(f"[!] AVISO funnel: with_stock={with_stock} vs suma={with_stock_check} (diff={with_stock - with_stock_check})")

    # --- Tablas ---

    # Tabla Publicacion Inmediata
    publish_rows = ""
    for i, p in enumerate(sorted(publish_ready, key=lambda x: x.get("vendor_name", "")), 1):
        pcf_link = f'<a href="https://www.pcfactory.cl/producto/{p["pcf_id"]}" target="_blank" style="color: var(--accent-blue); text-decoration: none;">{p["pcf_id"]}</a>'
        publish_rows += f'''<tr>
            <td>{i}</td>
            <td>{pcf_link}</td>
            <td class="desc-cell" title="{p["description"]}">{p["description"][:35]}{"..." if len(str(p["description"])) > 35 else ""}</td>
            <td>{p["vendor_name"]}</td>
            <td><code>{p["vendor_part"]}</code></td>
            <td class="num-cell">{p["available_qty"]}</td>
            <td class="num-cell">{fmt_usd(p.get("customer_price", 0))}</td>
            <td class="num-cell">{fmt_clp(p.get("customer_price", 0))}</td>
            <td class="num-cell">{fmt_pcf_price_colored(p.get("pcf_price"), p.get("mode_price"))}</td>
            <td class="num-cell">{fmt_clp_price(p.get("min_price"))}</td>
            <td class="num-cell">{fmt_clp_price(p.get("mean_price"))}</td>
            <td class="num-cell">{fmt_clp_price(p.get("mode_price"))}</td>
            <td class="num-cell">{fmt_solotodo_link(p.get("solotodo_id"))}</td>
            <td>{fmt_seguimiento(p.get("pcf_id"), p.get("ingram_part"))}</td>
            <td>{p["category"]}</td>
        </tr>'''

    # Tabla ID Existente Sin Ficha Solicitada
    ficha_rows = ""
    for i, p in enumerate(sorted(missing_ficha, key=lambda x: x.get("vendor_name", "")), 1):
        pcf_link = f'<a href="https://www.pcfactory.cl/producto/{p["pcf_id"]}" target="_blank" style="color: var(--accent-blue); text-decoration: none;">{p["pcf_id"]}</a>'
        ficha_rows += f'''<tr>
            <td>{i}</td>
            <td>{pcf_link}</td>
            <td class="desc-cell" title="{p["description"]}">{p["description"][:35]}{"..." if len(str(p["description"])) > 35 else ""}</td>
            <td>{p["vendor_name"]}</td>
            <td><code>{p["vendor_part"]}</code></td>
            <td class="num-cell">{p["available_qty"]}</td>
            <td class="num-cell">{fmt_usd(p.get("customer_price", 0))}</td>
            <td class="num-cell">{fmt_clp(p.get("customer_price", 0))}</td>
            <td class="num-cell">{fmt_pcf_price_colored(p.get("pcf_price"), p.get("mode_price"))}</td>
            <td class="num-cell">{fmt_clp_price(p.get("min_price"))}</td>
            <td class="num-cell">{fmt_clp_price(p.get("mean_price"))}</td>
            <td class="num-cell">{fmt_clp_price(p.get("mode_price"))}</td>
            <td class="num-cell">{fmt_solotodo_link(p.get("solotodo_id"))}</td>
            <td>{fmt_seguimiento(p.get("pcf_id"), p.get("ingram_part"))}</td>
            <td>{p["category"]}</td>
        </tr>'''

    # Tabla Ficha necesita revisión
    fichaok_rows = ""
    for i, p in enumerate(sorted(ficha_ok, key=lambda x: x.get("vendor_name", "")), 1):
        pcf_link = f'<a href="https://www.pcfactory.cl/producto/{p["pcf_id"]}" target="_blank" style="color: var(--accent-blue); text-decoration: none;">{p["pcf_id"]}</a>'
        fichaok_rows += f'''<tr>
            <td>{i}</td>
            <td>{pcf_link}</td>
            <td class="desc-cell" title="{p["description"]}">{p["description"][:35]}{"..." if len(str(p["description"])) > 35 else ""}</td>
            <td>{p["vendor_name"]}</td>
            <td><code>{p["vendor_part"]}</code></td>
            <td class="num-cell">{p["available_qty"]}</td>
            <td class="num-cell">{fmt_usd(p.get("customer_price", 0))}</td>
            <td class="num-cell">{fmt_clp(p.get("customer_price", 0))}</td>
            <td class="num-cell">{fmt_pcf_price_colored(p.get("pcf_price"), p.get("mode_price"))}</td>
            <td class="num-cell">{fmt_clp_price(p.get("min_price"))}</td>
            <td class="num-cell">{fmt_clp_price(p.get("mean_price"))}</td>
            <td class="num-cell">{fmt_clp_price(p.get("mode_price"))}</td>
            <td class="num-cell">{fmt_solotodo_link(p.get("solotodo_id"))}</td>
            <td>{fmt_seguimiento(p.get("pcf_id"), p.get("ingram_part"))}</td>
            <td>{p["category"]}</td>
        </tr>'''

    # Tabla Ficha Solicitada - Esperando que eCommerce la publique
    pending_rows = ""
    for i, p in enumerate(sorted(pending_ficha, key=lambda x: x.get("vendor_name", "")), 1):
        pcf_link = f'<a href="https://www.pcfactory.cl/producto/{p["pcf_id"]}" target="_blank" style="color: var(--accent-blue); text-decoration: none;">{p["pcf_id"]}</a>'
        pending_rows += f'''<tr>
            <td>{i}</td>
            <td>{pcf_link}</td>
            <td class="desc-cell" title="{p["description"]}">{p["description"][:35]}{"..." if len(str(p["description"])) > 35 else ""}</td>
            <td>{p["vendor_name"]}</td>
            <td><code>{p["vendor_part"]}</code></td>
            <td class="num-cell">{p["available_qty"]}</td>
            <td class="num-cell">{fmt_usd(p.get("customer_price", 0))}</td>
            <td class="num-cell">{fmt_clp(p.get("customer_price", 0))}</td>
            <td class="num-cell">{fmt_pcf_price_colored(p.get("pcf_price"), p.get("mode_price"))}</td>
            <td class="num-cell">{fmt_clp_price(p.get("min_price"))}</td>
            <td class="num-cell">{fmt_clp_price(p.get("mean_price"))}</td>
            <td class="num-cell">{fmt_clp_price(p.get("mode_price"))}</td>
            <td class="num-cell">{fmt_solotodo_link(p.get("solotodo_id"))}</td>
            <td>{fmt_seguimiento(p.get("pcf_id"), p.get("ingram_part"))}</td>
            <td>{p["category"]}</td>
        </tr>'''

    # Tabla ID No Existe y Requieren Creacion
    creation_rows = ""
    for i, p in enumerate(sorted(need_creation, key=lambda x: x.get("vendor_name", "")), 1):
        creation_rows += f'''<tr>
            <td>{i}</td>
            <td><code>{p["ingram_part"]}</code></td>
            <td class="desc-cell" title="{p["description"]}">{p["description"][:35]}{"..." if len(str(p["description"])) > 35 else ""}</td>
            <td>{p["vendor_name"]}</td>
            <td><code>{p["vendor_part"]}</code></td>
            <td class="num-cell">{p["available_qty"]}</td>
            <td class="num-cell">{fmt_usd(p.get("customer_price", 0))}</td>
            <td class="num-cell">{fmt_clp(p.get("customer_price", 0))}</td>
            <td>{fmt_seguimiento(p.get("pcf_id"), p.get("ingram_part"))}</td>
            <td>{p["category"]}</td>
        </tr>'''

    # Tabla Ya Mayorista
    mayorista_rows = ""
    for i, p in enumerate(sorted(already_mayorista, key=lambda x: x.get("vendor_name", "")), 1):
        pcf_link = f'<a href="https://www.pcfactory.cl/producto/{p["pcf_id"]}" target="_blank" style="color: var(--accent-blue); text-decoration: none;">{p["pcf_id"]}</a>'
        stock_display = p.get("stock_raw", "0")
        mayorista_rows += f'''<tr>
            <td>{i}</td>
            <td>{pcf_link}</td>
            <td class="desc-cell" title="{p["description"]}">{p["description"][:35]}{"..." if len(str(p["description"])) > 35 else ""}</td>
            <td>{p["vendor_name"]}</td>
            <td class="num-cell">{p["available_qty"]}</td>
            <td class="num-cell">{stock_display}</td>
            <td class="num-cell">{fmt_usd(p.get("customer_price", 0))}</td>
            <td class="num-cell">{fmt_clp(p.get("customer_price", 0))}</td>
            <td class="num-cell">{fmt_pcf_price_colored(p.get("pcf_price"), p.get("mode_price"))}</td>
            <td class="num-cell">{fmt_clp_price(p.get("min_price"))}</td>
            <td class="num-cell">{fmt_clp_price(p.get("mean_price"))}</td>
            <td class="num-cell">{fmt_clp_price(p.get("mode_price"))}</td>
            <td class="num-cell">{fmt_solotodo_link(p.get("solotodo_id"))}</td>
        </tr>'''

    def format_stock_detail(detail: dict) -> str:
        if not detail:
            return "—"
        parts = []
        for key, val in detail.items():
            if val and str(val) not in ("0", "false", "False", ""):
                parts.append(f"{key}: {val}")
        return " | ".join(parts) if parts else "—"

    # Tabla Con Stock PCF
    pcf_stock_rows = ""
    for i, p in enumerate(sorted(has_pcf_stock, key=lambda x: x.get("vendor_name", "")), 1):
        pcf_link = f'<a href="https://www.pcfactory.cl/producto/{p["pcf_id"]}" target="_blank" style="color: var(--accent-blue); text-decoration: none;">{p["pcf_id"]}</a>'
        stock_display = p.get("stock_raw", "0")
        pcf_stock_rows += f'''<tr>
            <td>{i}</td>
            <td>{pcf_link}</td>
            <td class="desc-cell" title="{p["description"]}">{p["description"][:35]}{"..." if len(str(p["description"])) > 35 else ""}</td>
            <td>{p["vendor_name"]}</td>
            <td class="num-cell">{p["available_qty"]}</td>
            <td class="num-cell">{stock_display}</td>
            <td class="num-cell">{fmt_usd(p.get("customer_price", 0))}</td>
            <td class="num-cell">{fmt_clp(p.get("customer_price", 0))}</td>
            <td class="desc-cell">{format_stock_detail(p.get("stock_detail", {}))}</td>
        </tr>'''

    # Tabla Potenciales (union de publish_ready + missing_ficha + need_creation)
    potenciales_rows = ""
    potenciales_all = []
    for p in publish_ready:
        potenciales_all.append({**p, "_estado": "Con Ficha Listo", "_estado_class": "badge-green"})
    for p in pending_ficha:
        potenciales_all.append({**p, "_estado": "Ficha Solicitada", "_estado_class": "badge-cyan"})
    for p in ficha_ok:
        potenciales_all.append({**p, "_estado": "Ficha necesita revisión", "_estado_class": "badge-blue"})
    for p in missing_ficha:
        potenciales_all.append({**p, "_estado": "Sin Ficha Solicitada", "_estado_class": "badge-yellow"})
    for p in need_creation:
        potenciales_all.append({**p, "_estado": "ID No Existe", "_estado_class": "badge-purple"})
    for i, p in enumerate(sorted(potenciales_all, key=lambda x: x.get("vendor_name", "")), 1):
        pcf_id_val = p.get("pcf_id", "")
        if pcf_id_val:
            id_cell = f'<a href="https://www.pcfactory.cl/producto/{pcf_id_val}" target="_blank" style="color: var(--accent-blue); text-decoration: none;">{pcf_id_val}</a>'
        else:
            id_cell = '<span style="color: var(--text-muted);">—</span>'
        desc = str(p.get("description", ""))
        potenciales_rows += f'''<tr>
            <td>{i}</td>
            <td>{id_cell}</td>
            <td class="desc-cell" title="{desc}">{desc[:35]}{"..." if len(desc) > 35 else ""}</td>
            <td>{p.get("vendor_name", "")}</td>
            <td><code>{p.get("vendor_part", "")}</code></td>
            <td class="num-cell">{p.get("available_qty", 0)}</td>
            <td class="num-cell">{fmt_usd(p.get("customer_price", 0))}</td>
            <td class="num-cell">{fmt_clp(p.get("customer_price", 0))}</td>
            <td class="num-cell">{fmt_pcf_price_colored(p.get("pcf_price"), p.get("mode_price"))}</td>
            <td class="num-cell">{fmt_clp_price(p.get("min_price"))}</td>
            <td class="num-cell">{fmt_clp_price(p.get("mean_price"))}</td>
            <td class="num-cell">{fmt_clp_price(p.get("mode_price"))}</td>
            <td class="num-cell">{fmt_solotodo_link(p.get("solotodo_id"))}</td>
            <td>{fmt_seguimiento(p.get("pcf_id"), p.get("ingram_part"))}</td>
            <td><span class="table-badge {p["_estado_class"]}">{p["_estado"]}</span></td>
        </tr>'''


    # Tabla No Elegibles (BAD BOX / OPEN BOX / CAJA / REF)
    no_eligible_rows = ""
    if not no_eligible_df.empty:
        for i, (_, row) in enumerate(no_eligible_df.iterrows(), 1):
            desc = str(row.get(COL_DESCRIPTION, ""))
            no_eligible_rows += f'''<tr>
            <td>{i}</td>
            <td><code>{row.get(COL_INGRAM_PART, "")}</code></td>
            <td class="desc-cell" title="{desc}">{desc[:35]}{"..." if len(desc) > 35 else ""}</td>
            <td>{row.get(COL_VENDOR_NAME, "")}</td>
            <td><code>{row.get(COL_VENDOR_PART, "")}</code></td>
            <td class="num-cell">{row.get(COL_AVAILABLE_QTY, 0)}</td>
            <td class="num-cell">{fmt_usd(row.get(COL_CUSTOMER_PRICE, 0))}</td>
            <td class="num-cell">{fmt_clp(row.get(COL_CUSTOMER_PRICE, 0))}</td>
            <td>{row.get(COL_CATEGORY, "")}</td>
        </tr>'''

    # Tabla Total Productos (todos los del price file)
    total_rows = ""
    for i, (_, row) in enumerate(df_original.iterrows(), 1):
        desc = str(row.get(COL_DESCRIPTION, ""))
        qty = row.get(COL_AVAILABLE_QTY, 0)
        vpart = str(row.get(COL_VENDOR_PART, ""))
        _st = solotodo_lookup.get(vpart, {})
        total_rows += f'''<tr>
            <td>{i}</td>
            <td><code>{row.get(COL_INGRAM_PART, "")}</code></td>
            <td class="desc-cell" title="{desc}">{desc[:35]}{"..." if len(desc) > 35 else ""}</td>
            <td>{row.get(COL_VENDOR_NAME, "")}</td>
            <td><code>{vpart}</code></td>
            <td class="num-cell">{qty}</td>
            <td class="num-cell">{fmt_usd(row.get(COL_CUSTOMER_PRICE, 0))}</td>
            <td class="num-cell">{fmt_clp(row.get(COL_CUSTOMER_PRICE, 0))}</td>
            <td class="num-cell">{fmt_pcf_price_colored(_st.get("pcf_price"), _st.get("mode_price"))}</td>
            <td class="num-cell">{fmt_clp_price(_st.get("min_price"))}</td>
            <td class="num-cell">{fmt_clp_price(_st.get("mean_price"))}</td>
            <td class="num-cell">{fmt_clp_price(_st.get("mode_price"))}</td>
            <td class="num-cell">{fmt_solotodo_link(_st.get("solotodo_id"))}</td>
            <td>{row.get(COL_CATEGORY, "")}</td>
        </tr>'''

    # Tabla Sin Stock Ingram
    sin_stock_rows = ""
    if not sin_stock_df.empty:
        for i, (_, row) in enumerate(sin_stock_df.iterrows(), 1):
            desc = str(row.get(COL_DESCRIPTION, ""))
            sin_stock_rows += f'''<tr>
            <td>{i}</td>
            <td><code>{row.get(COL_INGRAM_PART, "")}</code></td>
            <td class="desc-cell" title="{desc}">{desc[:35]}{"..." if len(desc) > 35 else ""}</td>
            <td>{row.get(COL_VENDOR_NAME, "")}</td>
            <td><code>{row.get(COL_VENDOR_PART, "")}</code></td>
            <td class="num-cell">{row.get(COL_AVAILABLE_QTY, 0)}</td>
            <td class="num-cell">{fmt_usd(row.get(COL_CUSTOMER_PRICE, 0))}</td>
            <td class="num-cell">{fmt_clp(row.get(COL_CUSTOMER_PRICE, 0))}</td>
            <td>{row.get(COL_CATEGORY, "")}</td>
        </tr>'''

    html = f'''<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="300">
    <title>pc Factory - Monitor Mayorista</title>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🏭</text></svg>">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Ubuntu:wght@400;500;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-primary: #0a0a0f;
            --bg-secondary: #12121a;
            --bg-card: #1a1a24;
            --bg-hover: #22222e;
            --text-primary: #f4f4f5;
            --text-secondary: #a1a1aa;
            --text-muted: #71717a;
            --accent-green: #10b981;
            --accent-yellow: #f59e0b;
            --accent-red: #ef4444;
            --accent-blue: #3b82f6;
            --accent-purple: #8b5cf6;
            --accent-cyan: #06b6d4;
            --border: #27272a;
            --font-mono: 'JetBrains Mono', ui-monospace, monospace;
            --font-sans: 'Ubuntu', -apple-system, BlinkMacSystemFont, sans-serif;
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: var(--font-sans);
            background: var(--bg-primary);
            color: var(--text-primary);
            line-height: 1.6;
            min-height: 100vh;
            padding-bottom: 2rem;
        }}
        .container {{ max-width: 1400px; margin: 0 auto; padding: 2rem; }}
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 2rem;
            padding-bottom: 1.5rem;
            border-bottom: 1px solid var(--border);
            flex-wrap: wrap;
            gap: 1rem;
        }}
        .logo {{ display: flex; align-items: center; gap: 1rem; }}
        .logo-icon {{ width: 48px; height: 48px; flex-shrink: 0; }}
        .logo-icon img {{ width: 100%; height: 100%; object-fit: contain; }}
        .logo-text h1 {{ font-size: 1.5rem; font-weight: 700; letter-spacing: -0.01em; }}
        .logo-text span {{ font-size: 0.875rem; color: var(--text-muted); }}
        .timestamp {{
            font-family: var(--font-mono);
            font-size: 0.875rem;
            color: var(--text-secondary);
            background: var(--bg-card);
            padding: 0.5rem 1rem;
            border-radius: 8px;
            border: 1px solid var(--border);
        }}
        .nav-links {{
            display: flex;
            gap: 0.5rem;
            margin-bottom: 1.5rem;
            flex-wrap: wrap;
            justify-content: flex-start;
        }}
        .nav-link {{
            font-family: var(--font-mono);
            font-size: 0.875rem;
            color: var(--text-secondary);
            text-decoration: none;
            padding: 0.625rem 1rem;
            background: var(--bg-card);
            border-radius: 8px;
            border: 1px solid var(--border);
            transition: all 0.2s;
        }}
        .nav-link:hover {{ background: var(--bg-hover); color: var(--text-primary); }}
        .nav-link.active {{ background: var(--accent-green); color: #000000; border-color: var(--accent-green); font-weight: 500; }}
        .status-banner {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 1.5rem 2rem;
            margin-bottom: 2rem;
            display: flex;
            align-items: center;
            gap: 1rem;
        }}
        .status-banner.healthy {{ border-color: var(--accent-green); background: rgba(16, 185, 129, 0.1); }}
        .status-banner.critical {{ border-color: var(--accent-red); background: rgba(239, 68, 68, 0.1); }}
        .status-banner.warning {{ border-color: var(--accent-yellow); background: rgba(245, 158, 11, 0.1); }}
        .status-indicator {{
            width: 12px;
            height: 12px;
            border-radius: 50%;
            animation: pulse 2s infinite;
        }}
        .status-banner.healthy .status-indicator {{ background: var(--accent-green); }}
        .status-banner.critical .status-indicator {{ background: var(--accent-red); }}
        .status-banner.warning .status-indicator {{ background: var(--accent-yellow); }}
        @keyframes pulse {{
            0%, 100% {{ opacity: 1; }}
            50% {{ opacity: 0.5; }}
        }}
        .status-text {{ font-size: 1.125rem; font-weight: 600; }}
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 1rem;
            margin-bottom: 2rem;
        }}
        .stat-card {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 1.25rem;
            text-align: center;
            transition: all 0.2s ease;
        }}
        .stat-card:hover {{ background: var(--bg-hover); transform: translateY(-2px); }}
        .stat-card.clickable {{ cursor: pointer; }}
        .stat-value {{
            font-family: var(--font-mono);
            font-size: 2.25rem;
            font-weight: 700;
        }}
        .stat-value.green {{ color: var(--accent-green); }}
        .stat-value.dark-green {{ color: #16a34a; }}
        .stat-value.red {{ color: var(--accent-red); }}
        .stat-value.blue {{ color: var(--accent-blue); }}
        .stat-value.yellow {{ color: var(--accent-yellow); }}
        .stat-value.purple {{ color: var(--accent-purple); }}
        .stat-value.cyan {{ color: var(--accent-cyan); }}
        .stat-label {{ color: var(--text-muted); font-size: 0.8rem; margin-top: 0.5rem; }}
        .section-title {{
            font-size: 1.25rem;
            margin-bottom: 1.5rem;
            padding-bottom: 0.75rem;
            border-bottom: 1px solid var(--border);
        }}

        /* Funnel */
        .funnel-section {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 1.5rem;
            margin-bottom: 2rem;
        }}
        .funnel-steps {{
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
            max-width: 800px;
            /*margin: 0 auto;*/
        }}
        .funnel-step {{
            display: flex;
            align-items: center;
            gap: 1rem;
            padding: 0.75rem 1rem;
            border-radius: 8px;
            background: var(--bg-secondary);
        }}
        .funnel-step.funnel-sub {{
            margin-left: 2rem;
            opacity: 0.9;
        }}
        .funnel-step.funnel-sub .funnel-bar {{
            height: 24px;
            font-size: 0.78rem;
        }}
        .funnel-step.funnel-sub .funnel-label {{
            font-size: 0.78rem;
            min-width: 180px;
        }}
        .funnel-bar {{
            height: 32px;
            border-radius: 6px;
            display: flex;
            align-items: center;
            padding: 0 1rem;
            font-family: var(--font-mono);
            font-size: 0.85rem;
            font-weight: 600;
            color: #000;
            min-width: 60px;
            transition: width 0.5s ease;
        }}
        .funnel-label {{
            font-size: 0.85rem;
            color: var(--text-secondary);
            white-space: nowrap;
            min-width: 200px;
        }}
        .funnel-count {{
            font-family: var(--font-mono);
            font-weight: 700;
            min-width: 60px;
            text-align: right;
        }}

        /* Tablas */
        .table-section {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 1.5rem;
            margin-bottom: 2rem;
        }}
        .table-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1rem;
            flex-wrap: wrap;
            gap: 0.75rem;
        }}
        .table-badge {{
            font-family: var(--font-mono);
            font-size: 0.85rem;
            padding: 0.35rem 0.75rem;
            border-radius: 20px;
            font-weight: 600;
        }}
        .badge-green {{ background: rgba(16,185,129,0.15); color: var(--accent-green); }}
        .badge-purple {{ background: rgba(139,92,246,0.15); color: var(--accent-purple); }}
        .badge-yellow {{ background: rgba(245,158,11,0.15); color: var(--accent-yellow); }}
        .badge-red {{ background: rgba(239,68,68,0.15); color: var(--accent-red); }}
        .badge-blue {{ background: rgba(59,130,246,0.15); color: var(--accent-blue); }}
        .badge-cyan {{ background: rgba(6,182,212,0.15); color: var(--accent-cyan); }}
        .search-input {{
            font-family: var(--font-mono);
            font-size: 0.85rem;
            padding: 0.5rem 1rem;
            background: var(--bg-secondary);
            color: var(--text-primary);
            border: 1px solid var(--border);
            border-radius: 8px;
            outline: none;
            min-width: 250px;
        }}
        .search-input:focus {{ border-color: var(--accent-blue); }}
        .table-container {{
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
            margin: 0 -1.5rem;
            padding: 0 1.5rem;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            min-width: 700px;
        }}
        th, td {{
            padding: 0.625rem 0.75rem;
            text-align: left;
            border-bottom: 1px solid var(--border);
            font-size: 0.85rem;
        }}
        th {{
            color: var(--text-muted);
            font-weight: 500;
            font-size: 0.75rem;
            text-transform: uppercase;
            position: sticky;
            top: 0;
            background: var(--bg-card);
            cursor: pointer;
            user-select: none;
        }}
        th:hover {{ color: var(--text-primary); }}
        th.sorted-asc::after {{ content: ' ▲'; font-size: 0.6rem; }}
        th.sorted-desc::after {{ content: ' ▼'; font-size: 0.6rem; }}
        tr:hover {{ background: var(--bg-hover); }}
        td code {{
            font-family: var(--font-mono);
            font-size: 0.8rem;
            background: var(--bg-secondary);
            padding: 0.15rem 0.4rem;
            border-radius: 4px;
        }}
        .desc-cell {{
            max-width: 200px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}
        .num-cell {{
            text-align: right;
            font-family: var(--font-mono);
        }}
        .file-info {{
            font-family: var(--font-mono);
            font-size: 0.8rem;
            color: var(--text-muted);
            margin-bottom: 0.5rem;
        }}
        .tab-section-header {{
            display: flex;
            align-items: center;
            gap: 0.75rem;
            margin-bottom: 0.75rem;
            padding-bottom: 0.5rem;
            border-bottom: 1px solid var(--border);
        }}
        .tab-section-title {{
            font-size: 0.7rem;
            font-weight: 700;
            letter-spacing: 0.1em;
            text-transform: uppercase;
            color: var(--text-muted);
        }}
        .tab-container {{
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem;
            margin-bottom: 1rem;
        }}
        .tab-btn {{
            font-family: var(--font-mono);
            font-size: 0.85rem;
            padding: 0.5rem 1rem;
            background: var(--bg-secondary);
            color: var(--text-secondary);
            border: 1px solid var(--border);
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.2s;
        }}
        .tab-btn:hover {{ background: var(--bg-hover); color: var(--text-primary); }}
        .tab-btn.active {{ background: var(--accent-blue); color: #000; border-color: var(--accent-blue); font-weight: 500; }}
        .tab-content {{ display: none; }}
        .tab-content.active {{ display: block; }}
        .tab-group-label {{
            font-size: 0.65rem;
            color: var(--text-muted);
            padding: 0 0.5rem;
            align-self: center;
            white-space: nowrap;
        }}
        .source-badge {{
            display: inline-flex;
            align-items: center;
            padding: 0.3rem 0.75rem;
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 6px;
            font-size: 0.75rem;
            font-weight: 600;
            color: var(--text-secondary);
            letter-spacing: 0.05em;
            text-transform: uppercase;
            cursor: default;
            align-self: center;
        }}
        .footer {{
            text-align: center;
            padding: 2rem;
            color: var(--text-muted);
            font-size: 0.875rem;
        }}
        /* Glosario */
        .glosario-section {{
            margin-top: 3rem;
            padding: 2rem;
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 12px;
        }}
        .glosario-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
            gap: 1rem;
            margin-top: 0.5rem;
        }}
        .glosario-card {{
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 1rem 1.25rem;
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
        }}
        .glosario-header {{
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}
        .glosario-icon {{
            font-size: 1.2rem;
            flex-shrink: 0;
        }}
        .glosario-title {{
            font-weight: 600;
            font-size: 0.95rem;
            color: var(--text-primary);
        }}
        .glosario-desc {{
            font-size: 0.82rem;
            color: var(--text-muted);
            line-height: 1.5;
            margin: 0;
        }}
        .glosario-criteria {{
            display: flex;
            flex-wrap: wrap;
            gap: 0.35rem;
            margin-top: 0.25rem;
        }}
        .criteria-tag {{
            font-size: 0.72rem;
            padding: 0.2rem 0.55rem;
            border-radius: 20px;
            font-family: 'Courier New', monospace;
            font-weight: 500;
        }}
        .tag-green  {{ background: rgba(0,200,120,0.15); color: var(--accent-green); border: 1px solid rgba(0,200,120,0.3); }}
        .tag-blue   {{ background: rgba(59,130,246,0.15); color: var(--accent-blue);  border: 1px solid rgba(59,130,246,0.3); }}
        .tag-orange {{ background: rgba(251,146,60,0.15); color: #fb923c;             border: 1px solid rgba(251,146,60,0.3); }}
        .tag-purple {{ background: rgba(167,139,250,0.15); color: var(--accent-purple); border: 1px solid rgba(167,139,250,0.3); }}
        .tag-neutral {{ background: rgba(148,163,184,0.15); color: var(--text-secondary); border: 1px solid rgba(148,163,184,0.3); }}
        .download-btn {{
            display: inline-flex;
            align-items: center;
            gap: 0.4rem;
            padding: 0.45rem 1rem;
            background: rgba(16,185,129,0.12);
            color: var(--accent-green);
            border: 1px solid rgba(16,185,129,0.35);
            border-radius: 6px;
            font-size: 0.82rem;
            font-family: var(--font-sans);
            font-weight: 500;
            text-decoration: none;
            cursor: pointer;
            transition: background 0.15s;
        }}
        .download-btn:hover {{ background: rgba(16,185,129,0.22); }}
        @media (max-width: 768px) {{
            .container {{ padding: 1rem; }}
            .header {{ flex-direction: column; text-align: center; }}
            .stats-grid {{ grid-template-columns: repeat(2, 1fr); }}
            .stat-value {{ font-size: 1.5rem; }}
            .funnel-label {{ min-width: 120px; font-size: 0.75rem; }}
            .search-input {{ min-width: 100%; }}
            .glosario-grid {{ grid-template-columns: 1fr; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <header class="header">
            <div class="logo">
                <div class="logo-icon">
                    <img src="https://assets-v3.pcfactory.cl/uploads/e964d6b9-e816-439f-8b97-ad2149772b7b/original/pcfactory-isotipo.svg" alt="pc Factory">
                </div>
                <div class="logo-text">
                    <h1>pc Factory Monitor</h1>
                    <span>Mayorista - Ingram Micro</span>
                </div>
            </div>
            <div style="display: flex; align-items: center; gap: 1rem; flex-wrap: wrap;">
                <div class="timestamp">{timestamp_display}</div>
                <a href="mayorista-report.xlsx" target="_blank" class="download-btn">⬇ Descargar Excel</a>
                <a href="mayorista-categories.csv" target="_blank" class="download-btn" style="background:#fef3c7;color:#92400e;">📊 CSV IMPORTDATA</a>
            </div>
        </header>

        <nav class="nav-links">
            <!-- TODO: habilitar cuando esten listos los otros monitores
            <a href="index.html" class="nav-link">📦 Categorias</a>
            <a href="delivery.html" class="nav-link">🚚 Despacho Nacional</a>
            <a href="checkout.html" class="nav-link">🛒 Checkout</a>
            <a href="payments.html" class="nav-link">💳 Medios de Pago</a>
            <a href="login.html" class="nav-link">🔐 Login</a>
            <a href="banners.html" class="nav-link">🎨 Banners</a>
            <a href="pagespeed.html" class="nav-link">⚡ PageSpeed</a>
            -->
            <a href="mayorista.html" class="nav-link active">🏭 Ingram</a>
        </nav>

        <div class="file-info">📄 Archivo: <span id="file-url">{price_file_name_display}</span></div>
        {usd_info_html}

        <div class="status-banner {status_class}">
            <div class="status-indicator"></div>
            <span class="status-text">{status_text}</span>
        </div>

        <div class="stats-grid">
            <div class="stat-card clickable" onclick="switchTab('total')">
                <div class="stat-value blue">{total}</div>
                <div class="stat-label">Total Productos</div>
            </div>
            <div class="stat-card clickable" onclick="switchTab('constock')">
                <div class="stat-value cyan">{with_stock}</div>
                <div class="stat-label">Con Stock Ingram</div>
            </div>
            <div class="stat-card clickable" onclick="switchTab('pcfstock')">
                <div class="stat-value red">{len(has_pcf_stock)}</div>
                <div class="stat-label">Con Stock PCF (de Ingram)</div>
            </div>
            <div class="stat-card clickable" onclick="switchTab('noelegible')">
                <div class="stat-value red">{no_eligible}</div>
                <div class="stat-label">No Elegibles (Open/Bad Box / Caja / Refurbished)</div>
            </div>
            <div class="stat-card clickable" onclick="switchTab('mayorista')">
                <div class="stat-value dark-green">{len(already_mayorista)}</div>
                <div class="stat-label">Publicados</div>
            </div>
            <div class="stat-card clickable" onclick="switchTab('potenciales')">
                <div class="stat-value green">{total_potencial}</div>
                <div class="stat-label">Potenciales</div>
            </div>
            <div class="stat-card clickable" onclick="switchTab('publish')">
                <div class="stat-value green">{len(publish_ready)}</div>
                <div class="stat-label">Con Ficha Listos para Publicar</div>
            </div>
            <div class="stat-card clickable" onclick="switchTab('pending')">
                <div class="stat-value cyan">{len(pending_ficha)}</div>
                <div class="stat-label">Ficha Solicitada (esperando eCommerce)</div>
            </div>
            <div class="stat-card clickable" onclick="switchTab('fichaok')">
                <div class="stat-value blue">{len(ficha_ok)}</div>
                <div class="stat-label">Ficha necesita revisión</div>
            </div>
            <div class="stat-card clickable" onclick="switchTab('ficha')">
                <div class="stat-value yellow">{len(missing_ficha)}</div>
                <div class="stat-label">Sin Ficha Solicitada</div>
            </div>
            <div class="stat-card clickable" onclick="switchTab('creation')">
                <div class="stat-value purple">{len(need_creation)}</div>
                <div class="stat-label">ID No Existe y Requieren Creacion</div>
            </div>
        </div>

        <!-- Funnel de elegibilidad -->
        <div class="funnel-section">
            <h2 class="section-title" style="border-bottom: none; margin-bottom: 1rem;">Funnel de Elegibilidad de productos</h2>
            <div class="funnel-steps">
                <div class="funnel-step clickable" onclick="switchTab('total')" style="cursor:pointer;">
                    <span class="funnel-label">Total en Price File</span>
                    <div class="funnel-bar" style="width: 100%; background: var(--accent-blue);">{total}</div>
                </div>
                <div class="funnel-step clickable" onclick="switchTab('constock')" style="cursor:pointer;">
                    <span class="funnel-label">Con Stock Ingram</span>
                    <div class="funnel-bar" style="width: {max(with_stock / total * 100, 5) if total > 0 else 5}%; background: var(--accent-cyan);">{with_stock}</div>
                </div>
                <div class="funnel-step clickable" onclick="switchTab('mayorista')" style="cursor:pointer;">
                    <span class="funnel-label">├── Publicados</span>
                    <div class="funnel-bar" style="width: {max(len(already_mayorista) / total * 100, 5) if total > 0 else 5}%; background: #16a34a;">{len(already_mayorista)}</div>
                </div>
                <div class="funnel-step clickable" onclick="switchTab('potenciales')" style="cursor:pointer;">
                    <span class="funnel-label">├── Potenciales (total)</span>
                    <div class="funnel-bar" style="width: {max(after_api_filters / total * 100, 5) if total > 0 else 5}%; background: var(--accent-green);">{after_api_filters}</div>
                </div>
                <div class="funnel-step funnel-sub clickable" onclick="switchTab('publish')" style="cursor:pointer;">
                    <span class="funnel-label">│ ├── Con Ficha Listos</span>
                    <div class="funnel-bar" style="width: {max(len(publish_ready) / total * 100, 2) if total > 0 else 2}%; background: #86efac; color: #000;">{len(publish_ready)}</div>
                </div>
                <div class="funnel-step funnel-sub clickable" onclick="switchTab('pending')" style="cursor:pointer;">
                    <span class="funnel-label">│ ├── Ficha Solicitada</span>
                    <div class="funnel-bar" style="width: {max(len(pending_ficha) / total * 100, 2) if total > 0 else 2}%; background: var(--accent-cyan); color: #000;">{len(pending_ficha)}</div>
                </div>
                <div class="funnel-step funnel-sub clickable" onclick="switchTab('fichaok')" style="cursor:pointer;">
                    <span class="funnel-label">│ ├── Ficha necesita revisión</span>
                    <div class="funnel-bar" style="width: {max(len(ficha_ok) / total * 100, 2) if total > 0 else 2}%; background: var(--accent-blue); color: #fff;">{len(ficha_ok)}</div>
                </div>
                <div class="funnel-step funnel-sub clickable" onclick="switchTab('ficha')" style="cursor:pointer;">
                    <span class="funnel-label">│ ├── Sin Ficha Solicitada</span>
                    <div class="funnel-bar" style="width: {max(len(missing_ficha) / total * 100, 2) if total > 0 else 2}%; background: var(--accent-yellow); color: #000;">{len(missing_ficha)}</div>
                </div>
                <div class="funnel-step funnel-sub clickable" onclick="switchTab('creation')" style="cursor:pointer;">
                    <span class="funnel-label">│ └── Requieren Creación</span>
                    <div class="funnel-bar" style="width: {max(len(need_creation) / total * 100, 2) if total > 0 else 2}%; background: var(--accent-purple); color: #fff;">{len(need_creation)}</div>
                </div>
                <div class="funnel-step clickable" onclick="switchTab('pcfstock')" style="cursor:pointer;">
                    <span class="funnel-label">├── Con Stock PCF</span>
                    <div class="funnel-bar" style="width: {max(len(has_pcf_stock) / total * 100, 5) if total > 0 else 5}%; background: #fb923c; color: #000;">{len(has_pcf_stock)}</div>
                </div>
                <div class="funnel-step clickable" onclick="switchTab('noelegible')" style="cursor:pointer;">
                    <span class="funnel-label">└── No Elegibles</span>
                    <div class="funnel-bar" style="width: {max(no_eligible / total * 100, 5) if total > 0 else 5}%; background: var(--accent-red);">{no_eligible}</div>
                </div>
            </div>
        </div>

        <!-- Tabs para las tablas -->
        <div class="tab-section-header">
            <span class="source-badge">📦 Ingram</span>
            <span class="tab-section-title">Selecciona una categoría</span>
        </div>
        <div class="tab-container">
            <button class="tab-btn" onclick="switchTab('potenciales')">🎯 Potenciales ({total_potencial})</button>

            <button class="tab-btn active" onclick="switchTab('publish')">↳ ✅ Con Ficha Listos para Publicar ({len(publish_ready)})</button>
            <button class="tab-btn" onclick="switchTab('pending')">↳ ⏳ Ficha Solicitada ({len(pending_ficha)})</button>
            <button class="tab-btn" onclick="switchTab('fichaok')">↳ ✅ Ficha necesita revisión ({len(ficha_ok)})</button>
            <button class="tab-btn" onclick="switchTab('ficha')">↳ 📝 Sin Ficha Solicitada ({len(missing_ficha)})</button>
            <button class="tab-btn" onclick="switchTab('creation')">↳ 🆕 ID No Existe y Requieren Creacion ({len(need_creation)})</button>
            <button class="tab-btn" onclick="switchTab('mayorista')">🏭 Publicados ({len(already_mayorista)})</button>
            <button class="tab-btn" onclick="switchTab('pcfstock')">📦 Con Stock PCF ({len(has_pcf_stock)})</button>
            <button class="tab-btn" onclick="switchTab('noelegible')">🚫 No Elegibles ({no_eligible})</button>
            <button class="tab-btn" onclick="switchTab('constock')">📊 Con Stock Ingram ({with_stock})</button>
            <button class="tab-btn" onclick="switchTab('sinstock')">🚫 Sin Stock ({sin_stock})</button>
            <button class="tab-btn" onclick="switchTab('total')">📋 Total ({total})</button>
        </div>

        <!-- Tabla: Potenciales -->
        <div id="tab-potenciales" class="tab-content">
            <div class="table-section">
                <div class="table-header">
                    <div>
                        <h2 class="section-title" style="border-bottom: none; margin-bottom: 0.25rem; font-size: 1.1rem;">Todos los Potenciales</h2>
                        <span class="table-badge badge-green">{total_potencial} productos potenciales</span>
                    </div>
                    <input type="text" class="search-input" placeholder="🔍 Buscar..." oninput="filterTable('table-potenciales', this.value)">
                </div>
                <div class="table-container">
                    <table id="table-potenciales">
                        <thead>
                            <tr>
                                <th onclick="sortTable('table-potenciales', 0, 'num')">#</th>
                                <th onclick="sortTable('table-potenciales', 1, 'num')">PCF ID</th>
                                <th onclick="sortTable('table-potenciales', 2, 'str')">Descripcion</th>
                                <th onclick="sortTable('table-potenciales', 3, 'str')">Vendor</th>
                                <th onclick="sortTable('table-potenciales', 4, 'str')">Part Number</th>
                                <th onclick="sortTable('table-potenciales', 5, 'num')">Stock Ingram</th>
                                <th onclick="sortTable('table-potenciales', 6, 'num')">Costo USD</th>
                                <th onclick="sortTable('table-potenciales', 7, 'num')">Costo CLP</th>
                                <th onclick="sortTable('table-potenciales', 8, 'num')">PCF SoloTodo</th>
                                <th onclick="sortTable('table-potenciales', 9, 'num')">Min. Mercado</th>
                                <th onclick="sortTable('table-potenciales', 10, 'num')">Media Mercado</th>
                                <th onclick="sortTable('table-potenciales', 11, 'num')">Moda Mercado</th>
                                <th>SoloTodo</th>
                                <th onclick="sortTable('table-potenciales', 13, 'str')">Solicitud Ficha</th>
                                <th onclick="sortTable('table-potenciales', 14, 'str')">Estado</th>
                            </tr>
                        </thead>
                        <tbody>
                            {potenciales_rows if potenciales_rows else '<tr><td colspan="15" style="text-align: center; color: var(--text-muted); padding: 2rem;">Sin productos potenciales</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Tabla: Con Ficha Listos para Publicar -->
        <div id="tab-publish" class="tab-content active">
            <div class="table-section">
                <div class="table-header">
                    <div>
                        <h2 class="section-title" style="border-bottom: none; margin-bottom: 0.25rem; font-size: 1.1rem;">Con Ficha Listos para Publicar</h2>
                        <span class="table-badge badge-green">{len(publish_ready)} productos listos</span>
                    </div>
                    <input type="text" class="search-input" placeholder="🔍 Buscar por nombre, vendor, part number..." oninput="filterTable('table-publish', this.value)">
                </div>
                <div class="table-container">
                    <table id="table-publish">
                        <thead>
                            <tr>
                                <th onclick="sortTable('table-publish', 0, 'num')">#</th>
                                <th onclick="sortTable('table-publish', 1, 'num')">PCF ID</th>
                                <th onclick="sortTable('table-publish', 2, 'str')">Descripcion</th>
                                <th onclick="sortTable('table-publish', 3, 'str')">Vendor</th>
                                <th onclick="sortTable('table-publish', 4, 'str')">Part Number</th>
                                <th onclick="sortTable('table-publish', 5, 'num')">Stock Ingram</th>
                                <th onclick="sortTable('table-publish', 6, 'num')">Costo USD</th>
                                <th onclick="sortTable('table-publish', 7, 'num')">Costo CLP</th>
                                <th onclick="sortTable('table-publish', 8, 'num')">PCF SoloTodo</th>
                                <th onclick="sortTable('table-publish', 9, 'num')">Min. Mercado</th>
                                <th onclick="sortTable('table-publish', 10, 'num')">Media Mercado</th>
                                <th onclick="sortTable('table-publish', 11, 'num')">Moda Mercado</th>
                                <th>SoloTodo</th>
                                <th onclick="sortTable('table-publish', 13, 'str')">Solicitud Ficha</th>
                                <th onclick="sortTable('table-publish', 14, 'str')">Categoria</th>
                            </tr>
                        </thead>
                        <tbody>
                            {publish_rows if publish_rows else '<tr><td colspan="15" style="text-align: center; color: var(--text-muted); padding: 2rem;">Sin productos en esta categoria</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Tabla: Ficha Solicitada - Esperando que eCommerce publique -->
        <div id="tab-pending" class="tab-content">
            <div class="table-section">
                <div class="table-header">
                    <div>
                        <h2 class="section-title" style="border-bottom: none; margin-bottom: 0.25rem; font-size: 1.1rem;">Ficha Solicitada — Esperando publicación en eCommerce</h2>
                        <span class="table-badge badge-cyan">{len(pending_ficha)} productos en espera</span>
                    </div>
                    <input type="text" class="search-input" placeholder="🔍 Buscar por nombre, vendor, part number..." oninput="filterTable('table-pending', this.value)">
                </div>
                <div class="table-container">
                    <table id="table-pending">
                        <thead>
                            <tr>
                                <th onclick="sortTable('table-pending', 0, 'num')">#</th>
                                <th onclick="sortTable('table-pending', 1, 'num')">PCF ID</th>
                                <th onclick="sortTable('table-pending', 2, 'str')">Descripcion</th>
                                <th onclick="sortTable('table-pending', 3, 'str')">Vendor</th>
                                <th onclick="sortTable('table-pending', 4, 'str')">Part Number</th>
                                <th onclick="sortTable('table-pending', 5, 'num')">Stock Ingram</th>
                                <th onclick="sortTable('table-pending', 6, 'num')">Costo USD</th>
                                <th onclick="sortTable('table-pending', 7, 'num')">Costo CLP</th>
                                <th onclick="sortTable('table-pending', 8, 'num')">PCF SoloTodo</th>
                                <th onclick="sortTable('table-pending', 9, 'num')">Min. Mercado</th>
                                <th onclick="sortTable('table-pending', 10, 'num')">Media Mercado</th>
                                <th onclick="sortTable('table-pending', 11, 'num')">Moda Mercado</th>
                                <th>SoloTodo</th>
                                <th onclick="sortTable('table-pending', 13, 'str')">Solicitud Ficha</th>
                                <th onclick="sortTable('table-pending', 14, 'str')">Categoria</th>
                            </tr>
                        </thead>
                        <tbody>
                            {pending_rows if pending_rows else '<tr><td colspan="15" style="text-align: center; color: var(--text-muted); padding: 2rem;">Sin productos en esta categoria</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Tabla: Ficha necesita revisión -->
        <div id="tab-fichaok" class="tab-content">
            <div class="table-section">
                <div class="table-header">
                    <div>
                        <h2 class="section-title" style="border-bottom: none; margin-bottom: 0.25rem; font-size: 1.1rem;">Ficha necesita revisión</h2>
                        <span class="table-badge badge-blue">{len(ficha_ok)} productos</span>
                    </div>
                    <input type="text" class="search-input" placeholder="🔍 Buscar por nombre, vendor, part number..." oninput="filterTable('table-fichaok', this.value)">
                </div>
                <div class="table-container">
                    <table id="table-fichaok">
                        <thead>
                            <tr>
                                <th onclick="sortTable('table-fichaok', 0, 'num')">#</th>
                                <th onclick="sortTable('table-fichaok', 1, 'num')">PCF ID</th>
                                <th onclick="sortTable('table-fichaok', 2, 'str')">Descripcion</th>
                                <th onclick="sortTable('table-fichaok', 3, 'str')">Vendor</th>
                                <th onclick="sortTable('table-fichaok', 4, 'str')">Part Number</th>
                                <th onclick="sortTable('table-fichaok', 5, 'num')">Stock Ingram</th>
                                <th onclick="sortTable('table-fichaok', 6, 'num')">Costo USD</th>
                                <th onclick="sortTable('table-fichaok', 7, 'num')">Costo CLP</th>
                                <th onclick="sortTable('table-fichaok', 8, 'num')">PCF SoloTodo</th>
                                <th onclick="sortTable('table-fichaok', 9, 'num')">Min. Mercado</th>
                                <th onclick="sortTable('table-fichaok', 10, 'num')">Media Mercado</th>
                                <th onclick="sortTable('table-fichaok', 11, 'num')">Moda Mercado</th>
                                <th>SoloTodo</th>
                                <th onclick="sortTable('table-fichaok', 13, 'str')">Solicitud Ficha</th>
                                <th onclick="sortTable('table-fichaok', 14, 'str')">Categoria</th>
                            </tr>
                        </thead>
                        <tbody>
                            {fichaok_rows if fichaok_rows else '<tr><td colspan="15" style="text-align: center; color: var(--text-muted); padding: 2rem;">Sin productos en esta categoria</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Tabla: Producto sin ficha solicitada -->
        <div id="tab-ficha" class="tab-content">
            <div class="table-section">
                <div class="table-header">
                    <div>
                        <h2 class="section-title" style="border-bottom: none; margin-bottom: 0.25rem; font-size: 1.1rem;">Sin Ficha Solicitada</h2>
                        <span class="table-badge badge-yellow">{len(missing_ficha)} productos necesitan ficha</span>
                    </div>
                    <input type="text" class="search-input" placeholder="🔍 Buscar por nombre, vendor, part number..." oninput="filterTable('table-ficha', this.value)">
                </div>
                <div class="table-container">
                    <table id="table-ficha">
                        <thead>
                            <tr>
                                <th onclick="sortTable('table-ficha', 0, 'num')">#</th>
                                <th onclick="sortTable('table-ficha', 1, 'num')">PCF ID</th>
                                <th onclick="sortTable('table-ficha', 2, 'str')">Descripcion</th>
                                <th onclick="sortTable('table-ficha', 3, 'str')">Vendor</th>
                                <th onclick="sortTable('table-ficha', 4, 'str')">Part Number</th>
                                <th onclick="sortTable('table-ficha', 5, 'num')">Stock Ingram</th>
                                <th onclick="sortTable('table-ficha', 6, 'num')">Costo USD</th>
                                <th onclick="sortTable('table-ficha', 7, 'num')">Costo CLP</th>
                                <th onclick="sortTable('table-ficha', 8, 'num')">PCF SoloTodo</th>
                                <th onclick="sortTable('table-ficha', 9, 'num')">Min. Mercado</th>
                                <th onclick="sortTable('table-ficha', 10, 'num')">Media Mercado</th>
                                <th onclick="sortTable('table-ficha', 11, 'num')">Moda Mercado</th>
                                <th>SoloTodo</th>
                                <th onclick="sortTable('table-ficha', 13, 'str')">Solicitud Ficha</th>
                                <th onclick="sortTable('table-ficha', 14, 'str')">Categoria</th>
                            </tr>
                        </thead>
                        <tbody>
                            {ficha_rows if ficha_rows else '<tr><td colspan="15" style="text-align: center; color: var(--text-muted); padding: 2rem;">Sin productos en esta categoria</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Tabla: Requieren Creacion -->
        <div id="tab-creation" class="tab-content">
            <div class="table-section">
                <div class="table-header">
                    <div>
                        <h2 class="section-title" style="border-bottom: none; margin-bottom: 0.25rem; font-size: 1.1rem;">ID No Existe y Requieren Creacion</h2>
                        <span class="table-badge badge-purple">{len(need_creation)} productos no encontrados</span>
                    </div>
                    <input type="text" class="search-input" placeholder="🔍 Buscar por nombre, vendor, part number..." oninput="filterTable('table-creation', this.value)">
                </div>
                <div class="table-container">
                    <table id="table-creation">
                        <thead>
                            <tr>
                                <th onclick="sortTable('table-creation', 0, 'num')">#</th>
                                <th onclick="sortTable('table-creation', 1, 'str')">Ingram Part</th>
                                <th onclick="sortTable('table-creation', 2, 'str')">Descripcion</th>
                                <th onclick="sortTable('table-creation', 3, 'str')">Vendor</th>
                                <th onclick="sortTable('table-creation', 4, 'str')">Part Number</th>
                                <th onclick="sortTable('table-creation', 5, 'num')">Stock Ingram</th>
                                <th onclick="sortTable('table-creation', 6, 'num')">Costo USD</th>
                                <th onclick="sortTable('table-creation', 7, 'num')">Costo CLP</th>
                                <th onclick="sortTable('table-creation', 8, 'str')">Solicitud Ficha</th>
                                <th onclick="sortTable('table-creation', 9, 'str')">Categoria</th>
                            </tr>
                        </thead>
                        <tbody>
                            {creation_rows if creation_rows else '<tr><td colspan="10" style="text-align: center; color: var(--text-muted); padding: 2rem;">Sin productos en esta categoria</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Tabla: Publicados -->
        <div id="tab-mayorista" class="tab-content">
            <div class="table-section">
                <div class="table-header">
                    <div>
                        <h2 class="section-title" style="border-bottom: none; margin-bottom: 0.25rem; font-size: 1.1rem;">Publicados</h2>
                        <span class="table-badge badge-green">{len(already_mayorista)} productos publicados</span>
                    </div>
                    <input type="text" class="search-input" placeholder="🔍 Buscar..." oninput="filterTable('table-mayorista', this.value)">
                </div>
                <div class="table-container">
                    <table id="table-mayorista">
                        <thead>
                            <tr>
                                <th onclick="sortTable('table-mayorista', 0, 'num')">#</th>
                                <th onclick="sortTable('table-mayorista', 1, 'num')">PCF ID</th>
                                <th onclick="sortTable('table-mayorista', 2, 'str')">Descripcion</th>
                                <th onclick="sortTable('table-mayorista', 3, 'str')">Vendor</th>
                                <th onclick="sortTable('table-mayorista', 4, 'num')">Stock Ingram</th>
                                <th onclick="sortTable('table-mayorista', 5, 'num')">Stock PCF</th>
                                <th onclick="sortTable('table-mayorista', 6, 'num')">Costo USD</th>
                                <th onclick="sortTable('table-mayorista', 7, 'num')">Costo CLP</th>
                                <th onclick="sortTable('table-mayorista', 8, 'num')">PCF SoloTodo</th>
                                <th onclick="sortTable('table-mayorista', 9, 'num')">Min. Mercado</th>
                                <th onclick="sortTable('table-mayorista', 10, 'num')">Media Mercado</th>
                                <th onclick="sortTable('table-mayorista', 11, 'num')">Moda Mercado</th>
                                <th>SoloTodo</th>
                            </tr>
                        </thead>
                        <tbody>
                            {mayorista_rows if mayorista_rows else '<tr><td colspan="13" style="text-align: center; color: var(--text-muted); padding: 2rem;">Sin productos en esta categoria</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Tabla: Con Stock PCF -->
        <div id="tab-pcfstock" class="tab-content">
            <div class="table-section">
                <div class="table-header">
                    <div>
                        <h2 class="section-title" style="border-bottom: none; margin-bottom: 0.25rem; font-size: 1.1rem;">Excluidos: Con Stock en PCFactory (tienen stock Ingram)</h2>
                        <span class="table-badge badge-red">{len(has_pcf_stock)} productos excluidos</span>
                    </div>
                    <input type="text" class="search-input" placeholder="🔍 Buscar..." oninput="filterTable('table-pcfstock', this.value)">
                </div>
                <div class="table-container">
                    <table id="table-pcfstock">
                        <thead>
                            <tr>
                                <th onclick="sortTable('table-pcfstock', 0, 'num')">#</th>
                                <th onclick="sortTable('table-pcfstock', 1, 'num')">PCF ID</th>
                                <th onclick="sortTable('table-pcfstock', 2, 'str')">Descripcion</th>
                                <th onclick="sortTable('table-pcfstock', 3, 'str')">Vendor</th>
                                <th onclick="sortTable('table-pcfstock', 4, 'num')">Stock Ingram</th>
                                <th onclick="sortTable('table-pcfstock', 5, 'num')">Stock PCF</th>
                                <th onclick="sortTable('table-pcfstock', 6, 'num')">Costo USD</th>
                                <th onclick="sortTable('table-pcfstock', 7, 'num')">Costo CLP</th>
                                <th onclick="sortTable('table-pcfstock', 8, 'str')">Ubicación Stock</th>
                            </tr>
                        </thead>
                        <tbody>
                            {pcf_stock_rows if pcf_stock_rows else '<tr><td colspan="9" style="text-align: center; color: var(--text-muted); padding: 2rem;">Sin productos en esta categoria</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Tabla: No Elegibles (BAD BOX / OPEN BOX / CAJA / REF) -->
        <div id="tab-noelegible" class="tab-content">
            <div class="table-section">
                <div class="table-header">
                    <div>
                        <h2 class="section-title" style="border-bottom: none; margin-bottom: 0.25rem; font-size: 1.1rem;">Productos No Elegibles</h2>
                        <span class="table-badge badge-red">{no_eligible} productos BAD BOX / OPEN BOX / CAJA DAÑADA / CAJA ABIERTA / CAJA DETERIORADA / REF / REFURBISHED</span>
                    </div>
                    <input type="text" class="search-input" placeholder="🔍 Buscar..." oninput="filterTable('table-noelegible', this.value)">
                </div>
                <div class="table-container">
                    <table id="table-noelegible">
                        <thead>
                            <tr>
                                <th onclick="sortTable('table-noelegible', 0, 'num')">#</th>
                                <th onclick="sortTable('table-noelegible', 1, 'str')">SKU Ingram</th>
                                <th onclick="sortTable('table-noelegible', 2, 'str')">Nombre</th>
                                <th onclick="sortTable('table-noelegible', 3, 'str')">Marca</th>
                                <th onclick="sortTable('table-noelegible', 4, 'str')">Part Number</th>
                                <th onclick="sortTable('table-noelegible', 5, 'num')">Stock Ingram</th>
                                <th onclick="sortTable('table-noelegible', 6, 'num')">Costo USD</th>
                                <th onclick="sortTable('table-noelegible', 7, 'num')">Costo CLP</th>
                                <th onclick="sortTable('table-noelegible', 8, 'str')">Categoria</th>
                            </tr>
                        </thead>
                        <tbody>
                            {no_eligible_rows if no_eligible_rows else '<tr><td colspan="9" style="text-align: center; color: var(--text-muted); padding: 2rem;">Sin productos en esta categoria</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Tabla: Con Stock Ingram -->
        <div id="tab-constock" class="tab-content">
            <div class="table-section">
                <div class="table-header">
                    <div>
                        <h2 class="section-title" style="border-bottom: none; margin-bottom: 0.25rem; font-size: 1.1rem;">Con Stock en Ingram</h2>
                        <span class="table-badge badge-blue">{with_stock} productos con stock</span>
                    </div>
                    <input type="text" class="search-input" placeholder="🔍 Buscar..." oninput="filterTable('table-constock', this.value)">
                </div>
                <div class="table-container">
                    <table id="table-constock">
                        <thead>
                            <tr>
                                <th onclick="sortTable('table-constock', 0, 'num')">#</th>
                                <th onclick="sortTable('table-constock', 1, 'str')">Ingram Part</th>
                                <th onclick="sortTable('table-constock', 2, 'str')">Descripcion</th>
                                <th onclick="sortTable('table-constock', 3, 'str')">Vendor</th>
                                <th onclick="sortTable('table-constock', 4, 'str')">Part Number</th>
                                <th onclick="sortTable('table-constock', 5, 'num')">Stock</th>
                                <th onclick="sortTable('table-constock', 6, 'num')">Costo USD</th>
                                <th onclick="sortTable('table-constock', 7, 'num')">Costo CLP</th>
                                <th onclick="sortTable('table-constock', 8, 'str')">Categoria</th>
                            </tr>
                        </thead>
                        <tbody>'''

    # Generar filas Con Stock Ingram
    constock_rows = ""
    if not has_stock_df.empty:
        for i, (_, row) in enumerate(has_stock_df.iterrows(), 1):
            desc = str(row.get(COL_DESCRIPTION, ""))
            constock_rows += f'''<tr>
            <td>{i}</td>
            <td><code>{row.get(COL_INGRAM_PART, "")}</code></td>
            <td class="desc-cell" title="{desc}">{desc[:35]}{"..." if len(desc) > 35 else ""}</td>
            <td>{row.get(COL_VENDOR_NAME, "")}</td>
            <td><code>{row.get(COL_VENDOR_PART, "")}</code></td>
            <td class="num-cell">{row.get(COL_AVAILABLE_QTY, 0)}</td>
            <td class="num-cell">{fmt_usd(row.get(COL_CUSTOMER_PRICE, 0))}</td>
            <td class="num-cell">{fmt_clp(row.get(COL_CUSTOMER_PRICE, 0))}</td>
            <td>{row.get(COL_CATEGORY, "")}</td>
        </tr>'''

    html += f'''{constock_rows if constock_rows else '<tr><td colspan="9" style="text-align: center; color: var(--text-muted); padding: 2rem;">Sin productos</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Tabla: Sin Stock Ingram -->
        <div id="tab-sinstock" class="tab-content">
            <div class="table-section">
                <div class="table-header">
                    <div>
                        <h2 class="section-title" style="border-bottom: none; margin-bottom: 0.25rem; font-size: 1.1rem;">Sin Stock en Ingram</h2>
                        <span class="table-badge badge-red">{sin_stock} productos sin stock</span>
                    </div>
                    <input type="text" class="search-input" placeholder="🔍 Buscar..." oninput="filterTable('table-sinstock', this.value)">
                </div>
                <div class="table-container">
                    <table id="table-sinstock">
                        <thead>
                            <tr>
                                <th onclick="sortTable('table-sinstock', 0, 'num')">#</th>
                                <th onclick="sortTable('table-sinstock', 1, 'str')">Ingram Part</th>
                                <th onclick="sortTable('table-sinstock', 2, 'str')">Descripcion</th>
                                <th onclick="sortTable('table-sinstock', 3, 'str')">Vendor</th>
                                <th onclick="sortTable('table-sinstock', 4, 'str')">Part Number</th>
                                <th onclick="sortTable('table-sinstock', 5, 'num')">Stock</th>
                                <th onclick="sortTable('table-sinstock', 6, 'num')">Costo USD</th>
                                <th onclick="sortTable('table-sinstock', 7, 'num')">Costo CLP</th>
                                <th onclick="sortTable('table-sinstock', 8, 'str')">Categoria</th>
                            </tr>
                        </thead>
                        <tbody>
                            {sin_stock_rows if sin_stock_rows else '<tr><td colspan="9" style="text-align: center; color: var(--text-muted); padding: 2rem;">Sin productos</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Tabla: Total Productos -->
        <div id="tab-total" class="tab-content">
            <div class="table-section">
                <div class="table-header">
                    <div>
                        <h2 class="section-title" style="border-bottom: none; margin-bottom: 0.25rem; font-size: 1.1rem;">Total Productos en Price File</h2>
                        <span class="table-badge badge-blue">{total} productos</span>
                    </div>
                    <input type="text" class="search-input" placeholder="🔍 Buscar..." oninput="filterTable('table-total', this.value)">
                </div>
                <div class="table-container">
                    <table id="table-total">
                        <thead>
                            <tr>
                                <th onclick="sortTable('table-total', 0, 'num')">#</th>
                                <th onclick="sortTable('table-total', 1, 'str')">Ingram Part</th>
                                <th onclick="sortTable('table-total', 2, 'str')">Descripcion</th>
                                <th onclick="sortTable('table-total', 3, 'str')">Vendor</th>
                                <th onclick="sortTable('table-total', 4, 'str')">Part Number</th>
                                <th onclick="sortTable('table-total', 5, 'num')">Stock</th>
                                <th onclick="sortTable('table-total', 6, 'num')">Costo USD</th>
                                <th onclick="sortTable('table-total', 7, 'num')">Costo CLP</th>
                                <th onclick="sortTable('table-total', 8, 'num')">PCF SoloTodo</th>
                                <th onclick="sortTable('table-total', 9, 'num')">Min. Mercado</th>
                                <th onclick="sortTable('table-total', 10, 'num')">Media Mercado</th>
                                <th onclick="sortTable('table-total', 11, 'num')">Moda Mercado</th>
                                <th>SoloTodo</th>
                                <th onclick="sortTable('table-total', 13, 'str')">Categoria</th>
                            </tr>
                        </thead>
                        <tbody>
                            {total_rows if total_rows else '<tr><td colspan="14" style="text-align: center; color: var(--text-muted); padding: 2rem;">Sin productos</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- GLOSARIO DE CLASIFICACIONES -->
        <div class="glosario-section">
            <h2 class="section-title" style="margin-bottom: 1.5rem;">📖 Glosario de Clasificaciones</h2>
            <div class="glosario-grid">

                <div class="glosario-card">
                    <div class="glosario-header">
                        <span class="glosario-icon">📋</span>
                        <span class="glosario-title">Total Productos</span>
                    </div>
                    <p class="glosario-desc">Todos los productos presentes en el price file de Ingram Micro, sin ningún filtro aplicado. Es el universo completo de referencia.</p>
                    <div class="glosario-criteria">
                        <span class="criteria-tag tag-neutral">Price file completo</span>
                    </div>
                </div>

                <div class="glosario-card">
                    <div class="glosario-header">
                        <span class="glosario-icon">📊</span>
                        <span class="glosario-title">Con Stock Ingram</span>
                    </div>
                    <p class="glosario-desc">Productos que Ingram tiene disponibles para despachar hoy. Son el subconjunto relevante para trabajar.</p>
                    <div class="glosario-criteria">
                        <span class="criteria-tag tag-blue">Available Quantity &gt; 0</span>
                    </div>
                </div>

                <div class="glosario-card">
                    <div class="glosario-header">
                        <span class="glosario-icon">🏭</span>
                        <span class="glosario-title">Publicados</span>
                    </div>
                    <p class="glosario-desc">Productos que ya están activos en pc Factory como mayoristas en Lista 1. No requieren ninguna acción — ya están funcionando en la web.</p>
                    <div class="glosario-criteria">
                        <span class="criteria-tag tag-purple">mayorista: true</span>
                        <span class="criteria-tag tag-purple">lista: "1"</span>
                    </div>
                </div>

                <div class="glosario-card">
                    <div class="glosario-header">
                        <span class="glosario-icon">📦</span>
                        <span class="glosario-title">Con Stock PCF</span>
                    </div>
                    <p class="glosario-desc">Productos que PCFactory ya tiene en stock propio. Se excluyen porque PCFactory los puede vender directamente, sin necesidad de habilitarlos como mayorista.</p>
                    <div class="glosario-criteria">
                        <span class="criteria-tag tag-orange">stock.aproximado &gt; 0 en PCF</span>
                    </div>
                </div>

                <div class="glosario-card">
                    <div class="glosario-header">
                        <span class="glosario-icon">🚫</span>
                        <span class="glosario-title">No Elegibles</span>
                    </div>
                    <p class="glosario-desc">Productos que no son elegibles para el canal mayorista porque corresponden a unidades dañadas o reacondicionadas. Se identifican por el texto en el nombre del producto.</p>
                    <div class="glosario-criteria">
                        <span class="criteria-tag tag-red">NOMBRE contiene "BAD BOX"</span>
                        <span class="criteria-tag tag-red">NOMBRE contiene "OPEN BOX"</span>
                        <span class="criteria-tag tag-red">NOMBRE contiene "CAJA DAÑADA"</span>
                        <span class="criteria-tag tag-red">NOMBRE contiene "CAJA ABIERTA"</span>
                        <span class="criteria-tag tag-red">NOMBRE contiene "CAJA DETERIORADA"</span>
                        <span class="criteria-tag tag-red">NOMBRE contiene "REF"</span>
                        <span class="criteria-tag tag-red">NOMBRE contiene "REFURBISHED"</span>
                    </div>
                </div>

                <div class="glosario-card">
                    <div class="glosario-header">
                        <span class="glosario-icon">🎯</span>
                        <span class="glosario-title">Potenciales</span>
                    </div>
                    <p class="glosario-desc">Total de productos que podrían publicarse o ya están en proceso. Es la suma de Con Ficha Listos + ID Existente Sin Ficha + ID No Existe y Requieren Creación. Si tienen PCF ID, deben tener <code>lista: "0"</code> y sin stock en PCFactory.</p>
                    <div class="glosario-criteria">
                        <span class="criteria-tag tag-green">Con Stock Ingram</span>
                        <span class="criteria-tag tag-green">lista: "0" (no publicados)</span>
                        <span class="criteria-tag tag-green">Sin stock PCF</span>
                    </div>
                </div>

                <div class="glosario-card">
                    <div class="glosario-header">
                        <span class="glosario-icon">✅</span>
                        <span class="glosario-title">Con Ficha Listos para Publicar</span>
                    </div>
                    <p class="glosario-desc">Productos listos para activarse en Lista 1 de forma inmediata. Tienen PCF ID, ficha completa, y no tienen stock propio en PCFactory.</p>
                    <div class="glosario-criteria">
                        <span class="criteria-tag tag-green">PCF ID en price file</span>
                        <span class="criteria-tag tag-green">mayorista: false</span>
                        <span class="criteria-tag tag-green">stock PCF = 0</span>
                        <span class="criteria-tag tag-green">Ficha con contenido</span>
                    </div>
                </div>

                <div class="glosario-card">
                    <div class="glosario-header">
                        <span class="glosario-icon">⏳</span>
                        <span class="glosario-title">Ficha Solicitada</span>
                    </div>
                    <p class="glosario-desc">Productos cuya ficha ya fue solicitada al equipo de eCommerce y está pendiente de publicación. El estado proviene del sheet de seguimiento (Pendiente, Ficha Básica, Ficha Antigua).</p>
                    <div class="glosario-criteria">
                        <span class="criteria-tag tag-blue">PCF ID en price file</span>
                        <span class="criteria-tag tag-orange">Ficha vacía en API</span>
                        <span class="criteria-tag tag-cyan">Con registro en seguimiento</span>
                    </div>
                </div>

                <div class="glosario-card">
                    <div class="glosario-header">
                        <span class="glosario-icon">✅</span>
                        <span class="glosario-title">Ficha necesita revisión</span>
                    </div>
                    <p class="glosario-desc">Seguimiento marca la ficha como OK pero la API del eCommerce aún la muestra vacía. Posible retraso de caché o indexación. Verificar manualmente si el producto ya puede publicarse.</p>
                    <div class="glosario-criteria">
                        <span class="criteria-tag tag-blue">PCF ID en price file</span>
                        <span class="criteria-tag tag-orange">Ficha vacía en API</span>
                        <span class="criteria-tag tag-green">Seguimiento: OK</span>
                    </div>
                </div>

                <div class="glosario-card">
                    <div class="glosario-header">
                        <span class="glosario-icon">📝</span>
                        <span class="glosario-title">ID Existente Sin Ficha Solicitada</span>
                    </div>
                    <p class="glosario-desc">Productos potenciales cuya ficha está vacía o sin contenido real en el eCommerce. No se pueden publicar hasta que el equipo de contenido complete la descripción.</p>
                    <div class="glosario-criteria">
                        <span class="criteria-tag tag-blue">PCF ID en price file</span>
                        <span class="criteria-tag tag-blue">mayorista: false</span>
                        <span class="criteria-tag tag-blue">stock PCF = 0</span>
                        <span class="criteria-tag tag-orange">Ficha vacía o incompleta</span>
                    </div>
                </div>

                <div class="glosario-card">
                    <div class="glosario-header">
                        <span class="glosario-icon">🆕</span>
                        <span class="glosario-title">ID No Existe y Requieren Creación</span>
                    </div>
                    <p class="glosario-desc">Productos cuyo ID no existe aún en PCFactory o que no tienen PCF ID asignado en el price file. Requieren creación del producto en el sistema (proceso de x–x días hábiles).</p>
                    <div class="glosario-criteria">
                        <span class="criteria-tag tag-orange">API retorna 404</span>
                        <span class="criteria-tag tag-orange">o sin PCF ID en price file</span>
                    </div>
                </div>

            </div>
        </div>

        <footer class="footer">
            <p>Monitor Mayorista - Ingram Micro | Datos actualizados periodicamente</p>
            <p>Hecho con ❤️ por Ain Cortes Catoni</p>
        </footer>
    </div>

    <script>
        // Auto-refresh
        setTimeout(() => location.reload(), 5 * 60 * 1000);

        // Tab switching
        function switchTab(name) {{
            document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
            const tabEl = document.getElementById('tab-' + name);
            if (tabEl) tabEl.classList.add('active');
            // Activar el boton del tab correspondiente
            document.querySelectorAll('.tab-btn').forEach(btn => {{
                if (btn.getAttribute('onclick') && btn.getAttribute('onclick').includes("'" + name + "'")) {{
                    btn.classList.add('active');
                }}
            }});
            // Scroll al area de tabs
            const tabContainer = document.querySelector('.tab-container');
            if (tabContainer) tabContainer.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
        }}

        // Table filtering
        function filterTable(tableId, query) {{
            const table = document.getElementById(tableId);
            const rows = table.querySelectorAll('tbody tr');
            const q = query.toLowerCase();
            rows.forEach(row => {{
                const text = row.textContent.toLowerCase();
                row.style.display = text.includes(q) ? '' : 'none';
            }});
        }}

        // Table sorting
        let sortState = {{}};
        function sortTable(tableId, colIdx, type) {{
            const table = document.getElementById(tableId);
            const tbody = table.querySelector('tbody');
            const rows = Array.from(tbody.querySelectorAll('tr'));
            const key = tableId + '-' + colIdx;
            const asc = sortState[key] !== 'asc';
            sortState[key] = asc ? 'asc' : 'desc';

            // Update header classes
            table.querySelectorAll('th').forEach(th => th.classList.remove('sorted-asc', 'sorted-desc'));
            table.querySelectorAll('th')[colIdx].classList.add(asc ? 'sorted-asc' : 'sorted-desc');

            rows.sort((a, b) => {{
                let va = a.cells[colIdx]?.textContent.trim() || '';
                let vb = b.cells[colIdx]?.textContent.trim() || '';
                if (type === 'num') {{
                    va = parseFloat(va.replace(/[^0-9.-]/g, '')) || 0;
                    vb = parseFloat(vb.replace(/[^0-9.-]/g, '')) || 0;
                    return asc ? va - vb : vb - va;
                }}
                return asc ? va.localeCompare(vb) : vb.localeCompare(va);
            }});
            rows.forEach(row => tbody.appendChild(row));
        }}
    </script>
</body>
</html>'''

    return html

# ==============================================================================
# MAIN
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="PCFactory Mayorista Monitor - Ingram Micro")
    parser.add_argument("--source", type=str, default="gsheet",
                       choices=["gsheet", "local"],
                       help="Fuente de datos: 'gsheet' (Google Sheets) o 'local' (archivo XLSX)")
    parser.add_argument("--sheet-id", type=str, default=GOOGLE_SHEET_ID,
                       help="ID del Google Sheet (solo para --source gsheet)")
    parser.add_argument("--gid", type=str, default=GOOGLE_SHEET_GID,
                       help="ID de la hoja dentro del Google Sheet")
    parser.add_argument("--mayorista-dir", type=str, default=MAYORISTA_DIR,
                       help="Directorio con los price files (solo para --source local)")
    parser.add_argument("--output-dir", type=str, default="./output",
                       help="Directorio de salida")
    parser.add_argument("--workers", type=int, default=5,
                       help="Workers para consultas API")
    parser.add_argument("--skip-api", action="store_true",
                       help="Saltar consultas a la API (solo filtros XLSX)")
    parser.add_argument("--no-solotodo", action="store_true",
                       help="Omitir consulta de precios en SoloTodo")
    parser.add_argument("--pcf-catalog", type=str, default=None,
                       help="Ruta al XLS del catálogo interno PCFactory para cruce de IDs faltantes")
    parser.add_argument("--ingram-file", type=str, default=None,
                       help="Ruta directa al XLSX de Ingram (alternativa a --source local con patrón fijo)")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("PCFactory Mayorista Monitor - Ingram Micro")
    print("=" * 60)

    # 1. Obtener datos segun la fuente
    price_file_url = None
    if args.ingram_file:
        print(f"[*] Fuente: Archivo directo ({args.ingram_file})")
        price_file_name = Path(args.ingram_file).name
        df = read_price_file(args.ingram_file)
    elif args.source == "gsheet":
        print(f"[*] Fuente: Google Sheets (ID: {args.sheet_id})")
        try:
            df = read_google_sheet(args.sheet_id, args.gid)
            price_file_name = f"Google Sheet ({args.sheet_id[:8]}...)"
            price_file_url = f"https://docs.google.com/spreadsheets/d/{args.sheet_id}/export?format=xlsx"
        except Exception:
            print(f"[!] No se pudo leer el Google Sheet")
            empty_stats = {"total": 0, "sin_stock_ingram": 0}
            empty_class = {"publish_ready": [], "missing_ficha": [], "need_creation": [], "already_mayorista": [], "has_pcf_stock": [], "api_errors": []}
            ts = datetime.now(timezone.utc).isoformat()
            html = generate_html_dashboard(empty_stats, empty_class, "Error al leer Google Sheet", ts)
            html_path = output_dir / "mayorista.html"
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"[+] Dashboard vacio guardado: {html_path}")
            return
    else:
        print(f"[*] Fuente: Archivo local ({args.mayorista_dir}/)")
        price_file = find_latest_price_file(args.mayorista_dir)
        if not price_file:
            print(f"[!] No se encontro ningun price file en {args.mayorista_dir}/")
            empty_stats = {"total": 0, "sin_stock_ingram": 0}
            empty_class = {"publish_ready": [], "missing_ficha": [], "need_creation": [], "already_mayorista": [], "has_pcf_stock": [], "api_errors": []}
            ts = datetime.now(timezone.utc).isoformat()
            html = generate_html_dashboard(empty_stats, empty_class, "No encontrado", ts)
            html_path = output_dir / "mayorista.html"
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"[+] Dashboard vacio guardado: {html_path}")
            return
        price_file_name = Path(price_file).name
        print(f"[+] Price file: {price_file_name}")
        df = read_price_file(price_file)

    # 3. Filtros XLSX
    print("\n[*] Aplicando filtros XLSX...")
    xlsx_stats = apply_xlsx_filters(df)
    print(f"    Total: {xlsx_stats['total']}")
    print(f"    Sin stock Ingram: {xlsx_stats['sin_stock_ingram']}")
    print(f"    No elegibles (BAD/OPEN BOX/CAJA DAÑADA/CAJA ABIERTA/CAJA DETERIORADA/REF/REFURBISHED): {xlsx_stats['no_eligible']}")
    print(f"    Con PCF ID: {len(xlsx_stats['has_pcf_id'])}")
    print(f"    Sin PCF ID: {len(xlsx_stats['no_pcf_id'])}")

    # 3b. Enriquecer con catálogo PCF (local vía --pcf-catalog o desde Google Sheets)
    if len(xlsx_stats["no_pcf_id"]) > 0:
        if args.pcf_catalog:
            print(f"\n[*] Cruzando con catálogo PCF (local): {args.pcf_catalog}")
            catalog_df = load_pcf_catalog(args.pcf_catalog)
        else:
            catalog_df = read_pcf_catalog_sheet()
        new_matched, still_no_pcf = enrich_with_pcf_catalog(xlsx_stats["no_pcf_id"], catalog_df)
        xlsx_stats["has_pcf_id"] = pd.concat([xlsx_stats["has_pcf_id"], new_matched], ignore_index=True)
        xlsx_stats["no_pcf_id"] = still_no_pcf
        print(f"    Con PCF ID (actualizado): {len(xlsx_stats['has_pcf_id'])}")
        print(f"    Sin PCF ID (actualizado): {len(xlsx_stats['no_pcf_id'])}")

    # 4. Cargar seguimiento (necesario antes de clasificar para separar pending_ficha)
    seguimiento = read_seguimiento_sheet()

    # 5. Consultar API
    classification = {
        "publish_ready": [],
        "pending_ficha": [],
        "missing_ficha": [],
        "need_creation": [],
        "already_mayorista": [],
        "has_pcf_stock": [],
        "api_errors": [],
    }

    if not args.skip_api and len(xlsx_stats["has_pcf_id"]) > 0:
        session = create_session()
        api_results = check_products_batch(session, xlsx_stats["has_pcf_id"], args.workers)
        classification = classify_products(api_results, xlsx_stats["no_pcf_id"], seguimiento)
    else:
        if args.skip_api:
            print("\n[*] API omitida (--skip-api)")
        # Sin API, todos los que tienen PCF ID van a publish_ready (sin validar)
        for _, row in xlsx_stats["has_pcf_id"].iterrows():
            classification["publish_ready"].append({
                "pcf_id": int(float(row[COL_PCF_ID])) if pd.notna(row[COL_PCF_ID]) else None,
                "ingram_part": row.get(COL_INGRAM_PART, ""),
                "description": row.get(COL_DESCRIPTION, ""),
                "vendor_name": row.get(COL_VENDOR_NAME, ""),
                "vendor_part": row.get(COL_VENDOR_PART, ""),
                "customer_price": row.get(COL_CUSTOMER_PRICE, 0),
                "available_qty": row.get(COL_AVAILABLE_QTY, 0),
                "category": row.get(COL_CATEGORY, ""),
                "subcategory": row.get(COL_SUBCATEGORY, ""),
            })
        for _, row in xlsx_stats["no_pcf_id"].iterrows():
            classification["need_creation"].append({
                "pcf_id": None,
                "ingram_part": row.get(COL_INGRAM_PART, ""),
                "description": row.get(COL_DESCRIPTION, ""),
                "vendor_name": row.get(COL_VENDOR_NAME, ""),
                "vendor_part": row.get(COL_VENDOR_PART, ""),
                "customer_price": row.get(COL_CUSTOMER_PRICE, 0),
                "available_qty": row.get(COL_AVAILABLE_QTY, 0),
                "category": row.get(COL_CATEGORY, ""),
                "subcategory": row.get(COL_SUBCATEGORY, ""),
            })

    # 6. Resumen
    print(f"\n{'=' * 60}")
    print("RESULTADOS")
    print(f"{'=' * 60}")
    print(f"  Publicacion inmediata: {len(classification['publish_ready'])}")
    print(f"  Ficha solicitada (esperando eCommerce): {len(classification.get('pending_ficha', []))}")
    print(f"  Ficha necesita revisión:          {len(classification.get('ficha_ok', []))}")
    print(f"  Sin ficha (no solicitada): {len(classification.get('missing_ficha', []))}")
    print(f"  Requieren creacion:    {len(classification['need_creation'])}")
    print(f"  Ya mayorista:          {len(classification['already_mayorista'])}")
    print(f"  Con stock PCF:         {len(classification['has_pcf_stock'])}")
    print(f"  Errores API:           {len(classification['api_errors'])}")
    total_potencial = (len(classification['publish_ready']) + len(classification.get('pending_ficha', []))
                       + len(classification.get('ficha_ok', [])) + len(classification.get('missing_ficha', []))
                       + len(classification['need_creation']))
    print(f"  TOTAL POTENCIALES:     {total_potencial}")

    # 6b. Enriquecer con SoloTodo (por defecto, omitir con --no-solotodo)
    if not args.no_solotodo and not args.skip_api:
        st_session = create_session()
        actionable = (classification["publish_ready"] + classification.get("pending_ficha", [])
                      + classification.get("ficha_ok", []) + classification.get("missing_ficha", [])
                      + classification["already_mayorista"] + classification["need_creation"]
                      + classification["has_pcf_stock"])
        enrich_with_solotodo(actionable, st_session, max_workers=4)
    elif args.skip_api:
        print("\n[!] SoloTodo omitido porque --skip-api esta activo")

    # 6. Generar dashboard HTML
    timestamp = datetime.now(timezone.utc).isoformat()
    print("\n[*] Obteniendo tipo de cambio USD...")
    usd_clp = fetch_usd_clp()
    if usd_clp:
        print(f"[+] USD observado: ${usd_clp:,.0f} CLP")
    else:
        print("[!] No se pudo obtener el tipo de cambio, columna CLP mostrara '—'")
    html = generate_html_dashboard(xlsx_stats, classification, price_file_name, timestamp, df_original=df, usd_clp=usd_clp, seguimiento=seguimiento, price_file_url=price_file_url)

    # 8. Guardar CSV para IMPORTDATA (Google Sheets)
    csv_path = output_dir / "mayorista-categories.csv"
    all_products = []
    for cat, prods in classification.items():
        if isinstance(prods, list):
            for p in prods:
                all_products.append({**p, "categoria": cat})
    if all_products:
        import csv
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            fieldnames = list(all_products[0].keys())
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_products)
        print(f"[+] CSV para IMPORTDATA guardado: {csv_path}")

    # 9. Guardar JSON report
    report = {
        "timestamp": timestamp,
        "price_file": price_file_name,
        "summary": {
            "total": xlsx_stats["total"],
            "con_stock_ingram": xlsx_stats["total"] - xlsx_stats["sin_stock_ingram"],
            "publish_ready": len(classification["publish_ready"]),
            "missing_ficha": len(classification.get("missing_ficha", [])),
            "need_creation": len(classification["need_creation"]),
            "already_mayorista": len(classification["already_mayorista"]),
            "has_pcf_stock": len(classification["has_pcf_stock"]),
            "api_errors": len(classification["api_errors"]),
            "total_potencial": total_potencial,
        },
        "publish_ready": classification["publish_ready"],
        "missing_ficha": classification.get("missing_ficha", []),
        "need_creation": classification["need_creation"],
        "already_mayorista": classification["already_mayorista"],
        "has_pcf_stock": classification["has_pcf_stock"],
        "api_errors": classification["api_errors"],
    }
    json_path = output_dir / "mayorista-report.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"[+] JSON guardado: {json_path}")

    print("\n[OK] Monitor Mayorista completado!")

if __name__ == "__main__":
    main()
