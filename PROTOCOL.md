# Protocolo agente ↔ servidor

Três chamadas HTTP, todas iniciadas pelo agente (nunca o servidor abre
conexão com o equipamento). O servidor aqui é o backend de um módulo do
Horun (ex. `RE7S-Horun`), não o Horun Core — o Core só faz o proxy de
`/m/re7s/*`, o contrato abaixo é implementado dentro do próprio módulo,
em rotas do tipo `/agent/*`.

## 1. Enrolamento — `POST {server_url}/agent/enroll`

Uma vez por instalação. Troca um código de uso único (gerado por um
admin do módulo, mesmo espírito do "código de primeiro acesso" já usado
no Horun Core) por uma credencial permanente.

**Request**
```json
{ "enroll_code": "A1B2C3D4", "device_name": "PC-ROCKEVAL-65" }
```

**Response `200`**
```json
{ "device_token": "<token longo, opaco>" }
```

**Response `4xx`**: código inválido, já usado, ou expirado — mensagem em
`detail`, mesmo padrão de erro do FastAPI já usado no resto do Horun.

O agente salva o `device_token` no `config.json` local e nunca precisa
enrolar de novo, a menos que o admin revogue a instalação.

## 2. Consulta de tarefas pendentes — `GET {server_url}/agent/tasks`

**Headers**: `Authorization: Bearer <device_token>`

**Response `200`**
```json
{
  "tasks": [
    {
      "id": "task-123",
      "op": "read_file",           // "read_file" | "write_file" | "list_files" | "move_files"
      "root": "data",              // chave dentro de `roots` no config.json do agente
      "path": "TABSAMPLE.txt",     // caminho relativo ao root, sempre posix (/, nunca \)
      "content_base64": null,      // write_file: conteúdo a gravar; move_files: JSON dos movimentos
      "glob": null                 // só em list_files — ex. "**/*.B00"
    }
  ]
}
```

Lista vazia (`"tasks": []`) é a resposta normal na maior parte das
consultas — não é erro, só quer dizer "nada pendente agora".

## 3. Resultado de uma tarefa — `POST {server_url}/agent/tasks/{id}/result`

**Headers**: `Authorization: Bearer <device_token>`

**Request (sucesso, leitura)**
```json
{ "ok": true, "content_base64": "<conteúdo do arquivo, codificado>" }
```

**Request (sucesso, listagem)**
```json
{ "ok": true, "paths": ["BULK ROCK/2026-07-17_IFP160000_1.B00", "..."] }
```

**Request (sucesso, movimento)** — `paths` = destinos efetivamente movidos
```json
{ "ok": true, "paths": ["J2/BULK ROCK/2026-07-17_IFP160000_X_1.B00", "J2/BULK ROCK/2026-07-17_IFP160000_X_1.B00~"] }
```

**Request (sucesso, escrita)**
```json
{ "ok": true }
```

**Request (falha)**
```json
{ "ok": false, "error": "arquivo não encontrado: TABSAMPLE.txt" }
```

## `move_files` — renomear/mover um grupo de arquivos (desde 0.2.0)

Usado pelo RE7S para renomear uma análise já feita (nome e/ou job): o
`.B00` e o `.B00~` (cópia anterior do GeoWorks) andam juntos. Os movimentos
vão em JSON, codificado em base64, no campo `content_base64` — **não** num
campo novo, de propósito: um agente antigo que não conhece a operação só
responde `{"ok": false, "error": "operação desconhecida: 'move_files'"}`
em vez de quebrar.

```json
{ "moves": [
    { "from": "J1/BULK ROCK/a_1.B00",  "to": "J2/BULK ROCK/a_X_1.B00" },
    { "from": "J1/BULK ROCK/a_1.B00~", "to": "J2/BULK ROCK/a_X_1.B00~", "optional": true }
] }
```

- `optional: true` = a origem pode não existir (é pulada).
- **Nunca sobrescreve**: se qualquer destino existir, nada é movido.
- **Tudo ou nada**: falha no meio (ex. arquivo aberto) desfaz os anteriores.
- Origem e destino confinados ao mesmo root; pastas de destino são criadas.
- O conteúdo dos arquivos nunca é lido nem alterado.

## Versão do agente — cabeçalho `X-Horun-Agent-Version` (desde 0.3.0)

Toda chamada do agente leva `X-Horun-Agent-Version: 0.3.0`. O servidor
guarda isso na instalação (`GET /agent/devices` mostra) e só manda o que
aquela versão entende. Agente que não manda o cabeçalho (0.1/0.2) é tratado
como 0.2.0.

## Leitura em pedaços (desde 0.3.0)

Para arquivos grandes, `read_file` pode vir com `args`:

```json
{ "id": 9, "op": "read_file", "root": "drive", "path": "x.pdf",
  "content_base64": null, "glob": null, "args": { "offset": 0, "length": 4194304 } }
```

O resultado traz o pedaço em `content_base64` e o tamanho TOTAL em `size`
(o servidor pede o próximo pedaço até completar). `args` só é enviado a
agentes >= 0.3.0 — para os antigos, o servidor recusa a operação antes de
enfileirar, com a mensagem "atualize o agente".

## Compatibilidade

- Para agentes 0.1/0.2, cada tarefa tem **exatamente** os 6 campos
  `id, op, root, path, content_base64, glob` (o 0.1 quebra com um campo a
  mais ou a menos). Campos novos só vão para quem se identificou >= 0.3.0.
- Desde 0.2.0 o agente ignora campos desconhecidos e nenhuma falha na
  consulta encerra o serviço.
- Código de enrolamento: 10 caracteres, uso único (consumido de forma
  atômica), vale 60 minutos — expirado responde `410`.

## Permissão por pasta (desde 0.3.0, no `config.json`)

Cada `root` pode ter `mode`: `read` (ler/listar), `read-move` (+ mover/
renomear, nunca alterar conteúdo) ou `read-write` (tudo — o padrão, e o que
vale para pastas escritas só como texto). Tarefa fora da permissão volta
`{"ok": false, "error": "sem permissão para ... em 'x' ..."}`.

## Vários módulos no mesmo PC (desde 0.3.0)

`config.json` com `servers` (uma lista): o agente atende cada um em
sequência a cada rodada, cada um com o seu token e as suas pastas — um fora
do ar não atrapalha os outros. O formato antigo (um `server_url` só)
continua valendo. Exemplos em `config.example.json` e no README.

## Por que `base64` e não texto puro

Os arquivos do equipamento (`.B00`, `TABSAMPLE.txt`) são texto, mas o
protocolo não assume isso — `base64` deixa o transporte binário-seguro
sem precisar de um caso especial por tipo de arquivo. Quem decodifica e
interpreta o conteúdo (texto `\r\n`, INI, o que for) é sempre o backend
do módulo do lado do servidor, nunca o agente.

## Caminhos são sempre relativos a um `root` nomeado

O agente nunca recebe (nem aceita) um caminho absoluto. Cada instalação
define, no `config.json` local, a que pasta real cada `root` aponta (ex.
`"data": "C:\\VT RE7S\\data\\system"`). O servidor só conhece os nomes
lógicos dos roots (documentados por módulo, ex. `data`/`jobs`/
`mtl_results` pro RE7S) — nunca o caminho real do disco do equipamento.
Isso, mais a checagem de que o caminho resolvido nunca escapa do root
(mesmo mecanismo de `_resolve_token` já usado em `routes_postrun.py`/
`routes_weighing.py` do RE7S), é a proteção contra um comando malicioso
tentar ler/escrever fora do que foi explicitamente permitido.

## Erros de rede / servidor fora do ar

O agente trata qualquer falha de conexão na consulta (passo 2) como
"nada a fazer agora" — espera o intervalo configurado e tenta de novo,
sem derrubar o processo. Tarefas que o servidor já tinha enfileirado
continuam esperando do lado dele; nada é perdido, só atrasado.
