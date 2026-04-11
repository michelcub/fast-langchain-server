#!/bin/bash

# Script para publicar el paquete a PyPI
# Uso: ./scripts/publish-to-pypi.sh [test|prod] [username] [password]

set -e

# Colores para output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuración
ENVIRONMENT=${1:-"test"}
USERNAME=${2:-""}
PASSWORD=${3:-""}
PACKAGE_NAME="fast-langchain-server"

# Función para mostrar mensajes
print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

# Función para mostrar ayuda
show_help() {
    cat << EOF
Uso: ./scripts/publish-to-pypi.sh [ENVIRONMENT] [USERNAME] [PASSWORD]

ENVIRONMENT:
  test    - Publica en TestPyPI (por defecto)
  prod    - Publica en PyPI oficial (requiere confirmación)

USERNAME:
  Tu email de PyPI (o usa variable TWINE_USERNAME)

PASSWORD:
  Tu contraseña o token de PyPI (o usa variable TWINE_PASSWORD)

Ejemplos:
  ./scripts/publish-to-pypi.sh test
  ./scripts/publish-to-pypi.sh test user@example.com mypassword
  ./scripts/publish-to-pypi.sh prod

Variables de entorno (alternativa):
  export TWINE_USERNAME="tu_email@example.com"
  export TWINE_PASSWORD="tu_contraseña"
  ./scripts/publish-to-pypi.sh prod
EOF
}

# Validar argumentos
if [[ "$ENVIRONMENT" == "-h" || "$ENVIRONMENT" == "--help" ]]; then
    show_help
    exit 0
fi

if [[ "$ENVIRONMENT" != "test" && "$ENVIRONMENT" != "prod" ]]; then
    print_error "ENVIRONMENT debe ser 'test' o 'prod'"
    show_help
    exit 1
fi

# Verificar dependencias
print_info "Verificando dependencias..."
if ! command -v twine &> /dev/null; then
    print_error "twine no está instalado. Instálalo con: pip install twine"
    exit 1
fi

if ! command -v python3 &> /dev/null; then
    print_error "python3 no está instalado"
    exit 1
fi

# Limpiar builds anteriores
print_info "Limpiando builds anteriores..."
rm -rf build/ dist/ *.egg-info

# Verificar que pyproject.toml existe
if [ ! -f "pyproject.toml" ]; then
    print_error "No se encontró pyproject.toml en el directorio actual"
    exit 1
fi

# Construir el paquete
print_info "Construyendo paquete..."
python3 -m build
if [ $? -ne 0 ]; then
    print_error "Error al construir el paquete"
    exit 1
fi

# Verificar que los archivos se crearon
if [ ! -d "dist" ] || [ -z "$(ls -A dist/)" ]; then
    print_error "No se encontraron archivos en dist/"
    exit 1
fi

print_info "Archivos creados:"
ls -lh dist/

# Verificar integridad del paquete
print_info "Verificando integridad del paquete..."
python3 -m twine check dist/*
if [ $? -ne 0 ]; then
    print_warning "La verificación del paquete mostró advertencias (pero continúa)"
fi

# Configurar URL según ambiente
if [[ "$ENVIRONMENT" == "test" ]]; then
    REPOSITORY_URL="https://test.pypi.org/legacy/"
    print_warning "Publicando en TestPyPI (ambiente de prueba)"
else
    REPOSITORY_URL="https://upload.pypi.org/legacy/"
    print_warning "⚠️  PUBLICANDO EN PYPI OFICIAL"
    echo -e "${YELLOW}¿Estás seguro? Escribe 'si' para continuar:${NC}"
    read -r confirmation
    if [[ "$confirmation" != "si" ]]; then
        print_info "Publicación cancelada"
        exit 0
    fi
fi

# Preparar comando twine
TWINE_CMD="python3 -m twine upload dist/* --repository-url $REPOSITORY_URL"

if [ -n "$USERNAME" ] && [ -n "$PASSWORD" ]; then
    TWINE_CMD="$TWINE_CMD --username '$USERNAME' --password '$PASSWORD'"
    print_info "Usando credenciales proporcionadas"
elif [ -n "$TWINE_USERNAME" ] && [ -n "$TWINE_PASSWORD" ]; then
    print_info "Usando variables de entorno TWINE_USERNAME y TWINE_PASSWORD"
else
    print_info "Se te pedirán las credenciales (usuario y contraseña)"
fi

# Subir a PyPI
print_info "Subiendo paquete a $ENVIRONMENT PyPI..."
eval "$TWINE_CMD"

if [ $? -eq 0 ]; then
    print_info "✅ Paquete publicado exitosamente"

    if [[ "$ENVIRONMENT" == "test" ]]; then
        print_info "Verificar en: https://test.pypi.org/project/$PACKAGE_NAME/"
    else
        print_info "Verificar en: https://pypi.org/project/$PACKAGE_NAME/"
    fi
else
    print_error "Error al subir el paquete"
    exit 1
fi
