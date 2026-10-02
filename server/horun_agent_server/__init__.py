"""Lado SERVIDOR do Agente Horun — o mesmo para todo módulo (RE7S,
Financeiro, LECO...). Antes cada módulo tinha a sua cópia (RE7S e a branch
wip/drive-agent do Financeiro), divergindo: cada uma tinha correções que a
outra não tinha. Agora é um pacote só, copiado para dentro de cada módulo em
`backend/app/agent_server/` por `scripts/vendor_server.py` (mesma ideia do
design-system: o build Docker do módulo só enxerga o próprio repositório).

Peças:
- `models`  — tabelas AgentEnrollCode / AgentDevice / AgentTask (mesmos
              nomes e colunas que o RE7S já tinha em produção) e
              `MIGRATIONS`, as colunas novas para o módulo garantir;
- `bridge`  — o que as rotas do módulo chamam: read_text/read_bytes (com
              leitura em pedaços), write_text, list_files, move_files;
- `routes`  — `build_router(...)`: /agent/enroll, /agent/tasks, resultado,
              códigos de enrolamento e instalações (admin);
- `settings`— prazos e opções, ajustáveis por cada módulo.

NÃO EDITE a cópia dentro de um módulo: mude aqui (repositório Agent-Horun)
e rode `scripts/vendor_server.py` de novo.
"""

PACKAGE_VERSION = "0.4.0"
