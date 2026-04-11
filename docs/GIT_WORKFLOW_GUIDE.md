# Protocolo de Workflow en Git

Guía completa sobre cómo trabajar con ramas, hacer commits, crear PRs y cómo funcionan las GitHub Actions en este proyecto.

---

## 📊 Estructura de ramas

```
main (producción)
  └── release tags (v0.1.0, v0.2.0, etc.)
  
develop (staging/pruebas)
  └── feature branches
  └── bugfix branches
```

### Ramas principales:

| Rama | Propósito | Quién merge | Publica a |
|------|-----------|-------------|-----------|
| `main` | Producción estable | PR aprobada | PyPI oficial |
| `develop` | Desarrollo y testing | PR aprobada | TestPyPI |
| `feature/*` | Nuevas funcionalidades | Merge a develop | Nada |
| `bugfix/*` | Correcciones de bugs | Merge a develop | Nada |

---

## 🔄 Flujo de trabajo paso a paso

### Paso 1: Crear una rama de trabajo

**Desde `develop`, crea una rama nueva:**

```bash
# Asegúrate de estar en develop
git checkout develop
git pull origin develop

# Crea una nueva rama
git checkout -b feature/nombre-de-la-funcionalidad
# O para bugfixes:
# git checkout -b bugfix/nombre-del-bug
```

**Convención de nombres:**
- `feature/add-logging` ✅
- `bugfix/fix-api-timeout` ✅
- `feature/WIP-new-feature` (si no está listo)
- `feature/user-auth` ✅
- `my-random-feature` ❌

---

### Paso 2: Hacer cambios y commits

**Mientras trabajas en tu rama:**

```bash
# Ver cambios
git status

# Añadir cambios
git add archivo1.py archivo2.py
# O todos los cambios:
git add .

# Hacer commit con mensaje descriptivo
git commit -m "feat: add new logging feature"
# O:
git commit -m "fix: resolve API timeout issue"
```

**Convención de commits (Conventional Commits):**
```
feat:     Nueva funcionalidad
fix:      Corrección de bug
docs:     Cambios en documentación
style:    Cambios de formato (no afecta lógica)
refactor: Refactorización de código
test:     Agregar o actualizar tests
chore:    Cambios en dependencias, configuración, etc.
ci:       Cambios en CI/CD (GitHub Actions, etc.)
```

**Ejemplos:**
```bash
git commit -m "feat: add user authentication"
git commit -m "fix: resolve memory leak in request handler"
git commit -m "docs: update API documentation"
git commit -m "ci: improve GitHub Actions workflow"
```

---

### Paso 3: Push a GitHub

**Sube tu rama a GitHub:**

```bash
git push origin feature/nombre-de-la-funcionalidad
```

---

### Paso 4: Crear un Pull Request (PR)

**En GitHub:**

1. Ve a tu repositorio: https://github.com/michelcub/fast-langchain-server
2. Verás una sugerencia: **"Create pull request"**
3. Click en **"Compare & pull request"**
4. **Completa el PR:**
   - **Title:** Breve descripción (ej: "Add user authentication")
   - **Description:** Explica qué cambios hace
   - **Base branch:** `develop` (por defecto debería estar)
   - **Compare branch:** Tu rama (ej: `feature/add-logging`)
5. Click en **"Create pull request"**

**Ejemplo de descripción de PR:**
```markdown
## Descripción
Agrega logging completo al servidor para mejor debugging.

## Cambios
- Configura logger centralizado
- Agrega logs en endpoints críticos
- Documenta niveles de logging

## Testing
- Probado en localhost
- Todos los tests pasan

Closes #123 (si cierras un issue)
```

---

### Paso 5: Revisión del PR

**Que sucede:**
1. GitHub Actions ejecuta automáticamente tests
2. Alguien revisa tu código
3. Se solicitan cambios o se aprueba

**Si necesitas hacer cambios:**
```bash
# Haz los cambios en tu rama
git add .
git commit -m "fix: address review comments"
git push origin feature/nombre-de-la-funcionalidad
```

El PR se actualiza automáticamente.

---

### Paso 6: Merge del PR

**Una vez aprobado:**

1. Click en **"Merge pull request"**
2. Elige merge strategy (por defecto está bien)
3. Click en **"Confirm merge"**
4. Opcionalmente: **"Delete branch"** (recomendado)

---

## 🤖 GitHub Actions - Cómo funciona la automatización

### Flujo automático:

```
┌─────────────────────────────────────────────────────────┐
│                    Tu código                            │
└────────┬─────────────────────────────────────┬──────────┘
         │                                      │
         v                                      v
    PUSH a develop                         PUSH a main
         │                                      │
         v                                      v
  ✅ Tests automáticos                  ✅ Tests automáticos
  ✅ Construye paquete                  ✅ Construye paquete
  ✅ Verifica integridad                ✅ Verifica integridad
  ✅ Sube a TestPyPI                    ✅ Sube a PyPI oficial
         │                                      │
         v                                      v
  test.pypi.org                           pypi.org
  (testing)                               (producción)
```

---

### Eventos que disparan Actions:

#### 1. **Push a `develop`**
```bash
git push origin develop
```
✅ Acciones que se ejecutan:
- Corre tests
- Construye paquete
- Publica en **TestPyPI**
- URL: https://test.pypi.org/project/fast-langchain-server/

**Prueba la instalación:**
```bash
pip install -i https://test.pypi.org/simple/ fast-langchain-server
```

---

#### 2. **Push a `main`**
```bash
git push origin main
```
✅ Acciones que se ejecutan:
- Corre tests
- Construye paquete
- Publica en **PyPI oficial**
- URL: https://pypi.org/project/fast-langchain-server/

**Instala normalmente:**
```bash
pip install fast-langchain-server
```

---

#### 3. **Manual (workflow_dispatch)**
```
GitHub → Actions → Publish to PyPI → Run workflow
```
- Selecciona `testpypi` o `pypi`
- Se ejecuta manualmente

---

#### 4. **Crear un Release**
```
GitHub → Releases → Create a new release
```
- **Draft release** → Publica en TestPyPI
- **Published release** → Publica en PyPI oficial

---

## 📝 Versioning

El proyecto usa **Semantic Versioning (MAJOR.MINOR.PATCH)**

Formato: `v0.1.0`
- **MAJOR** (0): Cambios incompatibles
- **MINOR** (1): Nuevas funcionalidades compatibles
- **PATCH** (0): Correcciones de bugs

**Ejemplos de cambios de versión:**
```
v0.1.0 → v0.1.1  (bugfix)
v0.1.1 → v0.2.0  (nueva funcionalidad)
v0.2.0 → v1.0.0  (cambios que rompen compatibilidad)
```

**Dónde cambiar la versión:**
```
pyproject.toml → version = "0.1.0"
```

---

## 🚀 Flujo completo de ejemplo

### Escenario: Agregar una nueva funcionalidad

**1. Crear rama:**
```bash
git checkout develop
git pull origin develop
git checkout -b feature/add-redis-support
```

**2. Hacer cambios:**
```bash
# Editar archivos...
git add .
git commit -m "feat: add Redis cache support"
```

**3. Push:**
```bash
git push origin feature/add-redis-support
```

**4. Crear PR en GitHub:**
- Title: "Add Redis cache support"
- Description: Explica qué hace

**5. GitHub Actions verifica automáticamente:**
- ✅ Tests pasan
- ✅ Paquete construye correctamente
- ✅ Code quality checks

**6. Revisión y aprobación:**
- Alguien revisa y aprueba
- O solicita cambios

**7. Merge:**
```
Click "Merge pull request"
```

**8. Automático - GitHub Actions:**
- ✅ Publica en TestPyPI
- ✅ Disponible en `test.pypi.org`

**9. Probar (opcional):**
```bash
pip install -i https://test.pypi.org/simple/ fast-langchain-server
```

**10. Cuando esté listo para producción:**
- Merge a `main`
- GitHub Actions automáticamente publica en PyPI oficial
- Los usuarios instalan: `pip install fast-langchain-server`

---

## ⚠️ Casos especiales

### Caso 1: Necesito deshacer cambios en mi rama

```bash
# Ver el historial
git log --oneline

# Deshacer último commit (mantiene cambios)
git reset --soft HEAD~1

# Deshacer último commit (descarta cambios)
git reset --hard HEAD~1
```

---

### Caso 2: Mi rama está desactualizada

```bash
# Traer cambios de develop
git fetch origin
git rebase origin/develop
# O merge (más simple):
git merge origin/develop
```

---

### Caso 3: Necesito cambiar a otra rama

```bash
# Ver ramas locales
git branch

# Ver todas las ramas
git branch -a

# Cambiar de rama
git checkout nombre-rama
```

---

### Caso 4: El PR tiene conflictos

```bash
# Actualiza tu rama con develop
git fetch origin
git merge origin/develop

# Resuelve conflictos manualmente en tus editores
# Busca: <<<<<<< HEAD, =======, >>>>>>>

# Una vez resueltos:
git add .
git commit -m "fix: resolve merge conflicts"
git push origin tu-rama
```

---

## 📊 Monitorear el estado del workflow

### En GitHub:

1. Ve a **Actions**
2. Verás un listado de todos los workflows ejecutados
3. Click en uno para ver detalles
4. **Status:**
   - 🟡 En curso
   - ✅ Exitoso
   - ❌ Falló

### Si falla:

1. Click en el workflow fallido
2. Expande el paso que falló
3. Lee el error
4. Soluciona en tu rama local
5. Push de nuevo

---

## 🔒 Buenas prácticas

✅ **Haz:**
- Commits pequeños y enfocados
- Mensajes de commit descriptivos
- PRs con descripción clara
- Prueba en `develop` antes de `main`
- Espera aprobación antes de mergear

❌ **No hagas:**
- Commits sin descripción ("fix" o "update")
- PRs enormes con muchos cambios
- Push directo a `main` o `develop`
- Mergear tu propio PR sin revisión
- Cambios sin tests

---

## 🆘 Troubleshooting

### Problema: "fatal: not a git repository"
```bash
# Asegúrate de estar en el directorio correcto
cd /Users/mdev/workspace/IA/server/langchain-agent-server
```

### Problema: "Permission denied"
```bash
# Verifica tu SSH key
ssh -T git@github.com

# Si no funciona, configura SSH:
# https://docs.github.com/en/authentication/connecting-to-github-with-ssh
```

### Problema: "branch develop does not exist"
```bash
# Crea develop localmente
git checkout -b develop origin/develop
```

### Problema: "Your branch is behind by 5 commits"
```bash
# Trae los cambios
git pull origin develop
```

---

## 📚 Recursos útiles

- [Git Documentation](https://git-scm.com/doc)
- [GitHub Flow Guide](https://guides.github.com/introduction/flow/)
- [Conventional Commits](https://www.conventionalcommits.org/)
- [Semantic Versioning](https://semver.org/)
- [GitHub Actions Docs](https://docs.github.com/en/actions)

---

## Resumen rápido

```bash
# Actualizar develop local
git checkout develop && git pull origin develop

# Crear rama de trabajo
git checkout -b feature/mi-funcionalidad

# Hacer cambios y commit
git add .
git commit -m "feat: descripción de cambios"

# Push a GitHub
git push origin feature/mi-funcionalidad

# En GitHub: Crear PR
# En GitHub: Esperar aprobación
# En GitHub: Mergear PR

# GitHub Actions automáticamente:
# - Corre tests
# - Construye paquete
# - Publica en TestPyPI (si es develop)
# - Publica en PyPI (si es main)
```

---

¿Preguntas? Consulta esta guía o pide ayuda. 🚀
