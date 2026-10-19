#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clean Remoto - acesso a area de trabalho remota.

UM UNICO programa (como AnyDesk / TeamViewer). Ao abrir, uma janela so ja
mostra as duas coisas:

  * ESTE COMPUTADOR  -> seu endereco (IP) e senha. Deixe aberto para que
                        outra pessoa consiga acessar este computador.
  * ACESSAR OUTRO    -> digite o IP e a senha de outro computador e conecte.

Feito para rodar SOBRE a VPN do kit "acesso externo" (Tailscale / WireGuard):
os dois computadores ja se enxergam pelo IP da VPN, entao nao precisa abrir
porta no roteador nem de servidor intermediario. Em rede local tambem funciona.

Seguranca (acesso consentido e visivel):
  * So aceita conexao com a SENHA mostrada na tela.
  * A senha nunca trafega em texto puro: desafio/resposta com HMAC-SHA256.
  * Enquanto alguem esta conectado, aparece o status e o botao ENCERRAR SESSAO.
  * Nada de instalacao escondida, servico furtivo ou operacao invisivel.

Requisitos (so para rodar pelo codigo): Python 3.9+ e os pacotes de
requirements.txt. O executavel unico (CleanRemoto.exe) ja vem com tudo.
"""

import argparse
import hashlib
import hmac
import io
import json
import queue
import secrets
import socket
import struct
import threading
import time

# ---------------------------------------------------------------------------
# Dependencias externas (carregadas com mensagem amigavel se faltarem)
# ---------------------------------------------------------------------------
_MISSING = []
try:
    from PIL import Image, ImageTk  # noqa: F401
except Exception:  # pragma: no cover
    _MISSING.append("pillow")
try:
    import mss  # captura de tela rapida
except Exception:  # pragma: no cover
    mss = None  # usa fallback do PIL mais adiante
try:
    from pynput import mouse as _pynput_mouse
    from pynput import keyboard as _pynput_keyboard
except Exception:  # pragma: no cover
    _pynput_mouse = None
    _pynput_keyboard = None

import tkinter as tk
from tkinter import messagebox, scrolledtext


# ---------------------------------------------------------------------------
# Protocolo de rede
# ---------------------------------------------------------------------------
# Cada mensagem: 1 byte de tipo + 4 bytes (uint32 big-endian) de tamanho +
# o conteudo. Mensagens de controle usam JSON; quadros de tela usam JPEG.
PORT_PADRAO = 7700
VERSAO = 1

MSG_HELLO = 1      # host -> viewer : {nonce, versao}
MSG_AUTH = 2       # viewer -> host : {resposta, nome}
MSG_AUTH_OK = 3    # host -> viewer : {largura, altura}
MSG_AUTH_FAIL = 4  # host -> viewer : {motivo}
MSG_FRAME = 5      # host -> viewer : bytes JPEG
MSG_INPUT = 6      # viewer -> host : {evento de mouse/teclado}
MSG_CLIP = 7       # ambos          : texto da area de transferencia (utf-8)
MSG_PING = 8       # ambos          : mantem a conexao viva

_HDR = struct.Struct("!BI")


def enviar_msg(sock, tipo, dados=b""):
    """Envia uma mensagem enquadrada. `dados` pode ser bytes, str ou dict."""
    if isinstance(dados, dict):
        dados = json.dumps(dados).encode("utf-8")
    elif isinstance(dados, str):
        dados = dados.encode("utf-8")
    sock.sendall(_HDR.pack(tipo, len(dados)) + dados)


def _recv_exato(sock, n):
    """Le exatamente n bytes ou levanta ConnectionError."""
    buf = bytearray()
    while len(buf) < n:
        parte = sock.recv(n - len(buf))
        if not parte:
            raise ConnectionError("conexao encerrada")
        buf.extend(parte)
    return bytes(buf)


def receber_msg(sock):
    """Le uma mensagem enquadrada. Retorna (tipo, bytes)."""
    cabecalho = _recv_exato(sock, _HDR.size)
    tipo, tam = _HDR.unpack(cabecalho)
    if tam > 64 * 1024 * 1024:
        raise ConnectionError("mensagem grande demais")
    return tipo, _recv_exato(sock, tam) if tam else b""


def resposta_hmac(senha, nonce):
    """Resposta ao desafio: HMAC-SHA256(senha, nonce) em hexadecimal."""
    return hmac.new(senha.encode("utf-8"), nonce.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def ips_locais():
    """Lista enderecos IPv4 uteis deste computador (inclui o da VPN)."""
    enderecos = set()
    try:
        nome = socket.gethostname()
        for info in socket.getaddrinfo(nome, None, socket.AF_INET):
            enderecos.add(info[4][0])
    except Exception:
        pass
    try:  # IP usado para "sair" para a internet
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        enderecos.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    enderecos.discard("127.0.0.1")
    # IPs da VPN Tailscale comecam com 100.  -> mostra primeiro
    return sorted(enderecos, key=lambda ip: (not ip.startswith("100."), ip))


# ===========================================================================
# LADO HOST  -  compartilha a tela e injeta mouse/teclado
# ===========================================================================
class CapturadorTela:
    """Captura a tela principal como imagem RGB."""

    def __init__(self):
        self._sct = mss.mss() if mss else None

    def info(self):
        if self._sct:
            m = self._sct.monitors[1]  # monitor principal
            return m["width"], m["height"]
        from PIL import ImageGrab
        img = ImageGrab.grab()
        return img.size

    def capturar(self):
        if self._sct:
            m = self._sct.monitors[1]
            bruto = self._sct.grab(m)
            return Image.frombytes("RGB", bruto.size, bruto.bgra, "raw", "BGRX")
        from PIL import ImageGrab
        return ImageGrab.grab().convert("RGB")

    def fechar(self):
        if self._sct:
            self._sct.close()


class ControleEntrada:
    """Injeta eventos de mouse e teclado no computador host (via pynput)."""

    def __init__(self):
        self.mouse = _pynput_mouse.Controller()
        self.teclado = _pynput_keyboard.Controller()
        B = _pynput_mouse.Button
        self._BOTOES = {"left": B.left, "right": B.right, "middle": B.middle}
        K = _pynput_keyboard.Key
        self._ESPECIAIS = {
            "Return": K.enter, "KP_Enter": K.enter, "BackSpace": K.backspace,
            "Tab": K.tab, "Escape": K.esc, "space": K.space, "Delete": K.delete,
            "Home": K.home, "End": K.end, "Prior": K.page_up, "Next": K.page_down,
            "Up": K.up, "Down": K.down, "Left": K.left, "Right": K.right,
            "Shift_L": K.shift, "Shift_R": K.shift_r,
            "Control_L": K.ctrl, "Control_R": K.ctrl_r,
            "Alt_L": K.alt, "Alt_R": getattr(K, "alt_gr", K.alt_r),
            "Super_L": K.cmd, "Super_R": K.cmd, "Win_L": K.cmd, "Win_R": K.cmd,
            "Caps_Lock": K.caps_lock, "Insert": K.insert,
            "Print": K.print_screen, "Menu": K.menu, "Pause": getattr(K, "pause", K.esc),
        }
        for i in range(1, 13):
            self._ESPECIAIS["F%d" % i] = getattr(K, "f%d" % i)

    def aplicar(self, ev):
        t = ev.get("t")
        if t == "move":
            self.mouse.position = (ev["x"], ev["y"])
        elif t in ("down", "up"):
            self.mouse.position = (ev["x"], ev["y"])
            btn = self._BOTOES.get(ev.get("btn"), _pynput_mouse.Button.left)
            (self.mouse.press if t == "down" else self.mouse.release)(btn)
        elif t == "scroll":
            self.mouse.scroll(ev.get("dx", 0), ev.get("dy", 0))
        elif t == "type":
            self.teclado.type(ev.get("text", ""))
        elif t in ("kdown", "kup"):
            tecla = self._traduzir_tecla(ev)
            if tecla is None:
                return
            (self.teclado.press if t == "kdown" else self.teclado.release)(tecla)

    def _traduzir_tecla(self, ev):
        keysym = ev.get("key", "")
        if keysym in self._ESPECIAIS:
            return self._ESPECIAIS[keysym]
        char = ev.get("char") or ""
        if len(keysym) == 1:
            return _pynput_keyboard.KeyCode.from_char(keysym.lower())
        if char:
            return _pynput_keyboard.KeyCode.from_char(char)
        return None


class ServidorHost:
    """Aceita UMA conexao por vez e serve tela + entrada."""

    def __init__(self, senha, porta=PORT_PADRAO, fps=12, qualidade=55,
                 ao_conectar=None, ao_desconectar=None, ao_log=None,
                 ao_clipboard=None):
        self.senha = senha
        self.porta = porta
        self.intervalo = 1.0 / max(1, fps)
        self.qualidade = qualidade
        self.ao_conectar = ao_conectar or (lambda nome, ip: None)
        self.ao_desconectar = ao_desconectar or (lambda: None)
        self.ao_log = ao_log or (lambda m: None)
        self.ao_clipboard = ao_clipboard or (lambda texto: None)
        self._parar = threading.Event()
        self._cliente = None
        self._srv = None

    def iniciar(self):
        threading.Thread(target=self._loop_aceitar, daemon=True).start()

    def parar(self):
        self._parar.set()
        for s in (self._cliente, self._srv):
            try:
                if s:
                    s.close()
            except Exception:
                pass

    def encerrar_sessao(self):
        """Derruba so a conexao atual, mas continua escutando."""
        if self._cliente:
            try:
                self._cliente.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self._cliente.close()
            except Exception:
                pass

    def _loop_aceitar(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._srv.bind(("0.0.0.0", self.porta))
            self._srv.listen(1)
        except Exception as e:
            self.ao_log("ERRO ao escutar na porta %d: %s" % (self.porta, e))
            return
        self.ao_log("Escutando na porta %d. Aguardando conexao..." % self.porta)
        while not self._parar.is_set():
            try:
                cli, addr = self._srv.accept()
            except Exception:
                break
            self.ao_log("Conexao recebida de %s" % (addr[0],))
            try:
                self._atender(cli, addr)
            except (ConnectionError, OSError) as e:
                self.ao_log("Sessao encerrada: %s" % e)
            finally:
                try:
                    cli.close()
                except Exception:
                    pass
                self._cliente = None
                self.ao_desconectar()

    def _atender(self, cli, addr):
        cli.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # 1) desafio/resposta
        nonce = secrets.token_hex(16)
        enviar_msg(cli, MSG_HELLO, {"nonce": nonce, "versao": VERSAO})
        tipo, dados = receber_msg(cli)
        if tipo != MSG_AUTH:
            enviar_msg(cli, MSG_AUTH_FAIL, {"motivo": "handshake invalido"})
            return
        info = json.loads(dados.decode("utf-8"))
        esperado = resposta_hmac(self.senha, nonce)
        if not hmac.compare_digest(esperado, info.get("resposta", "")):
            enviar_msg(cli, MSG_AUTH_FAIL, {"motivo": "senha incorreta"})
            self.ao_log("Senha incorreta de %s - conexao recusada." % addr[0])
            time.sleep(1)
            return
        nome = info.get("nome") or addr[0]

        cap = CapturadorTela()
        larg, alt = cap.info()
        enviar_msg(cli, MSG_AUTH_OK, {"largura": larg, "altura": alt})
        self._cliente = cli
        self.ao_conectar(nome, addr[0])
        self.ao_log("%s entrou. Tela %dx%d sendo compartilhada." % (nome, larg, alt))

        # 2) thread que recebe entrada (mouse/teclado/clipboard) do viewer
        controle = ControleEntrada() if _pynput_mouse else None
        threading.Thread(target=self._receber_entrada, args=(cli, controle),
                          daemon=True).start()

        # 3) laco principal: envia quadros de tela
        ultimo_hash = None
        try:
            while not self._parar.is_set() and self._cliente is cli:
                t0 = time.time()
                img = cap.capturar()
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=self.qualidade)
                quadro = buf.getvalue()
                h = hashlib.md5(quadro).digest()
                if h != ultimo_hash:  # nao reenvia tela parada
                    enviar_msg(cli, MSG_FRAME, quadro)
                    ultimo_hash = h
                descanso = self.intervalo - (time.time() - t0)
                if descanso > 0:
                    time.sleep(descanso)
        finally:
            cap.fechar()

    def _receber_entrada(self, cli, controle):
        try:
            while self._cliente is cli:
                tipo, dados = receber_msg(cli)
                if tipo == MSG_INPUT and controle:
                    try:
                        controle.aplicar(json.loads(dados.decode("utf-8")))
                    except Exception:
                        pass
                elif tipo == MSG_CLIP:
                    try:
                        self.ao_clipboard(dados.decode("utf-8", "ignore"))
                    except Exception:
                        pass
                elif tipo == MSG_PING:
                    pass
        except (ConnectionError, OSError):
            pass


# ===========================================================================
# LADO VIEWER  -  mostra a tela do host e envia mouse/teclado
# ===========================================================================
class JanelaViewer:
    """Janela que exibe e controla o computador remoto.

    master=None  -> cria a propria janela principal (uso por linha de comando).
    master=root  -> abre como janela secundaria (Toplevel) do programa unico.
    """

    def __init__(self, host, porta, senha, nome="Clean Remoto", master=None):
        self.host = host
        self.porta = porta
        self.senha = senha
        self.nome = nome
        self.sock = None
        self.larg_remota = 0
        self.alt_remota = 0
        self._fila = queue.Queue(maxsize=2)
        self._parar = threading.Event()
        self._img_tk = None
        self._mods = set()  # modificadores pressionados (ctrl/alt)
        self._standalone = master is None

        self.win = tk.Tk() if master is None else tk.Toplevel(master)
        self.win.title("Clean Remoto - conectando a %s..." % host)
        self.win.configure(bg="#111")
        self.win.geometry("1100x700")
        self.canvas = tk.Canvas(self.win, bg="#111", highlightthickness=0,
                                cursor="arrow")
        self.canvas.pack(fill="both", expand=True)
        self._item = self.canvas.create_image(0, 0, anchor="nw")
        self._area = (0, 0, 1, 1)  # x0,y0,larg,alt da imagem desenhada

        self._ligar_eventos()
        self.win.protocol("WM_DELETE_WINDOW", self.fechar)

    # -- conexao -----------------------------------------------------------
    def conectar(self):
        try:
            self.sock = socket.create_connection((self.host, self.porta), timeout=10)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            tipo, dados = receber_msg(self.sock)
            if tipo != MSG_HELLO:
                raise ConnectionError("resposta inesperada do host")
            nonce = json.loads(dados.decode("utf-8"))["nonce"]
            enviar_msg(self.sock, MSG_AUTH,
                       {"resposta": resposta_hmac(self.senha, nonce), "nome": self.nome})
            tipo, dados = receber_msg(self.sock)
            if tipo == MSG_AUTH_FAIL:
                motivo = json.loads(dados.decode("utf-8")).get("motivo", "recusado")
                raise ConnectionError("Acesso negado: %s" % motivo)
            if tipo != MSG_AUTH_OK:
                raise ConnectionError("handshake falhou")
            info = json.loads(dados.decode("utf-8"))
            self.larg_remota = info["largura"]
            self.alt_remota = info["altura"]
        except Exception as e:
            messagebox.showerror("Clean Remoto", "Nao foi possivel conectar:\n%s" % e)
            self.fechar()
            return False
        self.win.title("Clean Remoto - %s (%dx%d)" %
                       (self.host, self.larg_remota, self.alt_remota))
        threading.Thread(target=self._loop_receber, daemon=True).start()
        self.win.after(30, self._atualizar_tela)
        self.canvas.focus_set()
        return True

    def _loop_receber(self):
        try:
            while not self._parar.is_set():
                tipo, dados = receber_msg(self.sock)
                if tipo == MSG_FRAME:
                    try:
                        img = Image.open(io.BytesIO(dados))
                        img.load()
                    except Exception:
                        continue
                    if self._fila.full():
                        try:
                            self._fila.get_nowait()
                        except queue.Empty:
                            pass
                    self._fila.put(img)
                elif tipo == MSG_CLIP:
                    self._receber_clip(dados.decode("utf-8", "ignore"))
        except (ConnectionError, OSError):
            pass
        finally:
            self._parar.set()
            try:
                self.win.after(0, lambda: self.win.title("Clean Remoto - DESCONECTADO"))
            except Exception:
                pass

    def _atualizar_tela(self):
        if self._parar.is_set():
            return
        img = None
        try:
            while True:  # pega o quadro mais recente
                img = self._fila.get_nowait()
        except queue.Empty:
            pass
        if img is not None:
            cw = max(1, self.canvas.winfo_width())
            ch = max(1, self.canvas.winfo_height())
            escala = min(cw / img.width, ch / img.height)
            nl, na = max(1, int(img.width * escala)), max(1, int(img.height * escala))
            x0, y0 = (cw - nl) // 2, (ch - na) // 2
            self._area = (x0, y0, nl, na)
            vis = img.resize((nl, na), Image.BILINEAR)
            self._img_tk = ImageTk.PhotoImage(vis)
            self.canvas.itemconfig(self._item, image=self._img_tk)
            self.canvas.coords(self._item, x0, y0)
        self.win.after(30, self._atualizar_tela)

    # -- mapeamento de coordenadas ----------------------------------------
    def _para_remoto(self, ex, ey):
        x0, y0, nl, na = self._area
        rx = (ex - x0) / nl * self.larg_remota
        ry = (ey - y0) / na * self.alt_remota
        rx = min(max(rx, 0), self.larg_remota - 1)
        ry = min(max(ry, 0), self.alt_remota - 1)
        return int(rx), int(ry)

    def _enviar(self, ev):
        if self._parar.is_set() or not self.sock:
            return
        try:
            enviar_msg(self.sock, MSG_INPUT, ev)
        except (ConnectionError, OSError):
            self._parar.set()

    # -- eventos de mouse e teclado ---------------------------------------
    def _ligar_eventos(self):
        c = self.canvas
        c.bind("<Motion>", self._mouse_move)
        c.bind("<ButtonPress-1>", lambda e: self._mouse_btn(e, "left", True))
        c.bind("<ButtonRelease-1>", lambda e: self._mouse_btn(e, "left", False))
        c.bind("<ButtonPress-3>", lambda e: self._mouse_btn(e, "right", True))
        c.bind("<ButtonRelease-3>", lambda e: self._mouse_btn(e, "right", False))
        c.bind("<ButtonPress-2>", lambda e: self._mouse_btn(e, "middle", True))
        c.bind("<ButtonRelease-2>", lambda e: self._mouse_btn(e, "middle", False))
        c.bind("<B1-Motion>", self._mouse_move)
        c.bind("<B3-Motion>", self._mouse_move)
        c.bind("<MouseWheel>", self._scroll)            # Windows / macOS
        c.bind("<Button-4>", lambda e: self._scroll_xy(e, 0, 1))   # Linux
        c.bind("<Button-5>", lambda e: self._scroll_xy(e, 0, -1))  # Linux
        self.win.bind("<KeyPress>", self._key_press)
        self.win.bind("<KeyRelease>", self._key_release)
        c.focus_set()

    def _mouse_move(self, e):
        x, y = self._para_remoto(e.x, e.y)
        self._enviar({"t": "move", "x": x, "y": y})

    def _mouse_btn(self, e, btn, pressed):
        self.canvas.focus_set()
        x, y = self._para_remoto(e.x, e.y)
        self._enviar({"t": "down" if pressed else "up", "btn": btn, "x": x, "y": y})

    def _scroll(self, e):
        self._scroll_xy(e, 0, 1 if e.delta > 0 else -1)

    def _scroll_xy(self, e, dx, dy):
        self._enviar({"t": "scroll", "dx": dx, "dy": dy})

    _MODS = {"Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L", "Shift_R",
             "Super_L", "Super_R", "Win_L", "Win_R"}

    def _key_press(self, e):
        ks = e.keysym
        if ks in self._MODS:
            if ks.startswith(("Control", "Alt")):
                self._mods.add(ks)
            self._enviar({"t": "kdown", "key": ks, "char": e.char})
            return
        # com Ctrl/Alt pressionado -> manda tecla crua (atalhos: Ctrl+C etc.)
        if self._mods:
            self._enviar({"t": "kdown", "key": ks, "char": e.char})
            self._enviar({"t": "kup", "key": ks, "char": e.char})
            return
        # caractere imprimivel normal -> digita (resolve shift, acentos, simbolos)
        if e.char and e.char.isprintable():
            self._enviar({"t": "type", "text": e.char})
            return
        # teclas especiais (setas, F1-F12, Enter, Backspace, Delete...)
        self._enviar({"t": "kdown", "key": ks, "char": e.char})
        self._enviar({"t": "kup", "key": ks, "char": e.char})

    def _key_release(self, e):
        ks = e.keysym
        if ks in self._MODS:
            self._mods.discard(ks)
            self._enviar({"t": "kup", "key": ks, "char": e.char})

    # -- area de transferencia --------------------------------------------
    def _receber_clip(self, texto):
        def aplicar():
            try:
                self.win.clipboard_clear()
                self.win.clipboard_append(texto)
            except Exception:
                pass
        try:
            self.win.after(0, aplicar)
        except Exception:
            pass

    # -- ciclo de vida -----------------------------------------------------
    def fechar(self):
        self._parar.set()
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass

    def abrir(self):
        """Conecta usando o mainloop ja existente (janela secundaria)."""
        self.conectar()

    def rodar(self):
        """Conecta e roda o proprio mainloop (uso por linha de comando)."""
        if self.conectar():
            self.win.mainloop()


# ===========================================================================
# JANELA PRINCIPAL  -  tudo em um so lugar (estilo AnyDesk)
# ===========================================================================
BG = "#0f1b2d"; CARD = "#16263d"; AZUL = "#5db0ff"; VERDE = "#9fffa6"
MUTED = "#9fc0e8"


class JanelaPrincipal:
    def __init__(self, senha=None, porta=PORT_PADRAO):
        self.porta = porta
        self.servidor = None
        self.root = tk.Tk()
        self.root.title("Clean Remoto")
        self.root.configure(bg=BG)
        self.root.geometry("520x620")
        self.root.minsize(480, 560)

        tk.Label(self.root, text="CLEAN REMOTO", bg=BG, fg=AZUL,
                 font=("Segoe UI", 20, "bold")).pack(pady=(16, 0))
        tk.Label(self.root, text="Acesso a area de trabalho remota", bg=BG,
                 fg=MUTED, font=("Segoe UI", 10)).pack(pady=(0, 10))

        # ---- ESTE COMPUTADOR (host) -------------------------------------
        card1 = tk.Frame(self.root, bg=CARD)
        card1.pack(fill="x", padx=16, pady=6)
        tk.Label(card1, text="ESTE COMPUTADOR", bg=CARD, fg=VERDE,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=14, pady=(12, 0))
        tk.Label(card1, text="Deixe aberto para que possam acessar este computador.",
                 bg=CARD, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=14)

        tk.Label(card1, text="Endereco (IP):", bg=CARD, fg=MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=14, pady=(8, 0))
        ips = ips_locais() or ["(descubra o IP desta maquina)"]
        self.lbl_ip = tk.Label(card1, text="   " + "\n   ".join(ips), bg=CARD,
                               fg="#ffffff", justify="left",
                               font=("Consolas", 12, "bold"))
        self.lbl_ip.pack(anchor="w", padx=14)

        linha = tk.Frame(card1, bg=CARD)
        linha.pack(fill="x", padx=14, pady=(8, 0))
        tk.Label(linha, text="Senha:", bg=CARD, fg=MUTED,
                 font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w")
        self.e_senha_host = tk.Entry(linha, font=("Consolas", 16, "bold"), width=10,
                                     justify="center", fg="#0f1b2d")
        self.e_senha_host.grid(row=0, column=1, padx=6)
        self.e_senha_host.insert(0, senha or self._gerar_senha())
        tk.Button(linha, text="Gerar nova", command=self._nova_senha,
                  bg="#1e5f8f", fg="white", relief="flat",
                  font=("Segoe UI", 9, "bold")).grid(row=0, column=2, padx=3)
        tk.Label(linha, text="Porta:", bg=CARD, fg=MUTED,
                 font=("Segoe UI", 9)).grid(row=0, column=3, sticky="e", padx=(8, 0))
        self.e_porta_host = tk.Entry(linha, font=("Consolas", 11), width=6,
                                     justify="center", fg="#0f1b2d")
        self.e_porta_host.grid(row=0, column=4, padx=4)
        self.e_porta_host.insert(0, str(porta))
        tk.Button(linha, text="Aplicar", command=self._iniciar_servidor,
                  bg="#1e6f3f", fg="white", relief="flat",
                  font=("Segoe UI", 9, "bold")).grid(row=0, column=5, padx=3)

        self.lbl_status = tk.Label(card1, text="Iniciando...", bg=CARD,
                                   fg="#ffd27f", font=("Segoe UI", 11, "bold"))
        self.lbl_status.pack(anchor="w", padx=14, pady=(10, 0))
        self.btn_encerrar = tk.Button(card1, text="ENCERRAR SESSAO",
                                      command=self._encerrar, state="disabled",
                                      bg="#c0392b", fg="white", relief="flat",
                                      font=("Segoe UI", 10, "bold"))
        self.btn_encerrar.pack(anchor="w", padx=14, pady=(6, 12))

        # ---- ACESSAR OUTRO (viewer) -------------------------------------
        card2 = tk.Frame(self.root, bg=CARD)
        card2.pack(fill="x", padx=16, pady=6)
        tk.Label(card2, text="ACESSAR OUTRO COMPUTADOR", bg=CARD, fg=AZUL,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=14, pady=(12, 0))
        g = tk.Frame(card2, bg=CARD)
        g.pack(fill="x", padx=14, pady=(6, 12))
        tk.Label(g, text="IP:", bg=CARD, fg=MUTED).grid(row=0, column=0, sticky="e", pady=3)
        self.e_ip = tk.Entry(g, font=("Consolas", 12), width=20)
        self.e_ip.grid(row=0, column=1, padx=6, pady=3, sticky="w")
        tk.Label(g, text="Senha:", bg=CARD, fg=MUTED).grid(row=1, column=0, sticky="e", pady=3)
        self.e_senha = tk.Entry(g, font=("Consolas", 12), width=20)
        self.e_senha.grid(row=1, column=1, padx=6, pady=3, sticky="w")
        tk.Label(g, text="Porta:", bg=CARD, fg=MUTED).grid(row=2, column=0, sticky="e", pady=3)
        self.e_porta = tk.Entry(g, font=("Consolas", 12), width=20)
        self.e_porta.insert(0, str(PORT_PADRAO))
        self.e_porta.grid(row=2, column=1, padx=6, pady=3, sticky="w")
        self.btn_conectar = tk.Button(g, text="CONECTAR", command=self._conectar,
                                      bg="#1e6f3f", fg="white", relief="flat",
                                      font=("Segoe UI", 11, "bold"), padx=16, pady=4)
        self.btn_conectar.grid(row=3, column=0, columnspan=2, pady=(8, 0))
        self.e_ip.bind("<Return>", lambda e: self._conectar())
        self.e_senha.bind("<Return>", lambda e: self._conectar())

        # ---- log --------------------------------------------------------
        self.log = scrolledtext.ScrolledText(self.root, height=5, bg="#0a1420",
                                             fg="#8fb6e0", font=("Consolas", 8),
                                             relief="flat")
        self.log.pack(fill="both", expand=True, padx=16, pady=(6, 14))

        self.root.protocol("WM_DELETE_WINDOW", self._sair)

    # -- senha / servidor --------------------------------------------------
    @staticmethod
    def _gerar_senha():
        return "%06d" % secrets.randbelow(1000000)

    def _nova_senha(self):
        self.e_senha_host.delete(0, "end")
        self.e_senha_host.insert(0, self._gerar_senha())
        self._iniciar_servidor()

    def _iniciar_servidor(self):
        if self.servidor:
            self.servidor.parar()
            self.servidor = None
        senha = self.e_senha_host.get().strip() or self._gerar_senha()
        self.e_senha_host.delete(0, "end")
        self.e_senha_host.insert(0, senha)
        try:
            porta = int(self.e_porta_host.get().strip() or PORT_PADRAO)
        except ValueError:
            porta = PORT_PADRAO
            self.e_porta_host.delete(0, "end")
            self.e_porta_host.insert(0, str(porta))
        self.servidor = ServidorHost(
            senha, porta,
            ao_conectar=self._ao_conectar, ao_desconectar=self._ao_desconectar,
            ao_log=self._log, ao_clipboard=self._set_clip)
        self.servidor.iniciar()
        self.lbl_status.config(text="Aguardando conexao...", fg="#ffd27f")
        self.btn_encerrar.config(state="disabled")

    def _set_clip(self, texto):
        def ap():
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(texto)
            except Exception:
                pass
        try:
            self.root.after(0, ap)
        except Exception:
            pass

    def _ao_conectar(self, nome, ip):
        def ui():
            self.lbl_status.config(text="CONECTADO: %s (%s)" % (nome, ip), fg=VERDE)
            self.btn_encerrar.config(state="normal")
            try:
                self.root.attributes("-topmost", True)
                self.root.after(1500, lambda: self.root.attributes("-topmost", False))
            except Exception:
                pass
        self.root.after(0, ui)

    def _ao_desconectar(self):
        self.root.after(0, lambda: (
            self.lbl_status.config(text="Aguardando conexao...", fg="#ffd27f"),
            self.btn_encerrar.config(state="disabled")))

    def _encerrar(self):
        if self.servidor:
            self.servidor.encerrar_sessao()
        self._log("Sessao encerrada por voce.")

    # -- viewer ------------------------------------------------------------
    def _conectar(self):
        if not _avisar_dependencias(precisa_entrada=False):
            return
        ip = self.e_ip.get().strip()
        senha = self.e_senha.get().strip()
        if not ip or not senha:
            messagebox.showwarning("Clean Remoto", "Informe o IP e a senha.")
            return
        try:
            porta = int(self.e_porta.get().strip() or PORT_PADRAO)
        except ValueError:
            messagebox.showwarning("Clean Remoto", "Porta invalida.")
            return
        self._log("Conectando a %s:%d..." % (ip, porta))
        JanelaViewer(ip, porta, senha, master=self.root).abrir()

    # -- utilitarios -------------------------------------------------------
    def _log(self, msg):
        def escrever():
            self.log.insert("end", time.strftime("[%H:%M:%S] ") + msg + "\n")
            self.log.see("end")
        try:
            self.root.after(0, escrever)
        except Exception:
            pass

    def _sair(self):
        if messagebox.askyesno("Clean Remoto", "Fechar o Clean Remoto?"):
            if self.servidor:
                self.servidor.parar()
            self.root.destroy()

    def rodar(self):
        if not _avisar_dependencias(precisa_entrada=True):
            self.lbl_status.config(text="Faltam componentes (veja o aviso).", fg="#ff8b8b")
        else:
            self._iniciar_servidor()
        self.root.mainloop()


def _avisar_dependencias(precisa_entrada):
    faltando = list(_MISSING)
    if precisa_entrada and _pynput_mouse is None:
        faltando.append("pynput")
    if faltando:
        try:
            messagebox.showerror(
                "Clean Remoto",
                "Faltam componentes. No Prompt de Comando rode:\n\n"
                "pip install -r requirements.txt\n\n"
                "Faltando: " + ", ".join(sorted(set(faltando))) +
                "\n\n(O executavel CleanRemoto.exe ja vem com tudo incluido.)")
        except Exception:
            print("Faltam pacotes:", ", ".join(sorted(set(faltando))))
        return False
    return True


# ===========================================================================
# Linha de comando (opcional) + janela unica
# ===========================================================================
def main(argv=None):
    p = argparse.ArgumentParser(
        description="Clean Remoto - acesso a area de trabalho remota.")
    sub = p.add_subparsers(dest="modo")

    ph = sub.add_parser("host", help="abrir ja pronto para ser acessado")
    ph.add_argument("--senha", help="define a senha (padrao: gera uma)")
    ph.add_argument("--porta", type=int, default=PORT_PADRAO)

    pv = sub.add_parser("viewer", help="acessar outro computador direto")
    pv.add_argument("ip")
    pv.add_argument("--senha", required=True)
    pv.add_argument("--porta", type=int, default=PORT_PADRAO)

    args = p.parse_args(argv)
    if args.modo == "viewer":
        JanelaViewer(args.ip, args.porta, args.senha).rodar()
    elif args.modo == "host":
        JanelaPrincipal(senha=args.senha, porta=args.porta).rodar()
    else:
        JanelaPrincipal().rodar()


if __name__ == "__main__":
    main()
