# Scripts para publicar a PyPI

Este directorio contiene scripts para automatizar la publicación del paquete a PyPI.

## Opciones disponibles

### 1. Script Python (recomendado - multiplataforma)

```bash
python scripts/publish.py [OPTIONS]
```

#### Opciones:
- `--test` - Publica en TestPyPI (por defecto es aquí)
- `--username USER` - Email/usuario de PyPI
- `--password PASS` - Contraseña o token
- `--skip-checks` - Salta verificación del paquete

#### Ejemplos:

**Publicar en TestPyPI (con credenciales):**
```bash
python scripts/publish.py --test --username tu@email.com --password tu_password
```

**Publicar en PyPI oficial (con credenciales):**
```bash
python scripts/publish.py --username tu@email.com --password tu_password
```

**Usar variables de entorno (más seguro):**
```bash
export TWINE_USERNAME="tu@email.com"
export TWINE_PASSWORD="tu_password"
python scripts/publish.py  # Publica en TestPyPI
```

---

### 2. Script Bash (Linux/Mac)

```bash
./scripts/publish-to-pypi.sh [ENVIRONMENT] [USERNAME] [PASSWORD]
```

#### Argumentos:
- `ENVIRONMENT` - `test` (por defecto) o `prod`
- `USERNAME` - Email de PyPI
- `PASSWORD` - Contraseña o token

#### Ejemplos:

**Publicar en TestPyPI interactivo (te pide contraseña):**
```bash
./scripts/publish-to-pypi.sh test
```

**Con credenciales:**
```bash
./scripts/publish-to-pypi.sh test tu@email.com tu_password
```

**Publicar en PyPI oficial:**
```bash
./scripts/publish-to-pypi.sh prod tu@email.com tu_password
```

---

## Instalación de dependencias

Antes de usar los scripts, instala las herramientas necesarias:

```bash
pip install build twine
```

## Flujo de publicación

1. **Limpiar builds anteriores**
2. **Construir el paquete** (`python -m build`)
3. **Verificar integridad** (`twine check`)
4. **Subir a PyPI** (`twine upload`)

## Opciones de credenciales (en orden de preferencia)

### 1. Variables de entorno (más seguro)
```bash
export TWINE_USERNAME="tu@email.com"
export TWINE_PASSWORD="tu_contraseña"
python scripts/publish.py
```

### 2. Archivo `~/.pypirc`
Crea `~/.pypirc`:
```ini
[distutils]
index-servers = pypi

[pypi]
repository = https://upload.pypi.org/legacy/
username = tu@email.com
password = tu_contraseña
```

### 3. Argumentos en el comando
```bash
python scripts/publish.py --username tu@email.com --password tu_password
```

### 4. Interactivo
El script te pedirá las credenciales si no las proporcionas:
```bash
python scripts/publish.py
# Username: tu@email.com
# Password: ****
```

## TestPyPI vs PyPI oficial

### TestPyPI (para pruebas)
- URL: https://test.pypi.org
- Propósito: Probar antes de publicar en producción
- Sin riesgo de afectar usuarios reales

### PyPI oficial
- URL: https://pypi.org
- Propósito: Distribución de producción
- Los usuarios pueden instalar tu paquete con `pip install`

## Verificar publicación

### En TestPyPI:
```bash
pip install -i https://test.pypi.org/simple/ fast-langchain-server
```

### En PyPI:
```bash
pip install fast-langchain-server
```

## Troubleshooting

### Error: "No se encuentra pyproject.toml"
- Asegúrate de ejecutar los scripts desde la raíz del proyecto

### Error: "twine no está instalado"
```bash
pip install twine
```

### Error: "Credenciales inválidas"
- Verifica tu email y contraseña en PyPI
- Si usas token, debe empezar con `pypi-`

### El paquete ya existe en la versión
- Incrementa la versión en `pyproject.toml`
- Ejemplo: `0.1.0` → `0.1.1`

## Automatizar con CI/CD

Para GitHub Actions, agrega a `.github/workflows/publish.yml`:

```yaml
name: Publish to PyPI

on:
  release:
    types: [created]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - run: pip install build twine
      - run: python -m build
      - run: twine upload dist/*
        env:
          TWINE_USERNAME: __token__
          TWINE_PASSWORD: ${{ secrets.PYPI_TOKEN }}
```

## Preguntas frecuentes

**P: ¿Necesito crear un token especial?**
R: No es obligatorio. Puedes usar tu contraseña, pero un token es más seguro.

**P: ¿Qué es TestPyPI?**
R: Un servidor de práctica idéntico a PyPI donde puedes probar sin afectar usuarios reales.

**P: ¿Puedo actualizar un paquete ya publicado?**
R: Sí, pero debes incrementar la versión en `pyproject.toml`.

**P: ¿Cómo se instala después de publicar?**
R: Los usuarios usan `pip install fast-langchain-server`
