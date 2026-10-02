#!/usr/bin/env python3
"""Copia o pacote do servidor do agente para dentro de um módulo.

Destino: <módulo>/backend/app/agent_server/ — dentro de `app/` de propósito:
o Dockerfile dos módulos só copia `app/`, então o pacote vai junto na imagem,
sem pip, sem rede e sem credencial do GitHub. O módulo importa como
`from app.agent_server import bridge, models, routes, settings`.

A cópia leva `HORUN_AGENT_SERVER_VERSION` (versão do pacote, commit do
Agent-Horun, data, hash do conteúdo). Atualizar = rodar de novo; conferir
se está desatualizada = `--check`. Mesma ideia do design-system
(Horun-Core/scripts/vendor_design_system.py).

Uso (a partir da pasta do Agent-Horun):
    python scripts/vendor_server.py "../Horun RE7S/backend"
    python scripts/vendor_server.py "../Horun RE7S/backend" --check
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "server" / "horun_agent_server"
TARGET_REL = Path("app") / "agent_server"
STAMP = "HORUN_AGENT_SERVER_VERSION"


def _files(root: Path) -> dict[str, Path]:
    return {
        p.relative_to(root).as_posix(): p
        for p in sorted(root.rglob("*.py"))
        if "__pycache__" not in p.parts
    }


def _hash(root: Path) -> str:
    digest = hashlib.sha256()
    for rel, p in _files(root).items():
        digest.update(rel.encode())
        digest.update(p.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()[:16]


def _commit() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "desconhecido"


def vendor(backend: Path) -> Path:
    target = backend / TARGET_REL
    target.mkdir(parents=True, exist_ok=True)
    wanted = _files(SOURCE)
    for rel, src in wanted.items():
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    # sincroniza em vez de apagar e recriar (no Windows/OneDrive apagar a
    # pasta inteira falha quando algo está com ela aberta)
    for existing in _files(target):
        if existing not in wanted:
            (target / existing).unlink()
    version = re.search(r'PACKAGE_VERSION = "([^"]+)"', (SOURCE / "__init__.py").read_text(encoding="utf-8")).group(1)
    (target / STAMP).write_text(
        f"versao={version}\ncommit_agent_horun={_commit()}\ndata={date.today().isoformat()}\nconteudo={_hash(target)}\n"
        "# Cópia gerada por Agent-Horun/scripts/vendor_server.py — não edite aqui;\n"
        "# mude no Agent-Horun e rode o script de novo.\n",
        encoding="utf-8",
    )
    return target


def check(backend: Path) -> int:
    target = backend / TARGET_REL
    if not (target / "__init__.py").exists():
        print(f"sem cópia do servidor do agente em {target}")
        return 2
    if _hash(target) != _hash(SOURCE):
        print("desatualizada: o pacote mudou no Agent-Horun — rode o script sem --check")
        return 1
    print("em dia com o Agent-Horun")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("backend", type=Path, help="pasta backend/ do módulo (a que tem app/)")
    parser.add_argument("--check", action="store_true", help="só confere se a cópia está em dia")
    args = parser.parse_args()
    backend = args.backend.resolve()
    if not (backend / "app").is_dir():
        print(f"não achei {backend / 'app'}", file=sys.stderr)
        return 2
    if args.check:
        return check(backend)
    print(f"pacote copiado para {vendor(backend)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
