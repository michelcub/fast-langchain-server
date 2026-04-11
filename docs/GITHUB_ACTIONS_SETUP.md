# Configurar GitHub Actions para publicar a PyPI

Esta guía explica cómo configurar GitHub Actions para publicar tu paquete a PyPI de forma segura.

## 1. Crear tokens en PyPI

### Para TestPyPI (desarrollo/pruebas)

1. Ve a https://test.pypi.org/account/login/
2. Inicia sesión con tu cuenta
3. Ve a **Account settings** → **API tokens**
4. Click en **"Add API token"**
5. Configura:
   - **Token name:** `github-actions-test`
   - **Scope:** Selecciona tu proyecto o "Entire account"
6. **Copia el token** (empieza con `pypi-`)

### Para PyPI oficial (producción)

1. Ve a https://pypi.org/account/login/
2. Inicia sesión
3. Ve a **Account settings** → **API tokens**
4. Click en **"Add API token"**
5. Configura:
   - **Token name:** `github-actions-prod`
   - **Scope:** Selecciona tu proyecto o "Entire account"
6. **Copia el token** (empieza con `pypi-`)

## 2. Agregar tokens a GitHub Secrets

### En tu repositorio en GitHub:

1. Ve a **Settings** → **Secrets and variables** → **Actions**
2. Click en **"New repository secret"**

**Crea dos secrets:**

| Nombre | Valor | Descripción |
|--------|-------|-------------|
| `TEST_PYPI_API_TOKEN` | `pypi-AgEI...` | Token de TestPyPI |
| `PYPI_API_TOKEN` | `pypi-AgEI...` | Token de PyPI oficial |

---

## 3. Cómo usar el workflow

### Opción A: Publicar en TestPyPI (recomendado para probar)

**Desde GitHub UI:**
1. Ve a **Actions** → **Publish to PyPI**
2. Click en **"Run workflow"**
3. Selecciona `testpypi` en el dropdown
4. Click en **"Run workflow"**

**Desde CLI (gh):**
```bash
gh workflow run publish-pypi.yml -f environment=testpypi
```

### Opción B: Publicar automáticamente en PyPI oficial

El workflow se ejecuta automáticamente cuando:

1. **Creas un release en GitHub**
   - Si la release está en estado "draft": **publica en TestPyPI**
   - Si la release se "publica": **publica en PyPI oficial**

**Pasos:**
1. Ve a **Releases** → **Draft a new release**
2. Click en **"Create a new tag"** (ej: `v0.1.0`)
3. Completa los detalles
4. Click en **"Save draft"** → Publica en TestPyPI
5. O click en **"Publish release"** → Publica en PyPI oficial

---

## 4. Flujos de trabajo disponibles

### Flujo de desarrollo (TestPyPI)

```
Crear release draft
    ↓
GitHub Actions (TestPyPI)
    ↓
Verificar en https://test.pypi.org/project/fast-langchain-server/
    ↓
Probar: pip install -i https://test.pypi.org/simple/ fast-langchain-server
    ↓
Si todo OK → Publicar release
```

### Flujo de producción (PyPI oficial)

```
Publicar release en GitHub
    ↓
GitHub Actions (PyPI)
    ↓
Paquete disponible en PyPI
    ↓
pip install fast-langchain-server
```

---

## 5. Monitorear la publicación

### En GitHub:
1. Ve a **Actions** → **Publish to PyPI**
2. Verás los workflows ejecutados
3. Click en un workflow para ver detalles

### Verificar en PyPI:

**TestPyPI:**
```bash
# Instalar desde TestPyPI
pip install -i https://test.pypi.org/simple/ fast-langchain-server

# O visitar: https://test.pypi.org/project/fast-langchain-server/
```

**PyPI oficial:**
```bash
# Instalar desde PyPI
pip install fast-langchain-server

# O visitar: https://pypi.org/project/fast-langchain-server/
```

---

## 6. Solucionar problemas

### Error: "Secret not found"
- Verifica que el nombre del secret es exacto: `TEST_PYPI_API_TOKEN` o `PYPI_API_TOKEN`
- Asegúrate de estar en la rama `develop` o `main` donde configuraste el secret

### Error: "Invalid token"
- Verifica que el token empieza con `pypi-`
- Asegúrate de que es el token correcto (TestPyPI vs PyPI)

### El workflow no se ejecuta
- Asegúrate de que `.github/workflows/publish-pypi.yml` existe en tu rama
- GitHub Actions debe estar habilitado en **Settings** → **Actions**

### Package already exists
- Debes incrementar la versión en `pyproject.toml`
- Ejemplo: `0.1.0` → `0.1.1` → `0.2.0`

---

## 7. Mejores prácticas

✅ **Haz:**
- Usa secrets para credenciales (nunca tokens en el código)
- Siempre prueba en TestPyPI primero
- Incrementa versiones semánticamente (MAJOR.MINOR.PATCH)
- Crea un release por cada versión publicada

❌ **No hagas:**
- No publiques el token en commits o PRs
- No uses la misma versión dos veces
- No publiques directamente en PyPI sin probar

---

## 8. Configuración avanzada

### Publicar en múltiples eventos

Puedes modificar el workflow para publicar en otros eventos:

```yaml
on:
  push:
    tags:
      - 'v*'  # Publica cuando haces push de un tag como v1.0.0
  workflow_dispatch:  # También permítelo manual
```

### Usar tokens específicos por repositorio

Si tienes múltiples repositorios, crea tokens separados:
- `token-langchain-server`
- `token-another-project`

Usa scopes específicos en PyPI para mayor seguridad.

---

## Próximos pasos

1. ✅ Crea los tokens en PyPI
2. ✅ Agrega los secrets a GitHub
3. ✅ Prueba publicando en TestPyPI
4. ✅ Verifica que la instalación funciona
5. ✅ Cuando esté listo, publica en PyPI oficial

¿Necesitas ayuda con algún paso?
