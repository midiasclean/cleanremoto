#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clean Remoto - acesso a area de trabalho remota (estilo AnyDesk).

UM UNICO programa. Ao abrir, uma janela so mostra:

  * ESTE COMPUTADOR  -> o seu CODIGO. Deixe aberto para que possam te acessar.
  * ACESSAR OUTRO    -> digite o codigo do outro computador e conecte.

Funciona de QUALQUER lugar (internet), nao so na rede local, atraves de uma
"ponte" (relay) publica - do mesmo jeito que o AnyDesk usa os servidores dele.
Cada computador se conecta na ponte (saida pela porta 443/wss, sem abrir porta
no roteador) e registra o seu codigo. Quem acessa informa o codigo; a ponte
liga os dois.

Seguranca (acesso consentido e visivel):
  * Ao conectar, o computador acessado mostra "Fulano quer acessar -
    Permitir / Recusar". Sem autorizar, a sessao nao comeca.
  * Opcional: senha para "permitir automaticamente" (acesso sem ninguem
    presente). A senha nunca trafega em texto puro (desafio/resposta HMAC).
  * A ponte so repassa bytes: nao ve a tela, o teclado nem a senha.

Config da ponte: variavel de ambiente CLEANREMOTO_RELAY, ou campo "Servidor"
na janela (fica salvo). Ex.: wss://minha-ponte.fly.dev
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
import threading
import time
import warnings

# conexao direta do cliente websockets (sem "with") e suportada, mas avisa;
# o aviso nao afeta o funcionamento - silenciamos para o log ficar limpo.
warnings.filterwarnings("ignore", category=DeprecationWarning, module="websockets")

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
try:
    import websockets  # noqa: F401  (ajuda o PyInstaller a incluir)
    from websockets.sync.client import connect as _ws_connect
except Exception:  # pragma: no cover
    _ws_connect = None

import tkinter as tk
from tkinter import messagebox, scrolledtext


# ---------------------------------------------------------------------------
# Protocolo de rede (ponta a ponta, por cima da ponte ou da LAN)
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
MSG_WAIT = 9       # host -> viewer : {texto} aguardando autorizacao ao vivo

_HDR = struct.Struct("!BI")

# Ponte padrao (vazio = precisa configurar). Pode ser trocado na janela ou
# pela variavel de ambiente CLEANREMOTO_RELAY.
RELAY_PADRAO = ""


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
    return hmac.new((senha or "").encode("utf-8"), nonce.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def ips_locais():
    """Lista enderecos IPv4 uteis deste computador (para uso em LAN)."""
    enderecos = set()
    try:
        nome = socket.gethostname()
        for info in socket.getaddrinfo(nome, None, socket.AF_INET):
            enderecos.add(info[4][0])
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        enderecos.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    enderecos.discard("127.0.0.1")
    return sorted(enderecos, key=lambda ip: (not ip.startswith("100."), ip))


# ---------------------------------------------------------------------------
# Configuracao local (codigo do dispositivo + endereco da ponte)
# ---------------------------------------------------------------------------
def _dir_config():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"),
                                                      ".config")
    d = os.path.join(base, "CleanRemoto")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = os.path.expanduser("~")
    return d


def carregar_config():
    try:
        with open(os.path.join(_dir_config(), "config.json"),
                  encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def salvar_config(cfg):
    try:
        with open(os.path.join(_dir_config(), "config.json"), "w",
                  encoding="utf-8") as f:
            json.dump(cfg, f)
    except Exception:
        pass


def meu_codigo(cfg):
    """Codigo fixo deste computador (9 digitos), criado uma vez e guardado."""
    c = str(cfg.get("codigo", "")).strip()
    if not (c.isdigit() and len(c) == 9):
        c = "".join(secrets.choice("0123456789") for _ in range(9))
        cfg["codigo"] = c
        salvar_config(cfg)
    return c


def endereco_ponte(cfg):
    return (os.environ.get("CLEANREMOTO_RELAY")
            or cfg.get("relay") or RELAY_PADRAO).strip()


# ---------------------------------------------------------------------------
# Tunel sobre WebSocket: faz um WebSocket "parecer" um socket (sendall/recv),
# para o resto do programa funcionar sem mudanca, passando pela ponte.
# ---------------------------------------------------------------------------
class TunelWS:
    def __init__(self, ws):
        self.ws = ws
        self._buf = bytearray()

    def setsockopt(self, *a, **k):
        pass

    def sendall(self, data):
        self.ws.send(bytes(data))

    def recv(self, n):
        while not self._buf:
            try:
                msg = self.ws.recv()
            except Exception:
                return b""
            if isinstance(msg, str):
                msg = msg.encode("utf-8")
            if msg:
                self._buf.extend(msg)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass

    def shutdown(self, *a, **k):
        self.close()


def ligar_ponte(url):
    if _ws_connect is None:
        raise RuntimeError("biblioteca 'websockets' ausente")
    return _ws_connect(url, open_timeout=15, max_size=64 * 1024 * 1024)


# ===========================================================================
# LADO HOST  -  compartilha a tela e injeta mouse/teclado
# ===========================================================================
class CapturadorTela:
    """Captura a tela principal como imagem RGB."""

    def __init__(self):
        self._sct = mss.mss() if mss else None

    def info(self):
        if self._sct:
            m = self._sct.monitors[1]
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
    """Fica disponivel (pela ponte ou na LAN) e serve tela + entrada."""

    def __init__(self, senha="", porta=PORT_PADRAO, fps=12, qualidade=55,
                 ao_conectar=None, ao_desconectar=None, ao_log=None,
                 ao_clipboard=None, ao_pedir_permissao=None, auto_aceitar=False,
                 relay_url="", meu_id="", ao_online=None):
        self.senha = senha
        self.porta = porta
        self.intervalo = 1.0 / max(1, fps)
        self.qualidade = qualidade
        self.ao_conectar = ao_conectar or (lambda nome, ip: None)
        self.ao_desconectar = ao_desconectar or (lambda: None)
        self.ao_log = ao_log or (lambda m: None)
        self.ao_clipboard = ao_clipboard or (lambda texto: None)
        self.ao_pedir_permissao = ao_pedir_permissao
        self.auto_aceitar = auto_aceitar
        self.ao_online = ao_online or (lambda online: None)
        self.relay_url = relay_url
        self.meu_id = meu_id
        self._parar = threading.Event()
        self._cliente = None
        self._srv = None
        self._ws_atual = None

    def iniciar(self):
        if self.relay_url and self.meu_id:
            threading.Thread(target=self._loop_relay, daemon=True).start()
        else:
            threading.Thread(target=self._loop_aceitar, daemon=True).start()

    def parar(self):
        self._parar.set()
        for s in (self._cliente, self._srv, self._ws_atual):
            try:
                if s:
                    s.close()
            except Exception:
                pass

    def encerrar_sessao(self):
        """Derruba so a conexao atual, mas continua disponivel."""
        if self._cliente:
            try:
                self._cliente.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self._cliente.close()
            except Exception:
                pass

    # -- modo PONTE (internet, por codigo) ---------------------------------
    def _loop_relay(self):
        while not self._parar.is_set():
            ws = None
            try:
                ws = ligar_ponte(self.relay_url)
                self._ws_atual = ws
                ws.send(json.dumps({"role": "host", "id": self.meu_id}))
                resp = json.loads(ws.recv())
                if resp.get("erro"):
                    self.ao_log("Ponte recusou: %s" % resp["erro"])
                    self.ao_online(False)
                    time.sleep(5)
                    continue
                self.ao_online(True)
                self.ao_log("Online. Seu codigo: %s. Aguardando conexao..."
                            % self.meu_id)
                evt = json.loads(ws.recv())  # bloqueia ate parear ou cair
                if evt.get("evt") != "paired":
                    ws.close()
                    continue
                tunel = TunelWS(ws)
                self._cliente = tunel
                self._atender(tunel, ("remoto", 0))
            except Exception as e:
                if not self._parar.is_set():
                    self.ao_log("Conexao com a ponte caiu: %s" % e)
                self.ao_online(False)
                if not self._parar.is_set():
                    time.sleep(5)
            finally:
                try:
                    if ws:
                        ws.close()
                except Exception:
                    pass
                self._ws_atual = None
                self._cliente = None
                self.ao_desconectar()

    # -- modo LAN (mesma rede, por IP) -------------------------------------
    def _loop_aceitar(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._srv.bind(("0.0.0.0", self.porta))
            self._srv.listen(1)
        except Exception as e:
            self.ao_log("ERRO ao escutar na porta %d: %s" % (self.porta, e))
            return
        self.ao_online(True)
        self.ao_log("Escutando na porta %d (rede local)." % self.porta)
        while not self._parar.is_set():
            try:
                cli, addr = self._srv.accept()
            except Exception:
                break
            cli.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                self._cliente = cli
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

    # -- atendimento comum (igual para ponte e LAN) ------------------------
    def _atender(self, cli, addr):
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
            self.ao_log("Senha incorreta - conexao recusada.")
            time.sleep(1)
            return
        nome = info.get("nome") or str(addr[0])

        # autorizacao AO VIVO (pulada quando "permitir automaticamente")
        if not self.auto_aceitar and self.ao_pedir_permissao is not None:
            enviar_msg(cli, MSG_WAIT,
                       {"texto": "Aguardando o outro computador autorizar o acesso..."})
            self.ao_log("%s pediu acesso. Aguardando sua autorizacao..." % nome)
            try:
                permitido = bool(self.ao_pedir_permissao(nome, str(addr[0])))
            except Exception:
                permitido = False
            if not permitido:
                enviar_msg(cli, MSG_AUTH_FAIL,
                           {"motivo": "acesso recusado no outro computador"})
                self.ao_log("Acesso de %s recusado." % nome)
                return

        cap = CapturadorTela()
        larg, alt = cap.info()
        enviar_msg(cli, MSG_AUTH_OK, {"largura": larg, "altura": alt})
        self.ao_conectar(nome, str(addr[0]))
        self.ao_log("%s entrou. Tela %dx%d sendo compartilhada." % (nome, larg, alt))

        controle = ControleEntrada() if _pynput_mouse else None
        threading.Thread(target=self._receber_entrada, args=(cli, controle),
                          daemon=True).start()

        ultimo_hash = None
        try:
            while not self._parar.is_set() and self._cliente is cli:
                t0 = time.time()
                img = cap.capturar()
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=self.qualidade)
                quadro = buf.getvalue()
                h = hashlib.md5(quadro).digest()
                if h != ultimo_hash:
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
    """Exibe e controla o computador remoto.

    codigo/host : em modo ponte, o CODIGO do outro; em LAN, o IP.
    relay_url   : se preenchido, conecta pela ponte; senao, direto por IP.
    master      : None cria janela propria; caso contrario abre como Toplevel.
    """

    def __init__(self, host, porta, senha="", nome="Clean Remoto",
                 master=None, relay_url=""):
        self.host = host
        self.porta = porta
        self.senha = senha
        self.nome = nome
        self.relay_url = relay_url
        self.sock = None
        self.larg_remota = 0
        self.alt_remota = 0
        self._fila = queue.Queue(maxsize=2)
        self._parar = threading.Event()
        self._img_tk = None
        self._mods = set()
        self._conectado = False

        self.win = tk.Tk() if master is None else tk.Toplevel(master)
        self.win.title("Clean Remoto - conectando...")
        self.win.configure(bg="#111")
        self.win.geometry("1100x700")
        self.canvas = tk.Canvas(self.win, bg="#111", highlightthickness=0,
                                cursor="arrow")
        self.canvas.pack(fill="both", expand=True)
        self._item = self.canvas.create_image(0, 0, anchor="nw")
        self._area = (0, 0, 1, 1)

        self.lbl_status = tk.Label(self.win, text="Conectando...", bg="#111",
                                   fg="#cfe3ff", font=("Segoe UI", 14))
        self.lbl_status.place(relx=0.5, rely=0.5, anchor="center")

        self._ligar_eventos()
        self.win.protocol("WM_DELETE_WINDOW", self.fechar)

    # -- conexao (em segundo plano, para nao travar a janela) --------------
    def _set_status(self, txt):
        def ap():
            if self.lbl_status is not None:
                if txt:
                    self.lbl_status.config(text=txt)
                else:
                    try:
                        self.lbl_status.destroy()
                    except Exception:
                        pass
                    self.lbl_status = None
        try:
            self.win.after(0, ap)
        except Exception:
            pass

    def _abrir_transporte(self):
        if self.relay_url:
            ws = ligar_ponte(self.relay_url)
            ws.send(json.dumps({"role": "viewer", "id": self.host,
                                "nome": self.nome}))
            resp = json.loads(ws.recv())
            if resp.get("erro") == "offline":
                raise ConnectionError("codigo offline ou inexistente")
            if resp.get("erro"):
                raise ConnectionError(resp["erro"])
            if resp.get("evt") != "paired":
                raise ConnectionError("resposta inesperada da ponte")
            return TunelWS(ws)
        tr = socket.create_connection((self.host, self.porta), timeout=10)
        tr.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return tr

    def _handshake(self):
        try:
            tr = self._abrir_transporte()
            tipo, dados = receber_msg(tr)
            if tipo != MSG_HELLO:
                raise ConnectionError("resposta inesperada do host")
            nonce = json.loads(dados.decode("utf-8"))["nonce"]
            enviar_msg(tr, MSG_AUTH,
                       {"resposta": resposta_hmac(self.senha, nonce), "nome": self.nome})
            while True:
                tipo, dados = receber_msg(tr)
                if tipo == MSG_WAIT:
                    try:
                        txt = json.loads(dados.decode("utf-8")).get("texto")
                    except Exception:
                        txt = None
                    self._set_status(txt or "Aguardando autorizacao do outro computador...")
                    continue
                if tipo == MSG_AUTH_FAIL:
                    motivo = json.loads(dados.decode("utf-8")).get("motivo", "recusado")
                    raise ConnectionError(motivo)
                if tipo == MSG_AUTH_OK:
                    info = json.loads(dados.decode("utf-8"))
                    self.larg_remota = info["largura"]
                    self.alt_remota = info["altura"]
                    break
                raise ConnectionError("handshake falhou")
            self.sock = tr
            self.win.after(0, self._iniciar_sessao)
        except Exception as e:
            def erro():
                try:
                    messagebox.showerror("Clean Remoto",
                                         "Nao foi possivel conectar:\n%s" % e)
                except Exception:
                    pass
                self.fechar()
            try:
                self.win.after(0, erro)
            except Exception:
                pass

    def _iniciar_sessao(self):
        self._conectado = True
        self._set_status("")
        self.win.title("Clean Remoto - %s (%dx%d)" %
                       (self.host, self.larg_remota, self.alt_remota))
        threading.Thread(target=self._loop_receber, daemon=True).start()
        self.win.after(30, self._atualizar_tela)
        self.canvas.focus_set()

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
            while True:
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

    def _para_remoto(self, ex, ey):
        x0, y0, nl, na = self._area
        rx = (ex - x0) / nl * self.larg_remota
        ry = (ey - y0) / na * self.alt_remota
        rx = min(max(rx, 0), self.larg_remota - 1)
        ry = min(max(ry, 0), self.alt_remota - 1)
        return int(rx), int(ry)

    def _enviar(self, ev):
        if not self._conectado or self._parar.is_set() or not self.sock:
            return
        try:
            enviar_msg(self.sock, MSG_INPUT, ev)
        except (ConnectionError, OSError):
            self._parar.set()

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
        c.bind("<MouseWheel>", self._scroll)
        c.bind("<Button-4>", lambda e: self._scroll_xy(e, 0, 1))
        c.bind("<Button-5>", lambda e: self._scroll_xy(e, 0, -1))
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
        if self._mods:
            self._enviar({"t": "kdown", "key": ks, "char": e.char})
            self._enviar({"t": "kup", "key": ks, "char": e.char})
            return
        if e.char and e.char.isprintable():
            self._enviar({"t": "type", "text": e.char})
            return
        self._enviar({"t": "kdown", "key": ks, "char": e.char})
        self._enviar({"t": "kup", "key": ks, "char": e.char})

    def _key_release(self, e):
        ks = e.keysym
        if ks in self._MODS:
            self._mods.discard(ks)
            self._enviar({"t": "kup", "key": ks, "char": e.char})

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
        threading.Thread(target=self._handshake, daemon=True).start()

    def rodar(self):
        self.abrir()
        self.win.mainloop()


# ===========================================================================
# JANELA PRINCIPAL  -  tudo em um so lugar, por CODIGO (estilo AnyDesk)
# ===========================================================================
BG = "#0f1b2d"; CARD = "#16263d"; AZUL = "#5db0ff"; VERDE = "#9fffa6"
MUTED = "#9fc0e8"


class JanelaPrincipal:
    def __init__(self):
        self.cfg = carregar_config()
        self.meu_id = meu_codigo(self.cfg)
        self.servidor = None

        self.root = tk.Tk()
        self.root.title("Clean Remoto")
        self.root.configure(bg=BG)
        self.root.geometry("540x640")
        self.root.minsize(500, 600)

        tk.Label(self.root, text="CLEAN REMOTO", bg=BG, fg=AZUL,
                 font=("Segoe UI", 20, "bold")).pack(pady=(14, 0))
        tk.Label(self.root, text="Acesso a area de trabalho remota",
                 bg=BG, fg=MUTED, font=("Segoe UI", 10)).pack(pady=(0, 8))

        # ---- ESTE COMPUTADOR ------------------------------------------
        card1 = tk.Frame(self.root, bg=CARD)
        card1.pack(fill="x", padx=16, pady=6)
        tk.Label(card1, text="ESTE COMPUTADOR", bg=CARD, fg=VERDE,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=14, pady=(12, 0))
        tk.Label(card1, text="Passe este codigo para quem vai te acessar:",
                 bg=CARD, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=14)
        linha_id = tk.Frame(card1, bg=CARD)
        linha_id.pack(anchor="w", padx=14, pady=(2, 2))
        tk.Label(linha_id, text=self._fmt(self.meu_id), bg=CARD, fg="#ffffff",
                 font=("Consolas", 24, "bold")).grid(row=0, column=0)
        tk.Button(linha_id, text="Copiar", command=self._copiar_id,
                  bg="#1e5f8f", fg="white", relief="flat",
                  font=("Segoe UI", 9, "bold")).grid(row=0, column=1, padx=10)

        self.lbl_online = tk.Label(card1, text="Iniciando...", bg=CARD,
                                   fg="#ffd27f", font=("Segoe UI", 10, "bold"))
        self.lbl_online.pack(anchor="w", padx=14, pady=(2, 0))

        self.var_auto = tk.BooleanVar(value=bool(self.cfg.get("auto", False)))
        aut = tk.Frame(card1, bg=CARD)
        aut.pack(fill="x", padx=12, pady=(6, 0))
        tk.Checkbutton(
            aut, variable=self.var_auto, command=self._toggle_auto,
            text="Permitir automaticamente com a senha:", bg=CARD, fg=MUTED,
            selectcolor="#0a1420", activebackground=CARD, activeforeground=VERDE,
            font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w")
        self.e_senha_host = tk.Entry(aut, font=("Consolas", 11), width=10,
                                     justify="center", fg="#0f1b2d")
        self.e_senha_host.insert(0, self.cfg.get("senha_host", ""))
        self.e_senha_host.grid(row=0, column=1, padx=6)

        self.btn_encerrar = tk.Button(card1, text="ENCERRAR SESSAO",
                                      command=self._encerrar, state="disabled",
                                      bg="#c0392b", fg="white", relief="flat",
                                      font=("Segoe UI", 10, "bold"))
        self.btn_encerrar.pack(anchor="w", padx=14, pady=(8, 12))

        # ---- ACESSAR OUTRO --------------------------------------------
        card2 = tk.Frame(self.root, bg=CARD)
        card2.pack(fill="x", padx=16, pady=6)
        tk.Label(card2, text="ACESSAR OUTRO COMPUTADOR", bg=CARD, fg=AZUL,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=14, pady=(12, 0))
        g = tk.Frame(card2, bg=CARD)
        g.pack(fill="x", padx=14, pady=(6, 12))
        tk.Label(g, text="Codigo:", bg=CARD, fg=MUTED).grid(row=0, column=0, sticky="e", pady=3)
        self.e_codigo = tk.Entry(g, font=("Consolas", 14), width=16)
        self.e_codigo.grid(row=0, column=1, padx=6, pady=3, sticky="w")
        tk.Label(g, text="Senha:", bg=CARD, fg=MUTED).grid(row=1, column=0, sticky="e", pady=3)
        self.e_senha = tk.Entry(g, font=("Consolas", 12), width=16)
        self.e_senha.grid(row=1, column=1, padx=6, pady=3, sticky="w")
        tk.Label(g, text="(so se o outro exigir)", bg=CARD, fg="#5a7290",
                 font=("Segoe UI", 8)).grid(row=1, column=2, sticky="w")
        self.btn_conectar = tk.Button(g, text="CONECTAR", command=self._conectar,
                                      bg="#1e6f3f", fg="white", relief="flat",
                                      font=("Segoe UI", 11, "bold"), padx=16, pady=4)
        self.btn_conectar.grid(row=2, column=0, columnspan=2, pady=(8, 0))
        self.e_codigo.bind("<Return>", lambda e: self._conectar())
        self.e_senha.bind("<Return>", lambda e: self._conectar())

        # ---- Servidor (ponte) -----------------------------------------
        card3 = tk.Frame(self.root, bg=BG)
        card3.pack(fill="x", padx=16, pady=(2, 0))
        tk.Label(card3, text="Servidor (ponte):", bg=BG, fg="#5a7290",
                 font=("Segoe UI", 8)).grid(row=0, column=0, sticky="e")
        self.e_relay = tk.Entry(card3, font=("Consolas", 9), width=34)
        self.e_relay.insert(0, endereco_ponte(self.cfg))
        self.e_relay.grid(row=0, column=1, padx=4)
        tk.Button(card3, text="Salvar", command=self._salvar_relay, bg="#24405f",
                  fg="white", relief="flat", font=("Segoe UI", 8)).grid(row=0, column=2)

        self.log = scrolledtext.ScrolledText(self.root, height=5, bg="#0a1420",
                                             fg="#8fb6e0", font=("Consolas", 8),
                                             relief="flat")
        self.log.pack(fill="both", expand=True, padx=16, pady=(8, 14))

        self.root.protocol("WM_DELETE_WINDOW", self._sair)

    # -- helpers de UI -----------------------------------------------------
    @staticmethod
    def _fmt(codigo):
        return " ".join([codigo[0:3], codigo[3:6], codigo[6:9]]) if len(codigo) == 9 else codigo

    def _copiar_id(self):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.meu_id)
            self._log("Codigo copiado: %s" % self.meu_id)
        except Exception:
            pass

    def _log(self, msg):
        def escrever():
            self.log.insert("end", time.strftime("[%H:%M:%S] ") + msg + "\n")
            self.log.see("end")
        try:
            self.root.after(0, escrever)
        except Exception:
            pass

    # -- servidor / ponte --------------------------------------------------
    def _iniciar_servidor(self):
        if self.servidor:
            self.servidor.parar()
            self.servidor = None
        relay = endereco_ponte(self.cfg)
        auto = self.var_auto.get()
        senha = self.e_senha_host.get().strip()
        if auto and not senha:
            messagebox.showwarning(
                "Clean Remoto",
                "Para 'permitir automaticamente' defina uma senha (senao qualquer "
                "um com o seu codigo entra sem pedir autorizacao).")
            self.var_auto.set(False)
            auto = False
        if not relay:
            self.lbl_online.config(text="Configure o servidor (ponte) abaixo.",
                                   fg="#ff8b8b")
            self._log("Sem ponte configurada: informe o endereco (ex.: "
                      "wss://sua-ponte.fly.dev) e clique em Salvar.")
            return
        self.servidor = ServidorHost(
            senha=senha, relay_url=relay, meu_id=self.meu_id,
            ao_conectar=self._ao_conectar, ao_desconectar=self._ao_desconectar,
            ao_log=self._log, ao_clipboard=self._set_clip,
            ao_pedir_permissao=self._pedir_permissao, auto_aceitar=auto,
            ao_online=self._ao_online)
        self.servidor.iniciar()
        self.lbl_online.config(text="Conectando a ponte...", fg="#ffd27f")

    def _toggle_auto(self):
        self.cfg["auto"] = self.var_auto.get()
        self.cfg["senha_host"] = self.e_senha_host.get().strip()
        salvar_config(self.cfg)
        self._iniciar_servidor()  # re-aplica (valida senha)

    def _salvar_relay(self):
        url = self.e_relay.get().strip()
        self.cfg["relay"] = url
        salvar_config(self.cfg)
        self._log("Servidor (ponte) salvo: %s" % (url or "(vazio)"))
        self._iniciar_servidor()

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

    def _ao_online(self, online):
        def ui():
            if online:
                self.lbl_online.config(text="Online - pronto para ser acessado",
                                       fg=VERDE)
            else:
                self.lbl_online.config(text="Offline (tentando reconectar...)",
                                       fg="#ff8b8b")
        try:
            self.root.after(0, ui)
        except Exception:
            pass

    def _ao_conectar(self, nome, ip):
        def ui():
            self.lbl_online.config(text="CONECTADO: %s" % nome, fg=VERDE)
            self.btn_encerrar.config(state="normal")
            try:
                self.root.attributes("-topmost", True)
                self.root.after(1500, lambda: self.root.attributes("-topmost", False))
            except Exception:
                pass
        self.root.after(0, ui)

    def _ao_desconectar(self):
        self.root.after(0, lambda: self.btn_encerrar.config(state="disabled"))

    def _encerrar(self):
        if self.servidor:
            self.servidor.encerrar_sessao()
        self._log("Sessao encerrada por voce.")

    # -- autorizacao ao vivo (Permitir / Recusar) --------------------------
    def _pedir_permissao(self, nome, ip):
        ev = threading.Event()
        res = {"ok": False}
        try:
            self.root.after(0, lambda: self._dialogo_permissao(nome, ip, res, ev))
        except Exception:
            return False
        ev.wait(timeout=75)
        return res["ok"]

    def _dialogo_permissao(self, nome, ip, res, ev, segundos=60):
        top = tk.Toplevel(self.root)
        top.title("Pedido de acesso")
        top.configure(bg=CARD)
        top.attributes("-topmost", True)
        top.resizable(False, False)
        try:
            top.grab_set()
        except Exception:
            pass
        tk.Label(top, text="Pedido de acesso", bg=CARD, fg=AZUL,
                 font=("Segoe UI", 14, "bold")).pack(padx=24, pady=(18, 4))
        tk.Label(top, text=str(nome), bg=CARD, fg="#ffffff",
                 font=("Segoe UI", 12, "bold")).pack(padx=24)
        tk.Label(top, text="quer acessar e controlar este computador.",
                 bg=CARD, fg=MUTED, font=("Segoe UI", 10)).pack(padx=24, pady=(0, 10))
        lbl_count = tk.Label(top, text="", bg=CARD, fg="#ffd27f",
                             font=("Segoe UI", 9))
        lbl_count.pack(pady=(0, 8))
        estado = {"left": segundos, "fim": False}

        def decidir(ok):
            if estado["fim"]:
                return
            estado["fim"] = True
            res["ok"] = ok
            ev.set()
            try:
                top.grab_release()
            except Exception:
                pass
            try:
                top.destroy()
            except Exception:
                pass

        linha = tk.Frame(top, bg=CARD)
        linha.pack(padx=24, pady=(0, 18))
        tk.Button(linha, text="RECUSAR", command=lambda: decidir(False),
                  bg="#c0392b", fg="white", relief="flat", padx=16, pady=6,
                  font=("Segoe UI", 11, "bold")).grid(row=0, column=0, padx=8)
        tk.Button(linha, text="PERMITIR", command=lambda: decidir(True),
                  bg="#1e6f3f", fg="white", relief="flat", padx=16, pady=6,
                  font=("Segoe UI", 11, "bold")).grid(row=0, column=1, padx=8)
        top.protocol("WM_DELETE_WINDOW", lambda: decidir(False))

        def tic():
            if estado["fim"]:
                return
            estado["left"] -= 1
            if estado["left"] <= 0:
                decidir(False)
                return
            lbl_count.config(text="Recusado automaticamente em %ds" % estado["left"])
            top.after(1000, tic)
        lbl_count.config(text="Recusado automaticamente em %ds" % segundos)
        top.after(1000, tic)
        try:
            top.bell()
        except Exception:
            pass

    # -- viewer ------------------------------------------------------------
    def _conectar(self):
        if not _avisar_dependencias(precisa_entrada=False):
            return
        codigo = "".join(ch for ch in self.e_codigo.get() if ch.isdigit())
        senha = self.e_senha.get().strip()
        relay = endereco_ponte(self.cfg)
        if not codigo:
            messagebox.showwarning("Clean Remoto", "Informe o codigo do outro computador.")
            return
        if not relay:
            messagebox.showwarning("Clean Remoto",
                                   "Configure o servidor (ponte) antes de conectar.")
            return
        nome = "%s (%s)" % (socket.gethostname(), self.meu_id)
        self._log("Conectando ao codigo %s..." % codigo)
        JanelaViewer(codigo, PORT_PADRAO, senha=senha, nome=nome,
                     master=self.root, relay_url=relay).abrir()

    def _sair(self):
        if messagebox.askyesno("Clean Remoto", "Fechar o Clean Remoto?"):
            if self.servidor:
                self.servidor.parar()
            self.root.destroy()

    def rodar(self):
        if not _avisar_dependencias(precisa_entrada=True):
            self.lbl_online.config(text="Faltam componentes (veja o aviso).",
                                   fg="#ff8b8b")
        else:
            self._iniciar_servidor()
        self.root.mainloop()


def _avisar_dependencias(precisa_entrada):
    faltando = list(_MISSING)
    if _ws_connect is None:
        faltando.append("websockets")
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

    sub.add_parser("app", help="abrir a janela (padrao)")

    pr = sub.add_parser("relay", help="rodar a ponte (servidor) - veja relay/relay.py")
    pr.add_argument("--porta", type=int, default=int(os.environ.get("PORT", "8765")))

    pv = sub.add_parser("viewer", help="acessar por IP na rede local (avancado)")
    pv.add_argument("ip")
    pv.add_argument("--senha", default="")
    pv.add_argument("--porta", type=int, default=PORT_PADRAO)

    args = p.parse_args(argv)
    if args.modo == "viewer":
        JanelaViewer(args.ip, args.porta, senha=args.senha).rodar()
    elif args.modo == "relay":
        print("Para rodar a ponte use: python relay/relay.py")
    else:
        JanelaPrincipal().rodar()


if __name__ == "__main__":
    main()
