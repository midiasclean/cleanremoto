# Ponte (relay) do Clean Remoto

Este é o **servidor-ponte** que faz o Clean Remoto funcionar pela **internet**
(como os servidores do AnyDesk). Cada computador se conecta aqui e registra o
seu código; quando um quer acessar o outro, a ponte liga os dois e repassa os
bytes. **A ponte não vê a tela, o teclado nem a senha** — só repassa dados
criptografados ponta a ponta.

Você só precisa subir isto **uma vez**. Depois é só colocar o endereço dela no
programa (campo "Servidor (ponte)") nos computadores.

---

## Opção A — Fly.io (recomendado, tem plano grátis)

1. Crie uma conta em <https://fly.io> e instale o `flyctl`:
   - Windows (PowerShell): `iwr https://fly.io/install.ps1 -useb | iex`
   - Linux/macOS: `curl -L https://fly.io/install.sh | sh`
2. Faça login: `fly auth login`
3. Entre nesta pasta `relay/` e **edite o `fly.toml`**: troque o `app` por um
   nome único seu (ex.: `cleanremoto-ponte-minhaempresa`).
4. Suba:
   ```bash
   fly launch --no-deploy --copy-config --name cleanremoto-ponte-minhaempresa
   fly deploy
   ```
   (se o `fly launch` perguntar algo, pode aceitar os padrões; não precisa de
   banco de dados nem Redis.)
5. O endereço será `https://SEU-APP.fly.dev`. No Clean Remoto, use com `wss://`:
   ```
   wss://SEU-APP.fly.dev
   ```

> A ponte fica **sempre ligada** (`min_machines_running = 1`). O consumo é
> mínimo (256 MB). Acompanhe o uso no painel do Fly.io.

---

## Opção B — Railway

1. Crie uma conta em <https://railway.app>.
2. **New Project → Deploy from GitHub repo** e aponte para este repositório,
   escolhendo a pasta `relay/` como raiz (Root Directory = `relay`), **ou**
   use o Dockerfile desta pasta.
3. O Railway detecta o `Dockerfile`, define a porta pela variável `PORT`
   automaticamente e gera um domínio público (em **Settings → Networking →
   Generate Domain**).
4. O endereço será algo como `https://SEU-APP.up.railway.app`. No Clean Remoto,
   use com `wss://`:
   ```
   wss://SEU-APP.up.railway.app
   ```

---

## Opção C — Qualquer VPS com IP público (avançado)

```bash
pip install -r requirements.txt
PORT=8765 python relay.py
```
Coloque atrás de um proxy com HTTPS (Caddy/Nginx) para ter `wss://`. Exemplo de
Caddy (TLS automático):
```
ponte.seudominio.com {
    reverse_proxy 127.0.0.1:8765
}
```
No Clean Remoto: `wss://ponte.seudominio.com`.

---

## Como testar se a ponte está no ar

Abra no navegador `https://SEU-APP.fly.dev/health` — deve responder
`Clean Remoto relay OK`.

## Colocar o endereço no programa

Em **cada** computador, abra o Clean Remoto, cole o endereço no campo
**"Servidor (ponte)"** (ex.: `wss://SEU-APP.fly.dev`) e clique em **Salvar**.
Pronto: o computador fica "Online" e já dá para acessar por código de qualquer
rede. Dica: dá para deixar o endereço fixo definindo a variável de ambiente
`CLEANREMOTO_RELAY` nas máquinas.
