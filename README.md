# Clean Remoto

Programa de **acesso à área de trabalho remota** — um **único arquivo**, como
AnyDesk ou TeamViewer. Abra o programa e, numa janela só, você já fica
disponível para ser acessado **e** pode acessar outro computador.

Feito para rodar **sobre a VPN** do kit
[`acessoexterno`](https://github.com/midiasclean/acessoexterno) (Tailscale /
WireGuard) ou em **rede local**.

```
 ┌───────────────────────────── Clean Remoto ─────────────────────────────┐
 │  ESTE COMPUTADOR                     ACESSAR OUTRO COMPUTADOR            │
 │  IP:    100.84.12.7                  IP:    [ 100.84.12.9 ]             │
 │  Senha: 429175   [Gerar nova]        Senha: [ •••••• ]                  │
 │  Status: Aguardando conexão...       [        CONECTAR        ]         │
 └─────────────────────────────────────────────────────────────────────────┘
          ▲ deixe aberto p/ ser acessado        ▲ conecta em outro PC
```

## Baixar

- **Windows:** baixe o arquivo único **`CleanRemoto.exe`** na
  [página de releases](https://github.com/midiasclean/cleanremoto/releases/latest)
  (ou no site). É só dar dois cliques — **não precisa instalar Python**.
- **Linux / macOS:** use o `cleanremoto.py` com Python (veja abaixo).

## Como usar

1. Abra o **CleanRemoto.exe** nos dois computadores.
2. No computador **que será acessado**: informe o **IP** e a **senha** que
   aparecem em *“Este computador”*.
3. No computador **que controla**: digite esse IP e senha em
   *“Acessar outro computador”* e clique em **CONECTAR**.
4. De volta no computador **que será acessado**: clique em **PERMITIR** no
   aviso que aparece (ou deixe *“Permitir automaticamente”* ligado para dispensar
   esse passo).

A tela do outro computador abre e você já controla mouse e teclado.

### Qual IP usar?

- **Com a VPN do `acessoexterno` (recomendado):** use o IP do **Tailscale**
  (começa com `100.`) — funciona de qualquer lugar, sem abrir porta no roteador.
- **Na mesma rede local:** use o IP local (ex.: `192.168.x.x`).

Porta padrão: **7700**.

## Funcionalidades (acesso integral)

- 🖥️ **Tela ao vivo** (captura rápida, compactada em JPEG, só reenvia quando muda).
- 🖱️ **Mouse completo**: mover, clicar (esq/dir/meio), arrastar, rolagem.
- ⌨️ **Teclado completo**: letras, acentos, símbolos, F1–F12, setas e atalhos
  com Ctrl/Alt/Shift (Ctrl+C, Alt+Tab…).
- 📋 **Área de transferência** (texto) sincronizada.
- ✅ **Autorização ao vivo**: além da senha, ao conectar aparece no computador
  acessado o aviso *“Fulano quer acessar — Permitir / Recusar”*. Sem autorizar,
  a sessão não começa (recusa sozinho em 60s). Para acessar o **próprio PC**
  sem ninguém presente, marque *“Permitir automaticamente, só com a senha”*.
- 🔒 **Acesso consentido e visível**: a senha não trafega em texto puro
  (desafio/resposta HMAC-SHA256); o status e o botão **ENCERRAR SESSÃO** ficam
  sempre visíveis.

> Para **suporte e acesso ao próprio computador**, com autorização. Não instala
> serviço escondido, não roda invisível e não contorna antivírus.

## Rodar pelo código (Linux / macOS / Windows com Python)

```bash
pip install -r requirements.txt
python cleanremoto.py            # abre a janela única
# opcionais:
python cleanremoto.py host --senha 123456      # ja pronto para ser acessado
python cleanremoto.py viewer 100.84.12.9 --senha 123456   # acessar outro direto
```

## De onde vem o `.exe`

O `CleanRemoto.exe` é gerado automaticamente pelo **GitHub Actions**
(`.github/workflows/build-exe.yml`) num runner Windows, com PyInstaller
`--onefile` (um arquivo só), e publicado na Release **`app`** a cada mudança no
programa. O site de download sempre aponta para a versão mais recente.

Para gerar manualmente num Windows com Python:

```bat
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --onefile --windowed --name CleanRemoto ^
  --collect-all pynput --collect-all mss cleanremoto.py
:: resultado em dist\CleanRemoto.exe
```

## Site de download (hospedagem na Vercel)

Este repositório também é um **site estático** pronto para a
[Vercel](https://vercel.com).

1. Entre na Vercel com o GitHub → **Add New → Project** → repositório `cleanremoto`.
2. Pode deixar tudo como está (o `vercel.json` cuida da configuração) → **Deploy**.

O `vercel.json` usa o builder `@vercel/static`, que **força o deploy 100%
estático** e desliga a autodetecção da Vercel — assim o `cleanremoto.py` é
tratado como arquivo para download, e não como app para rodar no servidor
(sem isso o deploy falharia com `PYTHON_ENTRYPOINT_NOT_FOUND`).

## Arquivos

```
cleanremoto.py                    o programa (janela única: host + viewer)
requirements.txt                  dependencias (pillow, mss, pynput)
.github/workflows/build-exe.yml   compila o CleanRemoto.exe (arquivo unico) e publica na Release
index.html                        site de download (para a Vercel)
vercel.json                       configuracao da Vercel (deploy estatico + cabecalhos)
favicon.svg                       icone do site
```

## Segurança

- Use apenas em computadores que você tem **autorização** para acessar. A pessoa
  do outro lado vê o status da sessão e pode encerrá-la a qualquer momento.
- A senha não trafega em texto puro, mas **use por dentro de uma VPN**
  (Tailscale/WireGuard), que criptografa todo o tráfego. **Não exponha a porta
  7700 direto na internet.**
- Use uma **senha forte** (a gerada automaticamente serve) e troque com
  **Gerar nova** quando quiser.
