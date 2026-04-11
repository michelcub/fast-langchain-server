#!/usr/bin/env python3
"""
Script para publicar el paquete a PyPI
Uso: python scripts/publish.py [--test] [--username USER] [--password PASS]
"""

import sys
import subprocess
import shutil
import os
from pathlib import Path
import argparse
from typing import Optional


class Colors:
    GREEN = '\033[0;32m'
    RED = '\033[0;31m'
    YELLOW = '\033[1;33m'
    RESET = '\033[0m'


def print_info(msg: str):
    print(f"{Colors.GREEN}[INFO]{Colors.RESET} {msg}")


def print_error(msg: str):
    print(f"{Colors.RED}[ERROR]{Colors.RESET} {msg}", file=sys.stderr)


def print_warning(msg: str):
    print(f"{Colors.YELLOW}[WARNING]{Colors.RESET} {msg}")


def run_command(cmd: list, description: str) -> bool:
    """Ejecuta un comando y retorna True si es exitoso"""
    print_info(f"{description}...")
    try:
        result = subprocess.run(cmd, check=False, capture_output=False)
        return result.returncode == 0
    except Exception as e:
        print_error(f"Error al ejecutar comando: {e}")
        return False


def clean_previous_builds():
    """Limpia builds anteriores"""
    print_info("Limpiando builds anteriores...")
    dirs_to_remove = ['build', 'dist', '*.egg-info']

    for pattern in dirs_to_remove:
        for path in Path('.').glob(pattern):
            if path.is_dir():
                shutil.rmtree(path)
                print_info(f"Eliminado: {path}")


def build_package() -> bool:
    """Construye el paquete"""
    return run_command(
        [sys.executable, '-m', 'build'],
        "Construyendo paquete"
    )


def check_package_integrity() -> bool:
    """Verifica la integridad del paquete"""
    dist_path = Path('dist')
    if not dist_path.exists() or not list(dist_path.glob('*')):
        print_error("No se encontraron archivos en dist/")
        return False

    print_info("Archivos creados:")
    for file in dist_path.glob('*'):
        size = file.stat().st_size / 1024  # KB
        print(f"  - {file.name} ({size:.1f} KB)")

    # Verificar con twine
    print_info("Verificando integridad del paquete...")
    return run_command(
        [sys.executable, '-m', 'twine', 'check', 'dist/*'],
        "Verificación"
    )


def publish_to_pypi(
    is_test: bool = False,
    username: Optional[str] = None,
    password: Optional[str] = None
) -> bool:
    """Sube el paquete a PyPI"""

    if is_test:
        repo_url = "https://test.pypi.org/legacy/"
        print_warning("Publicando en TestPyPI (ambiente de prueba)")
    else:
        repo_url = "https://upload.pypi.org/legacy/"
        print_warning("⚠️  PUBLICANDO EN PYPI OFICIAL")

        # Pedir confirmación para prod
        response = input(f"{Colors.YELLOW}¿Estás seguro? Escribe 'si' para continuar:{Colors.RESET} ")
        if response.lower() != "si":
            print_info("Publicación cancelada")
            return True

    cmd = [
        sys.executable, '-m', 'twine', 'upload',
        'dist/*',
        '--repository-url', repo_url
    ]

    # Agregar credenciales si se proporcionan
    if username and password:
        cmd.extend(['--username', username, '--password', password])
        print_info("Usando credenciales proporcionadas")
    else:
        # Verificar variables de entorno
        if os.getenv('TWINE_USERNAME') and os.getenv('TWINE_PASSWORD'):
            print_info("Usando variables de entorno TWINE_USERNAME y TWINE_PASSWORD")
        else:
            print_info("Se te pedirán las credenciales interactivamente")

    return run_command(cmd, f"Subiendo a {'TestPyPI' if is_test else 'PyPI'}")


def main():
    parser = argparse.ArgumentParser(
        description='Publica el paquete a PyPI'
    )
    parser.add_argument(
        '--test',
        action='store_true',
        help='Publica en TestPyPI en lugar de PyPI (por defecto)'
    )
    parser.add_argument(
        '--username',
        help='Email/usuario de PyPI'
    )
    parser.add_argument(
        '--password',
        help='Contraseña o token de PyPI'
    )
    parser.add_argument(
        '--skip-checks',
        action='store_true',
        help='Salta la verificación del paquete'
    )

    args = parser.parse_args()

    # Verificar dependencias
    try:
        import twine
        import build
    except ImportError:
        print_error("Se requieren: pip install twine build")
        sys.exit(1)

    # Verificar que estamos en el directorio correcto
    if not Path('pyproject.toml').exists():
        print_error("No se encontró pyproject.toml en el directorio actual")
        sys.exit(1)

    try:
        # Limpiar builds anteriores
        clean_previous_builds()

        # Construir paquete
        if not build_package():
            sys.exit(1)

        # Verificar integridad (opcional)
        if not args.skip_checks:
            if not check_package_integrity():
                print_warning("Continuando a pesar de las advertencias...")

        # Publicar
        if not publish_to_pypi(
            is_test=args.test,
            username=args.username,
            password=args.password
        ):
            sys.exit(1)

        print_info("✅ Paquete publicado exitosamente")

        package_name = "fast-langchain-server"
        if args.test:
            print_info(f"Verificar en: https://test.pypi.org/project/{package_name}/")
        else:
            print_info(f"Verificar en: https://pypi.org/project/{package_name}/")

    except KeyboardInterrupt:
        print_warning("Publicación cancelada por el usuario")
        sys.exit(1)
    except Exception as e:
        print_error(f"Error inesperado: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
