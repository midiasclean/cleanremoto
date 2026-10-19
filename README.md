# Clean Remoto

Programa de **acesso à área de trabalho remota** — no estilo do TeamViewer /
AnyDesk, porém simples, aberto e feito para rodar **sobre a VPN** do kit
[`acessoexterno`](https://github.com/midiasclean/acessoexterno) (Tailscale ou
WireGuard).

Um único programa faz as duas pontas:

| Modo | O que faz |
|---|---|
| **Permitir que me acessem** (host) | Compartilha a tela deste computador e deixa outra pessoa controlar mouse e teclado. |
| **Acessar outro** (viewer) | Conecta no computador que permitiu acesso e o controla. |

```
   Quem controla (viewer)                        Quem é acessado (host)
 ┌────────────────────────┐                    ┌────────────────────────────┐
 │  vê a tela do host      │ ◄───── tela ────── │  captura a tela             │
 │  move mouse / digita    │ ────── mouse/tecla►│  injeta mouse/teclado       │
 │  (janela do programa)   │                    │  (janela "CONECTADO" visível)│
 └────────────────────────┘                    └────────────────────────────┘
         conexão direta pelo IP da VPN (ex.: 100.x.x.x do Tailscale)
```

## Funcionalidades (acesso integral)

- 🖥️ **Tela ao vivo** do computador acessado.
- 🖱️ **Mouse completo**: mover, clique esquerdo/direito/meio, arrastar, rolagem.
- ⌨️ **Teclado completo**: letras, acentos, símbolos, F1–F12, setas, e
  **atalhos com Ctrl/Alt/Shift** (Ctrl+C, Alt+Tab, etc.).
- 📋 **Área de transferência** (texto) sincronizada.
- 🔒 **Acesso consentido e visível**: só conecta com a senha que o host mostra;
  o host exibe uma janela "CONECTADO" com o botão **ENCERRAR SESSÃO** sempre à mão.

> Pensado para **suporte e trabalho remoto autorizado** (acessar seu próprio PC
> ou dar suporte a quem está do outro lado e vê a tela de consentimento).
> Não instala serviço escondido, não roda invisível e não contorna antivírus.

---

## Como usar (Windows, em 3 passos)

Precisa do **Python 3.9+** instalado (<https://www.python.org/downloads/> —
marque **"Add python.exe to PATH"** na instalação). Depois é só dois cliques:

1. **No computador que será acessado**, dê dois cliques em
   **`1-PERMITIR-ACESSO.bat`**. Abre uma janela mostrando o **IP** e a **senha**.
2. **No computador que vai controlar**, dê dois cliques em
   **`2-ACESSAR-OUTRO.bat`**, digite o **IP** e a **senha** do passo 1 e clique
   em **CONECTAR**.
3. Pronto: a tela do outro computador aparece e você já controla mouse e teclado.

Prefere um atalho só? **`INICIAR.bat`** abre o menu com as duas opções.

### Qual IP usar?

- **Com a VPN do `acessoexterno` (recomendado):** use o IP do **Tailscale**
  (começa com `100.`) — funciona de qualquer lugar, sem abrir porta no roteador.
  A janela do host já mostra esse IP primeiro.
- **Na mesma rede local (casa/escritório):** use o IP local (ex.: `192.168.x.x`).

A porta padrão é **7700**.

---

## Gerar um `.exe` (sem precisar Python na outra máquina)

Dê dois cliques em **`GERAR-EXE.bat`**. Ele cria `dist\CleanRemoto.exe`, um
arquivo único que você pode copiar para qualquer PC Windows e executar direto.

---

## Uso pela linha de comando (opcional)

```bash
# instalar dependencias (uma vez)
pip install -r requirements.txt

# permitir acesso a este computador (gera uma senha)
python cleanremoto.py host --senha 123456 --porta 7700

# acessar outro computador
python cleanremoto.py viewer 100.101.102.103 --senha 123456 --porta 7700

# sem argumentos: abre o menu grafico
python cleanremoto.py
```

Também funciona em **Linux** e **macOS** (Python + as dependências). No Linux o
host precisa de sessão gráfica X11; no Wayland a injeção de teclado/mouse pode
ser limitada.

---

## Segurança — leia antes de usar

- **Só acesse computadores que você tem autorização para acessar.** A pessoa do
  outro lado vê a janela "CONECTADO" e pode encerrar a qualquer momento.
- A senha **não trafega em texto puro**: o host manda um desafio aleatório e o
  viewer responde com um HMAC-SHA256 da senha. Ainda assim, **use por dentro da
  VPN** (Tailscale/WireGuard), que já criptografa todo o tráfego ponta a ponta.
- **Não exponha a porta 7700 direto na internet / no roteador.** Deixe o acesso
  só pela VPN. Em uma LAN confiável também é seguro.
- Use uma **senha forte** no modo host (a gerada automaticamente serve; se
  definir a sua, evite algo óbvio).
- Para cortar o acesso de alguém, encerre a sessão no host e/ou remova o
  dispositivo no painel do Tailscale.

### Por que isto não é um "programa espião"

Clean Remoto foi feito para acesso **consentido**: exige senha, mostra uma
janela bem visível enquanto alguém está conectado, não se instala como serviço
oculto, não se esconde de antivírus e não persiste sozinho. É a mesma categoria
de ferramenta do TeamViewer/AnyDesk/VNC — para suporte e para acessar o próprio
computador de longe.

---

## Arquivos

```
INICIAR.bat            menu: permitir acesso OU acessar outro
1-PERMITIR-ACESSO.bat  abre direto o modo host (compartilhar este PC)
2-ACESSAR-OUTRO.bat    abre direto o modo viewer (acessar outro PC)
GERAR-EXE.bat          cria dist\CleanRemoto.exe (nao precisa Python na outra maquina)
cleanremoto.py         o programa (host + viewer num arquivo so)
requirements.txt       dependencias (pillow, mss, pynput)
```

## Problemas comuns

**"Python nao encontrado"** — instale o Python e marque *Add to PATH*, ou use o
`CleanRemoto.exe` gerado pelo `GERAR-EXE.bat`.

**"Nao foi possivel conectar"** — confira se o host está com o
`1-PERMITIR-ACESSO.bat` aberto, se o IP/senha/porta estão certos e se os dois
computadores estão na mesma VPN (ou na mesma rede local). Em rede local, o
Firewall do Windows pode pedir para liberar o Python na primeira vez — clique em
**Permitir acesso**.

**Tela um pouco lenta ou "mole"** — é normal em internet mais fraca; o programa
já comprime a imagem e só reenvia quando a tela muda. Pela VPN e em rede local
fica bem fluido.

**Teclado com acento/símbolo errado** — o layout usado é o do computador que está
sendo acessado (host). Configure o mesmo layout nos dois lados se precisar.
