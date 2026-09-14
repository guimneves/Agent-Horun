# Instalando o agente como serviço do Windows

Usamos o [NSSM](https://nssm.cc/) — transforma qualquer programa comum
num serviço do Windows (inicia sozinho no boot, reinicia se cair, roda
sem ninguém logado), sem precisar escrever a parte de "serviço Windows"
em Python na mão.

## Pré-requisitos

1. Python 3.11+ instalado no PC do equipamento.
2. `nssm.exe` baixado (https://nssm.cc/download) e colocado em algum lugar
   do `PATH` (ex. `C:\Windows\System32`), ou anote o caminho completo pra
   usar nos comandos abaixo.
3. Este repositório clonado/copiado pro PC do equipamento (ex.
   `C:\Horun\Horun-Agent`), com um ambiente virtual já criado e as
   dependências instaladas:
   ```powershell
   cd C:\Horun\Horun-Agent
   python -m venv .venv
   .venv\Scripts\pip install -e .
   ```
4. `config.json` preenchido (copiado de `config.example.json`) com o
   `server_url` certo, os `roots` reais daquele equipamento, e o
   `enroll_code` fornecido pelo administrador do módulo.

## Instalar

```powershell
cd C:\Horun\Horun-Agent
nssm install HorunAgent "C:\Horun\Horun-Agent\.venv\Scripts\python.exe" "-m agent"
nssm set HorunAgent AppDirectory "C:\Horun\Horun-Agent"
nssm set HorunAgent AppStdout "C:\Horun\Horun-Agent\service-stdout.log"
nssm set HorunAgent AppStderr "C:\Horun\Horun-Agent\service-stderr.log"
nssm set HorunAgent Start SERVICE_AUTO_START
nssm start HorunAgent
```

Na primeira execução, o agente lê o `enroll_code` do `config.json`, troca
por um `device_token` permanente, e salva de volta no próprio
`config.json` — depois disso pode apagar o `enroll_code` do arquivo, ele
não é mais usado.

## Verificar

```powershell
nssm status HorunAgent
Get-Content C:\Horun\Horun-Agent\agent.log -Tail 30
```

## Desinstalar

```powershell
nssm stop HorunAgent
nssm remove HorunAgent confirm
```

## Atualizar (nova versão do agente)

```powershell
nssm stop HorunAgent
cd C:\Horun\Horun-Agent
git pull
.venv\Scripts\pip install -e .
nssm start HorunAgent
```
