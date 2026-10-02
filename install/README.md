# Instalando o agente como serviço do Windows

Usamos o [NSSM](https://nssm.cc/) — transforma qualquer programa comum num
serviço do Windows: **inicia sozinho no boot, reinicia se cair e roda sem
ninguém logado**. Hoje, com o agente aberto num terminal, basta o PC do
equipamento reiniciar (atualização do Windows, queda de luz) para tudo que
depende dele parar — gravar no Rock-Eval, ler `.B00`, pesagem, renomear
análise.

Os passos abaixo são para o PC do Rock-Eval, mas valem para qualquer
equipamento. Rode o PowerShell **como administrador**.

## 0. Antes de começar

- **Feche o agente que está rodando no terminal** (Ctrl+C na janela dele) e
  confirme que não há outro `python -m agent` aberto (Gerenciador de
  Tarefas → `python.exe`). **Dois agentes ao mesmo tempo pegam as mesmas
  tarefas** e podem gravar o mesmo arquivo duas vezes.
- Atualize o agente para a versão mais nova (≥ 0.2.0):
  ```powershell
  cd C:\Horun\Horun-Agent        # a pasta onde o agente está clonado
  git pull
  .venv\Scripts\pip install -e .
  ```
- Confira o `config.json`: `server_url` tem que apontar para o IP **fixo**
  do servidor (`192.168.31.80` — se ainda estiver `.171`/`.117`, troque só o
  IP, mantendo o resto do endereço como está). No RE7S o endereço é a porta
  estreita do agente: `http://192.168.31.80:8001` (nunca `/m/re7s/`).

## 1. Baixar o NSSM

1. https://nssm.cc/download → `nssm-2.24.zip`.
2. Copie `win64\nssm.exe` para `C:\Windows\System32` (ou anote o caminho
   completo e use-o no lugar de `nssm` abaixo).
3. Teste: `nssm version`.

## 2. Instalar o serviço

```powershell
$A = "C:\Horun\Horun-Agent"      # ajuste se a pasta for outra
nssm install HorunAgent "$A\.venv\Scripts\python.exe" "-m agent"
nssm set HorunAgent AppDirectory "$A"
nssm set HorunAgent DisplayName "Horun Agent"
nssm set HorunAgent Description "Agente Horun: leitura/gravação de arquivos do equipamento para o servidor Horun"
nssm set HorunAgent Start SERVICE_AUTO_START
# se cair, volta sozinho em 10 s (e não fica em loop rápido se algo estiver muito errado)
nssm set HorunAgent AppExit Default Restart
nssm set HorunAgent AppRestartDelay 10000
nssm set HorunAgent AppThrottle 30000
# saída do processo em arquivo, com rotação (o log principal continua sendo o agent.log)
nssm set HorunAgent AppStdout "$A\service-stdout.log"
nssm set HorunAgent AppStderr "$A\service-stderr.log"
nssm set HorunAgent AppRotateFiles 1
nssm set HorunAgent AppRotateBytes 1048576
```

### Com qual usuário o serviço roda

Por padrão o serviço roda como **Sistema Local**, que lê e grava nas pastas
locais (ex. `C:\Users\RE7S 65\Documents\RE7raw-data`) sem problema. Só troque
se o agente precisar de uma **unidade de rede ou pasta compartilhada** (o
Sistema Local não enxerga unidades mapeadas):

```powershell
nssm set HorunAgent ObjectName ".\NomeDoUsuario" "SenhaDoUsuario"
```

(use a conta do Windows que já abre o RockSeven/GeoWorks nesse PC — a senha
você digita aí, ela fica guardada pelo Windows no serviço.)

## 3. Ligar e conferir

```powershell
nssm start HorunAgent
nssm status HorunAgent                       # deve dizer SERVICE_RUNNING
Get-Content "$A\agent.log" -Tail 20          # "agente em execução — servidor=..."
```

Teste de verdade: no Horun, abra o PostRun do RE7S (lista os `.B00` via
agente) ou o Painel → sincronizar o carrossel. Depois, **reinicie o PC** e
confira que o serviço voltou sozinho (`nssm status HorunAgent`) sem ninguém
logar.

Também dá para ver/parar pelo `services.msc` → "Horun Agent".

## Atualizar (nova versão do agente)

```powershell
nssm stop HorunAgent
cd C:\Horun\Horun-Agent
git pull
.venv\Scripts\pip install -e .
nssm start HorunAgent
```

## Problemas comuns

| Sintoma | O que olhar |
|---|---|
| `SERVICE_PAUSED` ou fica reiniciando | `service-stderr.log` — normalmente `config.json` com erro ou caminho do Python errado |
| Rodando, mas o Horun diz que o agente não respondeu | `agent.log`: falha de rede/`server_url` errado (IP antigo?); `consulta de tarefas recusada (401)` = token revogado, gere um novo código de enrolamento |
| Não acha a pasta dos dados | o `roots` do `config.json` aponta para uma unidade de rede → ver "Com qual usuário o serviço roda" |

## Desinstalar

```powershell
nssm stop HorunAgent
nssm remove HorunAgent confirm
```
