# Verificaciones de Seguridad

Este documento explica las verificaciones de seguridad automatizadas que se ejecutan en el proyecto.

---

## 🔐 Capas de seguridad

### 1. **pip-audit** - Auditoría de dependencias
Verifica si las librerías instaladas tienen **vulnerabilidades conocidas**.

**Qué detecta:**
- CVEs (Common Vulnerabilities and Exposures)
- Versiones vulnerables de paquetes
- Dependencias obsoletas

**Cuándo se ejecuta:**
- En cada push a `develop` y `main`
- Diariamente a las 2 AM UTC
- En pull requests

**Ejemplo de salida:**
```
Found 0 known security vulnerabilities
```

---

### 2. **Bandit** - Análisis de código Python
Escanea tu código Python para encontrar **problemas de seguridad comunes**.

**Qué detecta:**
- SQL injection
- Comandos de sistema inseguros
- Contraseñas hardcodeadas
- Uso inseguro de funciones
- Permisos incorrectos

**Cuándo se ejecuta:**
- En cada push a `develop` y `main`
- Diariamente a las 2 AM UTC
- En pull requests

**Ejemplo de problemas que encuentra:**
```
Issue: [B105:hardcoded_password_string] Possible hardcoded password
Severity: MEDIUM
Confidence: MEDIUM
Location: app.py:42
```

---

### 3. **Safety** - Base de datos de vulnerabilidades
Verifica contra una **base de datos de vulnerabilidades conocidas** en Python.

**Diferencia con pip-audit:**
- Usa su propia base de datos
- Más enfocado en vulnerabilidades de Python
- Integrado con servicios en línea

**Cuándo se ejecuta:**
- En cada push
- Diariamente

---

### 4. **CodeQL** - Análisis estático avanzado
Herramienta de GitHub que hace **análisis profundo del código**.

**Qué detecta:**
- Flujos de datos inseguros
- Lógica vulnerables
- Puntos de inyección de código
- Usos peligrosos de APIs

**Cuándo se ejecuta:**
- En cada push a `develop` y `main`
- Diariamente

**Resultados:**
- Se muestran en **Security** → **Code scanning alerts**

---

### 5. **pip-licenses** - Análisis de licencias
Verifica las **licencias de todas las dependencias**.

**Qué detecta:**
- Licencias incompatibles (GPL, AGPL, etc.)
- Licencias de las dependencias
- URLs de licencias

**Cuándo se ejecuta:**
- En cada push
- Diariamente

**Licencias comunes:**
- ✅ MIT, Apache 2.0, BSD → OK
- ⚠️ GPL, AGPL → Revisar

---

### 6. **Dependabot** - Actualizaciones automáticas de dependencias
GitHub verifica **automáticamente si hay nuevas versiones** de tus dependencias.

**Qué hace:**
- Detecta nuevas versiones disponibles
- Verifica si tienen vulnerabilidades
- Crea PRs automáticos con actualizaciones
- Incluye notas de cambio

**Configuración:**
- Ejecuta **semanalmente** los lunes
- Abre máximo 5 PRs simultáneos
- Etiquetadas con `dependencies` y `security`

**Cómo funciona:**
1. Dependabot detecta una nueva versión
2. Verifica si hay vulnerabilidades
3. Crea un PR con los cambios
4. GitHub Actions ejecuta tests
5. Si pasan → puedes mergear

---

## 📊 Dónde ver los resultados

### En GitHub:

**1. Security tab:**
```
Repository
→ Security
→ Code scanning alerts (CodeQL)
→ Dependabot alerts
→ Dependency graph
```

**2. Actions tab:**
```
Repository
→ Actions
→ "Security Checks" workflow
```

**3. Pull Requests:**
Si hay un problema de seguridad:
- Verás un comentario de Dependabot
- GitHub bloqueará el merge si es crítico

---

## 🚨 Qué hacer si falla una verificación de seguridad

### Caso 1: Dependencia vulnerable

**Problema:** pip-audit detecta una librería vulnerable
```
Found 1 known security vulnerability
langchain [1.0.0] has a vulnerability:
  - CVE-2024-xxxxx
```

**Solución:**
```bash
# Actualizar la dependencia
pip install --upgrade langchain

# Actualizar en pyproject.toml
# langchain>=1.0.0 → langchain>=1.1.0
```

### Caso 2: Bandit detecta problema de seguridad

**Problema:**
```
[B105:hardcoded_password_string] Possible hardcoded password
```

**Solución:**
```python
# ❌ Mal
API_KEY = "sk-1234567890"

# ✅ Bien
import os
API_KEY = os.getenv("API_KEY")
```

### Caso 3: CodeQL detecta flujo de datos inseguro

**Ver detalles:**
1. Ve a **Security** → **Code scanning alerts**
2. Click en la alerta
3. Lee la explicación
4. Arregla el código

### Caso 4: Licencia incompatible

**Problema:**
```
Dependencia XYZ usa licencia GPL
```

**Solución:**
- Reemplaza con alternativa con licencia MIT/Apache
- O solicita excepción legal

---

## 🔧 Configuración local

### Ejecutar Bandit localmente:

```bash
pip install bandit
bandit -r fast_langchain_server/
```

### Ejecutar pip-audit localmente:

```bash
pip install pip-audit
pip-audit
```

### Ejecutar Safety localmente:

```bash
pip install safety
safety check
```

---

## 📅 Cronograma de ejecuciones

| Herramienta | Trigger | Frecuencia |
|-------------|---------|-----------|
| pip-audit | Push, schedule | Cada push + diario |
| Bandit | Push, schedule | Cada push + diario |
| Safety | Push, schedule | Cada push + diario |
| CodeQL | Push, schedule | Cada push + diario |
| pip-licenses | Push, schedule | Cada push + diario |
| Dependabot | Schedule | Semanalmente (lunes) |

---

## ✅ Mejores prácticas de seguridad

✅ **Haz:**
- Actualiza dependencias regularmente
- Revisa las alertas de Dependabot
- Arregla problemas de seguridad rápidamente
- Usa variables de entorno para secretos
- Valida entrada de usuarios
- Usa librerías establecidas

❌ **No hagas:**
- Hardcodees contraseñas o API keys
- Ignores alertas de seguridad
- Ejecutes código sin validar entrada
- Uses librerías desconocidas
- Expongas datos sensibles en logs
- Ignores actualizaciones de seguridad

---

## 🔗 Recursos

- [OWASP Top 10](https://owasp.org/www-project-top-ten/)
- [Bandit Documentation](https://bandit.readthedocs.io/)
- [pip-audit](https://pypi.org/project/pip-audit/)
- [CodeQL](https://codeql.com/)
- [Dependabot Documentation](https://docs.github.com/en/code-security/dependabot)

---

## 📞 Si una verificación falla

1. **Lee el error** con atención
2. **Identifica el problema** (vulnerable dependency, código inseguro, etc.)
3. **Busca en la documentación** de la herramienta
4. **Arregla el problema**
5. **Prueba localmente** antes de push
6. **Haz un nuevo commit**

Si no entiendes un error, pregunta. 🚀

