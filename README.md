# Horun Agent

Serviço pequeno, instalado no computador de **cada equipamento** do
laboratório NQTR (Rock-Eval, LECO, e os que vierem depois), cuja única
função é dar ao Horun (rodando no servidor dedicado, atrás do
[Horun Core](https://github.com/guimneves/Horun-Core)) acesso aos arquivos
locais daquele equipamento — sem que o computador do equipamento precise
abrir nenhuma porta de entrada.

> Contexto completo da decisão de arquitetura (por que um agente, em vez
> de o backend do módulo continuar rodando no PC do equipamento, ou de um
> compartilhamento de rede): `Prompt_refinado.md` do RE7S, seção 7.1, e
> `Prompt_Fase2.md`/`Prompt_Horun_Core.md` do Horun Core, seção 8 (itens 4
> e 5 do backlog).

## A ideia em uma frase

O agente só sabe fazer quatro coisas — **ler um arquivo** (inteiro ou em
pedaços), **escrever um arquivo** (com segurança, por substituição
atômica), **listar arquivos** e **mover/renomear um grupo de arquivos**
(tudo ou nada) — dentro de uma lista de pastas explicitamente permitidas
por instalação (`roots`, no `config.json`), cada uma com a sua permissão
(`read`, `read-move` ou `read-write`). Ele **nunca** interpreta o
conteúdo desses arquivos (isso é sempre trabalho do backend do módulo,
ex. `RE7S-Horun/backend/app/modules/tabsample.py`) — é por isso que o
mesmo agente serve pra qualquer equipamento: o que muda de instalação pra
instalação é só a configuração (quais pastas, qual servidor, qual token),
nunca o código.

## Como ele conversa com o servidor

1. **Enrolamento, uma vez só**: o administrador gera um código de uso
   único no módulo (mesmo padrão já usado no Horun Core pra criar usuário
   sem senha). O agente troca esse código por uma credencial permanente
   própria daquela instalação (`POST /agent/enroll` — ver `PROTOCOL.md`).
2. **Consulta periódica** (a cada poucos segundos, configurável): o
   agente pergunta ao servidor "tem tarefa pra mim?" (`GET
   /agent/tasks`) — a conexão é **sempre iniciada pelo agente**, nunca o
   contrário, então o computador do equipamento não precisa aceitar
   nenhuma conexão de entrada.
3. **Executa e reporta**: cada tarefa é uma leitura, escrita ou listagem
   dentro de um `root` configurado; o resultado volta pro servidor
   (`POST /agent/tasks/{id}/result`).
4. Se o agente estiver desligado ou sem rede, a tarefa fica **pendente**
   do lado do servidor — nada se perde, só espera a próxima consulta bem
   sucedida.

Contrato completo (formato exato de cada chamada) em [`PROTOCOL.md`](PROTOCOL.md).

## Configuração de cada instalação (`config.json`)

Um arquivo por PC de equipamento, nunca commitado. Formato com a lista de
módulos atendidos (`servers`) e a permissão de cada pasta — ver
`config.example.json`:

```json
{
  "device_name": "PC-ROCKEVAL-65",
  "poll_interval_seconds": 3,
  "servers": [
    {
      "url": "https://192.168.31.80/m/re7s",
      "enroll_code": "",
      "device_token": "",
      "roots": {
        "data":        { "path": "C:\\VT RE7S\\data\\system", "mode": "read-write" },
        "jobs":        { "path": "C:\\Users\\RE7S 65\\Documents\\RE7raw-data", "mode": "read-move" },
        "mtl_results": { "path": "D:\\Results", "mode": "read" }
      }
    }
  ]
}
```

- **Um PC atendendo dois módulos** (ex. o OneDrive do Financeiro
  sincronizado no mesmo PC): um segundo bloco em `servers`, com a `url` do
  outro módulo, o código de enrolamento gerado **naquele** módulo e as
  pastas dele (ex. `"drive": {"path": "...", "mode": "read"}`).
- Cada módulo enrola separadamente: gere o código no módulo, ponha em
  `enroll_code` daquele bloco, reinicie o agente — ele troca pelo token,
  salva no arquivo e apaga o código.
- O formato antigo (`server_url` + `roots` com caminhos em texto) continua
  valendo igual, com permissão total nas pastas.

## Lado servidor (para quem mantém um módulo)

O mesmo pacote para todo módulo, em `server/horun_agent_server/`: tabelas,
rotas `/agent/*` e a ponte (`read_text`, `read_bytes` em pedaços,
`write_text`, `list_files`, `move_files`). Vai para dentro de cada módulo por
cópia versionada:

```bash
python scripts/vendor_server.py "<módulo>/backend"          # copia para app/agent_server/
python scripts/vendor_server.py "<módulo>/backend" --check  # está em dia?
```

Não edite a cópia no módulo — mude aqui e rode o script de novo. O módulo
monta as rotas com `build_router(...)` e garante as colunas de
`models.MIGRATIONS` (ver docstrings do pacote).

## Rodando em desenvolvimento

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[server-dev]"   # [dev] basta se não for mexer em server/
copy config.example.json config.json            # preencher servidores e pastas locais de teste
.venv/Scripts/python -m agent
```

`pytest` roda os testes do agente (`tests/`) e do pacote do servidor
(`server/tests/`, de ponta a ponta: servidor e agente de verdade
conversando, sem rede).

## Instalando como serviço do Windows (no PC do equipamento)

Ver [`install/README.md`](install/README.md) — usa o
[NSSM](https://nssm.cc/) pra rodar `python -m agent` como serviço, sem
depender de terminal aberto nem de login do Windows.

## Status

Versão 0.3.0. Em produção no RE7S (PC do Rock-Eval): leitura/escrita do
TABSAMPLE, filas, savecycle, standard.ini, .B00 do PostRun, pesagem e
renomeação de análises. O lado servidor é o pacote `server/`, usado pelo
RE7S e preparado para o Financeiro (drive).
