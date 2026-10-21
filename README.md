# Clean Remoto

Programa de **acesso à área de trabalho remota** — um **único arquivo**, como
AnyDesk ou TeamViewer. Cada computador tem um **código**; para acessar outro,
você digita o código dele e o outro lado clica em **Permitir**. Funciona **de
qualquer lugar pela internet**, não só na rede local.

```
 ┌───────────────────────────── Clean Remoto ─────────────────────────────┐
 │  ESTE COMPUTADOR                      ACESSAR OUTRO COMPUTADOR           │
 │  Seu código:  284 459 657             Código:  [ 123 456 789 ]          │
 │  Online - pronto para ser acessado    Senha:   [ (só se exigir) ]       │
 │                                       [        CONECTAR        ]        │
 └─────────────────────────────────────────────────────────────────────────┘
          ▲ passe seu código                 ▲ digite o código do outro
                 ao conectar, o computador acessado clica em PERMITIR
```

## Como funciona o acesso pela internet

Redes de casa/empresa ficam atrás de NAT/CGNAT, então não dá para conectar
direto pela internet. Por isso o Clean Remoto usa uma **ponte (relay)** — igual
ao AnyDesk, que usa os servidores da própria AnyDesk. Cada computador se conecta
na ponte (saída pela porta 443/`wss`, sem abrir porta no roteador) e registra o
seu código; quando um quer acessar o outro, a ponte liga os dois e repassa os
bytes. **A ponte não vê a tela, o teclado nem a senha** — só repassa dados.

Você sobe a ponte **uma vez** (há guia pronto para Fly.io/Railway) — veja
[`relay/README.md`](relay/README.md).

## Baixar

- **Windows:** baixe o arquivo único **`CleanRemoto.exe`** na
  [página de releases](https://github.com/midiasclean/cleanremoto/releases/latest)
  (ou no site). Dois cliques — **não precisa instalar Python**.
- **Linux / macOS:** use o `cleanremoto.py` com Python (veja abaixo).

## Como usar

1. **Suba a ponte uma vez** (veja [`relay/README.md`](relay/README.md)) e anote
   o endereço, ex.: `wss://sua-ponte.fly.dev`.
2. Abra o **CleanRemoto.exe** nos dois computadores. Em cada um, cole o endereço
   da ponte no campo **“Servidor (ponte)”** e clique em **Salvar** (fica guardado).
   Cada computador mostra o **seu código** e fica **Online**.
3. No computador **que controla**: digite o **código do outro** em
   *“Acessar outro computador”* e clique em **CONECTAR**.
4. No computador **que será acessado**: clique em **PERMITIR** no aviso que
   aparece (ou ligue *“Permitir automaticamente”* + senha, para acesso sem
   ninguém presente).

A tela do outro computador abre e você já controla mouse e teclado.

## Funcionalidades (acesso integral)

- 🌐 **De qualquer lugar, por código**: conecta pela internet via a ponte, não
  só na rede local. Sem abrir portas no roteador.
- 🖥️ **Tela ao vivo** (captura rápida, compactada em JPEG, só reenvia quando muda).
- 🖱️ **Mouse completo** e ⌨️ **teclado completo** (inclui Ctrl/Alt/Shift, F1–F12, acentos).
- 📋 **Área de transferência** (texto) sincronizada.
- ✅ **Autorização ao vivo** (*Permitir / Recusar*), com opção de **permitir
  automaticamente** (exige senha). A senha nunca trafega em texto puro
  (HMAC-SHA256); a ponte não vê o conteúdo.

## Rodar pelo código (Linux / macOS / Windows com Python)

```bash
pip install -r requirements.txt
python cleanremoto.py                       # abre a janela (por codigo, via ponte)
# a ponte pode vir da variavel de ambiente:
CLEANREMOTO_RELAY=wss://sua-ponte.fly.dev python cleanremoto.py
# acesso direto por IP na mesma rede local (avancado):
python cleanremoto.py viewer 192.168.0.10 --senha 123456
```

O código de cada computador fica guardado em `config.json`
(`%APPDATA%\CleanRemoto\` no Windows, `~/.config/CleanRemoto/` no Linux/macOS).

## De onde vem o `.exe`

O `CleanRemoto.exe` é gerado automaticamente pelo **GitHub Actions**
(`.github/workflows/build-exe.yml`) num runner Windows, com PyInstaller
`--onefile` (um arquivo só), e publicado na Release **`app`** a cada mudança.

## Site de download (hospedagem na Vercel)

Repositório também é um **site estático** pronto para a Vercel. O `vercel.json`
usa `@vercel/static` para forçar deploy estático (sem isso a Vercel tentaria
rodar o Python). Importe o repo na Vercel e clique em **Deploy**.

> **Download público:** o link do `.exe` é a Release do GitHub. Se o
> repositório for **privado**, o download só abre logado — deixe o repositório
> **público** (Settings → General → Change visibility) para o download funcionar
> de qualquer rede.

## Arquivos

```
cleanremoto.py                    o programa (janela única, por codigo)
requirements.txt                  dependencias do cliente (pillow, mss, pynput, websockets)
relay/relay.py                    a ponte (servidor) - repassa bytes por codigo
relay/Dockerfile, fly.toml        deploy da ponte (Fly.io/Railway/VPS)
relay/README.md                   guia para subir a ponte
.github/workflows/build-exe.yml   compila o CleanRemoto.exe e publica na Release
index.html, vercel.json           site de download (Vercel)
```

## Segurança

- Use apenas em computadores que você tem **autorização** para acessar. A pessoa
  do outro lado clica em Permitir e vê o status; pode encerrar quando quiser.
- O acesso padrão exige o **Permitir ao vivo**. O modo automático exige **senha**
  (não exponha seu código + senha automática por aí).
- A ponte é **sua** e só repassa bytes criptografados ponta a ponta. Prefira
  `wss://` (TLS), que Fly.io/Railway já fornecem.
