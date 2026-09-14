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

O agente só sabe fazer três coisas — **ler um arquivo**, **escrever um
arquivo** (com segurança, por substituição atômica) e **listar arquivos**
de uma pasta — dentro de uma lista de pastas explicitamente permitidas por
instalação (`roots`, no `config.json`). Ele **nunca** interpreta o
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

## Rodando em desenvolvimento

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
copy config.example.json config.json   # preencher server_url e roots locais de teste
.venv/Scripts/python -m agent
```

Sem um servidor de verdade rodando ainda, use os testes (`pytest`) pra
validar as três operações (`ler`/`escrever`/`listar`) e a lista de pastas
permitidas isoladamente — são a parte que não depende de rede nenhuma.

## Instalando como serviço do Windows (no PC do equipamento)

Ver [`install/README.md`](install/README.md) — usa o
[NSSM](https://nssm.cc/) pra rodar `python -m agent` como serviço, sem
depender de terminal aberto nem de login do Windows.

## Status

Esqueleto inicial: primitivas de arquivo + lista de permissões + loop de
consulta ao servidor, com testes cobrindo o que não depende de rede.
**Ainda não integrado a nenhum backend de módulo de verdade** — o próximo
passo é implementar o lado servidor (`POST /agent/enroll`, `GET
/agent/tasks`, `POST /agent/tasks/{id}/result`) no `RE7S-Horun` e trocar,
um arquivo de cada vez, as leituras/escritas diretas em disco por
chamadas que passam pelo agente quando o backend estiver rodando em modo
Core (ver `Prompt_Fase2.md`, seção 6).
