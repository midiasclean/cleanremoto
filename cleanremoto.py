#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clean Remoto - acesso a area de trabalho remota.

Um unico programa que funciona de dois jeitos:

  * PERMITIR ACESSO (host)  -> compartilha a tela deste computador e deixa
                               outra pessoa controlar mouse e teclado.
  * ACESSAR OUTRO (viewer)  -> conecta no computador que permitiu acesso e
                               o controla.

Projetado para ser usado SOBRE a VPN do kit "acesso externo" (Tailscale /
WireGuard): os dois computadores ja se enxergam pelo IP da VPN, entao nao
precisa abrir porta no roteador nem de servidor intermediario.

Principios de seguranca (acesso consentido e visivel):
  * O host so aceita a conexao com a SENHA que ele mesmo mostra na tela.
  * A senha nunca trafega em texto puro: usa desafio/resposta com HMAC.
  * Enquanto alguem esta conectado, o host mostra uma janela bem visivel
    com o botao "ENCERRAR SESSAO" sempre a mao.
  * Nada de instalacao escondida, servico furtivo ou operacao invisivel.

Requisitos: Python 3.9+ e os pacotes de requirements.txt
  (pip install -r requirements.txt)
"""

import argparse
import hashlib
import hmac
import io
import json
import os
import queue
import secrets
import socket
import struct
import sys
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

    _BOTOES = {}
    _ESPECIAIS = {}

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
                 ao_conectar=None, ao_desconectar=None, ao_log=None):
        self.senha = senha
        self.porta = porta
        self.intervalo = 1.0 / max(1, fps)
        self.qualidade = qualidade
        self.ao_conectar = ao_conectar or (lambda nome, ip: None)
        self.ao_desconectar = ao_desconectar or (lambda: None)
        self.ao_log = ao_log or (lambda m: None)
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
                    self._definir_clipboard(dados.decode("utf-8", "ignore"))
                elif tipo == MSG_PING:
                    pass
        except (ConnectionError, OSError):
            pass

    @staticmethod
    def _definir_clipboard(texto):
        try:
            r = tk.Tk()
            r.withdraw()
            r.clipboard_clear()
            r.clipboard_append(texto)
            r.update()
            r.destroy()
        except Exception:
            pass


# ===========================================================================
# LADO VIEWER  -  mostra a tela do host e envia mouse/teclado
# ===========================================================================
class JanelaViewer:
    def __init__(self, host, porta, senha, nome="Clean Remoto"):
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

        self.root = tk.Tk()
        self.root.title("Clean Remoto - conectando a %s..." % host)
        self.root.configure(bg="#111")
        self.root.geometry("1100x700")
        self.canvas = tk.Canvas(self.root, bg="#111", highlightthickness=0,
                                cursor="arrow")
        self.canvas.pack(fill="both", expand=True)
        self._item = self.canvas.create_image(0, 0, anchor="nw")

        # area desenhada (para mapear coordenadas) : x0,y0,larg,alt
        self._area = (0, 0, 1, 1)

        self._ligar_eventos()
        self.root.protocol("WM_DELETE_WINDOW", self.fechar)

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
            return False
        self.root.title("Clean Remoto - %s (%dx%d)" %
                        (self.host, self.larg_remota, self.alt_remota))
        threading.Thread(target=self._loop_receber, daemon=True).start()
        self.root.after(30, self._atualizar_tela)
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
                self.root.after(0, lambda: self.root.title(
                    "Clean Remoto - DESCONECTADO"))
            except Exception:
                pass

    def _atualizar_tela(self):
        if self._parar.is_set():
            return
        try:
            img = None
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
        self.root.after(30, self._atualizar_tela)

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
        self.root.bind("<KeyPress>", self._key_press)
        self.root.bind("<KeyRelease>", self._key_release)
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
        if e.char and e.char.isprintable() and e.char != "":
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
                self.root.clipboard_clear()
                self.root.clipboard_append(texto)
            except Exception:
                pass
        try:
            self.root.after(0, aplicar)
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
            self.root.destroy()
        except Exception:
            pass

    def rodar(self):
        if self.conectar():
            self.root.mainloop()


# ===========================================================================
# JANELA DO HOST  -  indicador visivel de consentimento
# ===========================================================================
class JanelaHost:
    def __init__(self, senha=None, porta=PORT_PADRAO):
        self.senha = senha or ("%06d" % secrets.randbelow(1000000))
        self.porta = porta
        self.root = tk.Tk()
        self.root.title("Clean Remoto - permitindo acesso a este computador")
        self.root.configure(bg="#0f1b2d")
        self.root.geometry("560x520")
        self.root.attributes("-topmost", True)

        tk.Label(self.root, text="CLEAN REMOTO", bg="#0f1b2d", fg="#5db0ff",
                 font=("Segoe UI", 20, "bold")).pack(pady=(18, 2))
        tk.Label(self.root, text="Este computador esta pronto para ser acessado",
                 bg="#0f1b2d", fg="#cfe3ff", font=("Segoe UI", 11)).pack()

        quadro = tk.Frame(self.root, bg="#16263d")
        quadro.pack(fill="x", padx=20, pady=16)
        tk.Label(quadro, text="Informe estes dados a quem vai acessar:",
                 bg="#16263d", fg="#9fc0e8", font=("Segoe UI", 10)).pack(
                     anchor="w", padx=14, pady=(12, 4))

        ips = ips_locais() or ["(descubra o IP desta maquina)"]
        tk.Label(quadro, text="Endereco (IP):", bg="#16263d", fg="#9fc0e8",
                 font=("Segoe UI", 10)).pack(anchor="w", padx=14)
        self.lbl_ip = tk.Label(quadro, text="   " + "\n   ".join(ips),
                                bg="#16263d", fg="#ffffff",
                                font=("Consolas", 13, "bold"), justify="left")
        self.lbl_ip.pack(anchor="w", padx=14, pady=(0, 8))

        tk.Label(quadro, text="Senha de acesso:", bg="#16263d", fg="#9fc0e8",
                 font=("Segoe UI", 10)).pack(anchor="w", padx=14)
        tk.Label(quadro, text="   " + self.senha, bg="#16263d", fg="#9fffa6",
                 font=("Consolas", 22, "bold")).pack(anchor="w", padx=14, pady=(0, 8))
        tk.Label(quadro, text="Porta: %d" % self.porta, bg="#16263d",
                 fg="#9fc0e8", font=("Segoe UI", 9)).pack(anchor="w", padx=14,
                                                          pady=(0, 12))

        self.lbl_status = tk.Label(self.root, text="Aguardando conexao...",
                                   bg="#0f1b2d", fg="#ffd27f",
                                   font=("Segoe UI", 12, "bold"))
        self.lbl_status.pack(pady=6)

        self.btn_encerrar = tk.Button(self.root, text="ENCERRAR SESSAO",
                                      command=self._encerrar, state="disabled",
                                      bg="#c0392b", fg="white",
                                      font=("Segoe UI", 12, "bold"),
                                      activebackground="#e74c3c", relief="flat",
                                      padx=18, pady=8)
        self.btn_encerrar.pack(pady=6)

        self.log = scrolledtext.ScrolledText(self.root, height=7, bg="#0a1420",
                                             fg="#8fb6e0", font=("Consolas", 9),
                                             relief="flat")
        self.log.pack(fill="both", expand=True, padx=20, pady=(8, 16))

        self.servidor = ServidorHost(
            self.senha, self.porta,
            ao_conectar=self._ao_conectar,
            ao_desconectar=self._ao_desconectar,
            ao_log=self._log)
        self.root.protocol("WM_DELETE_WINDOW", self._sair)

    def _log(self, msg):
        def escrever():
            self.log.insert("end", time.strftime("[%H:%M:%S] ") + msg + "\n")
            self.log.see("end")
        try:
            self.root.after(0, escrever)
        except Exception:
            pass

    def _ao_conectar(self, nome, ip):
        def ui():
            self.lbl_status.config(text="CONECTADO: %s (%s)" % (nome, ip),
                                   fg="#9fffa6")
            self.btn_encerrar.config(state="normal")
            self.root.attributes("-topmost", True)
        self.root.after(0, ui)

    def _ao_desconectar(self):
        def ui():
            self.lbl_status.config(text="Aguardando conexao...", fg="#ffd27f")
            self.btn_encerrar.config(state="disabled")
        self.root.after(0, ui)

    def _encerrar(self):
        self.servidor.encerrar_sessao()
        self._log("Sessao encerrada pelo usuario deste computador.")

    def _sair(self):
        if messagebox.askyesno("Clean Remoto",
                               "Parar de permitir acesso e fechar?"):
            self.servidor.parar()
            self.root.destroy()

    def rodar(self):
        self.servidor.iniciar()
        self.root.mainloop()


# ===========================================================================
# MENU INICIAL  -  escolhe entre host e viewer
# ===========================================================================
class MenuInicial:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Clean Remoto")
        self.root.configure(bg="#0f1b2d")
        self.root.geometry("460x440")

        tk.Label(self.root, text="CLEAN REMOTO", bg="#0f1b2d", fg="#5db0ff",
                 font=("Segoe UI", 22, "bold")).pack(pady=(26, 2))
        tk.Label(self.root, text="Acesso a area de trabalho remota",
                 bg="#0f1b2d", fg="#cfe3ff", font=("Segoe UI", 11)).pack(pady=(0, 20))

        tk.Button(self.root, text="PERMITIR QUE ME ACESSEM\n(compartilhar este computador)",
                  command=self._abrir_host, bg="#1e6f3f", fg="white",
                  font=("Segoe UI", 12, "bold"), activebackground="#2ecc71",
                  relief="flat", padx=10, pady=14, width=34).pack(pady=8)

        tk.Label(self.root, text="— ou —", bg="#0f1b2d", fg="#5a7290",
                 font=("Segoe UI", 10)).pack(pady=4)

        quadro = tk.Frame(self.root, bg="#16263d")
        quadro.pack(fill="x", padx=24, pady=6)
        tk.Label(quadro, text="Acessar outro computador", bg="#16263d",
                 fg="#cfe3ff", font=("Segoe UI", 11, "bold")).grid(
                     row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(10, 6))
        tk.Label(quadro, text="IP:", bg="#16263d", fg="#9fc0e8").grid(
            row=1, column=0, sticky="e", padx=(10, 4), pady=3)
        self.e_ip = tk.Entry(quadro, font=("Consolas", 11), width=22)
        self.e_ip.grid(row=1, column=1, sticky="w", pady=3)
        tk.Label(quadro, text="Senha:", bg="#16263d", fg="#9fc0e8").grid(
            row=2, column=0, sticky="e", padx=(10, 4), pady=3)
        self.e_senha = tk.Entry(quadro, font=("Consolas", 11), width=22)
        self.e_senha.grid(row=2, column=1, sticky="w", pady=3)
        tk.Label(quadro, text="Porta:", bg="#16263d", fg="#9fc0e8").grid(
            row=3, column=0, sticky="e", padx=(10, 4), pady=3)
        self.e_porta = tk.Entry(quadro, font=("Consolas", 11), width=22)
        self.e_porta.insert(0, str(PORT_PADRAO))
        self.e_porta.grid(row=3, column=1, sticky="w", pady=3)
        tk.Button(quadro, text="CONECTAR", command=self._abrir_viewer,
                  bg="#1e5f8f", fg="white", font=("Segoe UI", 11, "bold"),
                  activebackground="#3498db", relief="flat", padx=10, pady=6).grid(
                      row=4, column=0, columnspan=2, pady=12)

    def _abrir_host(self):
        if not _avisar_dependencias(precisa_entrada=True):
            return
        self.root.destroy()
        JanelaHost().rodar()

    def _abrir_viewer(self):
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
        self.root.destroy()
        JanelaViewer(ip, porta, senha).rodar()

    def rodar(self):
        self.root.mainloop()


def _avisar_dependencias(precisa_entrada):
    faltando = list(_MISSING)
    if mss is None and "pillow" not in faltando:
        pass  # pillow cobre o fallback de captura
    if precisa_entrada and _pynput_mouse is None:
        faltando.append("pynput")
    if faltando:
        try:
            messagebox.showerror(
                "Clean Remoto",
                "Faltam componentes. No Prompt de Comando rode:\n\n"
                "pip install -r requirements.txt\n\n"
                "Faltando: " + ", ".join(sorted(set(faltando))))
        except Exception:
            print("Faltam pacotes:", ", ".join(sorted(set(faltando))))
        return False
    return True


# ===========================================================================
# Linha de comando (opcional) + menu grafico
# ===========================================================================
def main(argv=None):
    p = argparse.ArgumentParser(
        description="Clean Remoto - acesso a area de trabalho remota.")
    sub = p.add_subparsers(dest="modo")

    ph = sub.add_parser("host", help="permitir que acessem este computador")
    ph.add_argument("--senha", help="define a senha (padrao: gera uma)")
    ph.add_argument("--porta", type=int, default=PORT_PADRAO)

    pv = sub.add_parser("viewer", help="acessar outro computador")
    pv.add_argument("ip")
    pv.add_argument("--senha", required=True)
    pv.add_argument("--porta", type=int, default=PORT_PADRAO)

    args = p.parse_args(argv)
    if args.modo == "host":
        JanelaHost(senha=args.senha, porta=args.porta).rodar()
    elif args.modo == "viewer":
        JanelaViewer(args.ip, args.porta, args.senha).rodar()
    else:
        MenuInicial().rodar()


if __name__ == "__main__":
    main()
