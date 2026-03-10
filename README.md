# PCFactory Monitor Mayorista — Ingram Micro

Monitor automático que lee el price file de Ingram Micro, cruza con la API de productos de PCFactory, y genera un dashboard HTML con el estado de cada producto.

---

## Fuente de datos

El monitor lee el price file directamente desde Google Sheets. El sheet debe estar compartido como **"Cualquier persona con el enlace puede ver"**.

```bash
python mayorista_monitor.py              # Lee desde Google Sheets (default)
python mayorista_monitor.py --skip-api   # Solo filtros, sin consultar API
python mayorista_monitor.py --source local  # Lee desde archivo XLSX local (mayorista/)
```

---

## Cards del Dashboard

### 📋 Total Productos
Todos los productos presentes en el price file de Ingram, sin ningún filtro aplicado.

---

### 📊 Con Stock Ingram
Productos donde `Available Quantity > 0` en el price file.

> **Filtro aplicado:** `Available Quantity > 0`

---

### 🏭 Publicados (Lista 1)
Productos que ya están activos en PCFactory como productos mayoristas (Lista 1). La API de PCFactory retorna tanto `mayorista: true` como `lista: "1"` para estos productos.

> **Criterio:** PCF ID existe en el price file + API retorna `mayorista: true` **y** `lista: "1"`
>
> Un producto con `mayorista: true` pero `lista: "0"` **no** se cuenta como publicado — queda en el grupo potencial que corresponda.
>
> Estos productos ya están funcionando en la web. No requieren acción.

---

### 🎯 Potenciales
Total de productos que podrían publicarse o ya están en proceso. Es la suma de los tres grupos siguientes:

```
Potenciales = Con Ficha Listos + ID Existente Sin Ficha + ID No Existe y Requieren Creación
```

> **Criterio:** Tienen stock en Ingram + no son CLEARANCE + si tienen PCF ID deben tener `lista: "0"` (no publicados) + no tienen stock propio en PCFactory

---

### ✅ Con Ficha Listos para Publicar
Productos que cumplen todos los requisitos para publicarse de forma **inmediata** en Lista 1.

> **Criterios:**
> - Tiene stock en Ingram (`Available Quantity > 0`)
> - No es CLEARANCE
> - Tiene PCF ID asignado en el price file
> - La API retorna `mayorista: false` (no está aún publicado)
> - La API retorna `stock: 0` en PCFactory (no tiene stock propio)
> - Tiene ficha completa (`descripcion` con contenido real en la API)

---

### 📝 ID Existente Sin Ficha Solicitada
Productos potenciales que tienen PCF ID pero cuya ficha está vacía o sin contenido relevante en PCFactory. No se pueden publicar hasta que se complete la descripción del producto.

> **Criterios:**
> - Tiene stock en Ingram
> - No es CLEARANCE
> - Tiene PCF ID asignado
> - La API retorna `mayorista: false`
> - La API retorna `stock: 0` en PCFactory
> - La `descripcion` en la API está vacía o tiene menos de 20 caracteres de texto real (solo HTML sin contenido)

---

### 🆕 ID No Existe y Requieren Creación
Productos con stock en Ingram cuyo ID **no existe** en PCFactory o que directamente no tienen PCF ID asignado en el price file. Requieren creación del producto en el sistema (proceso de x–x días hábiles).

> **Criterios (cualquiera de estos dos casos):**
> - Tiene PCF ID en el price file pero la API retorna `404 Not Found`
> - No tiene PCF ID asignado en el price file (campo vacío, "Sin ID", etc.)

---

### 📦 Con Stock PCF
Productos que tienen stock propio en PCFactory. Se excluyen del monitoreo porque PCFactory ya los puede vender con stock propio, sin necesidad de habilitarlos como mayorista.

> **Criterio:** API retorna `stock.aproximado > 0`

---

### ⚠️ CLEARANCE
Productos marcados como liquidación en el price file de Ingram. Se excluyen del monitoreo porque son productos en proceso de descontinuación.

> **Criterio:** Campo `Creation Reason Value` contiene la palabra `CLEARANCE`

---

## Funnel de Elegibilidad

El funnel muestra el recorrido de los productos desde el total hasta los potenciales. Cada paso es clickeable y lleva a la lista correspondiente.

```
Total Productos
    └─ Con Stock Ingram          (filtro: Available Quantity > 0)
         └─ Sin CLEARANCE         (filtro: Creation Reason ≠ CLEARANCE)
              ├─ Publicados       (info: ya activos en Lista 1)
              └─ Potenciales      (filtro: lista "0" + sin stock PCF)
```

---

## Ejecución automática (GitHub Actions)

El workflow corre automáticamente dos veces al día: **10:00 y 16:00 hora Chile** (UTC-3).

También puede ejecutarse manualmente desde GitHub → Actions → **Mayorista Monitor** → Run workflow.

El resultado se publica automáticamente en GitHub Pages.

---

## Desarrollo local

```bash
# Crear entorno virtual
python3 -m venv venv
source venv/bin/activate

# Instalar dependencias
pip install -r requirements.txt

# Ejecutar (lee desde Google Sheets, consulta API)
python3 mayorista_monitor.py

# Ejecutar sin consultar la API (más rápido, solo filtros del price file)
python3 mayorista_monitor.py --skip-api
```

El dashboard se genera en `output/mayorista.html`.
