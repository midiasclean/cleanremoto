#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clean Remoto - servidor "ponte" (relay).

Funciona como os servidores do AnyDesk: cada computador se conecta aqui
(saida para a internet, pela porta 443/wss) e registra o seu CODIGO. Para
acessar, o outro computador informa esse codigo; a ponte liga os dois e
repassa os bytes entre eles. Assim funciona de QUALQUER rede, mesmo atras
de NAT/CGNAT, sem abrir porta no roteador.

A ponte NAO ve nada do conteudo: so repassa bytes. A senha/autorizacao
continuam sendo tratadas pelos dois computadores (ponta a ponta).

Protocolo (texto JSON na 1a mensagem, depois bytes crus):
  host   ->  {"role":"host","id":"<codigo>"}       ; recebe {"evt":"online"}
  viewer ->  {"role":"viewer","id":"<codigo>"}      ; recebe {"evt":"paired"}
                                                    ; ou {"erro":"offline"}
  Ao parear, a ponte manda {"evt":"paired"} para o host e repassa tudo.

Rodar:  python relay.py      (porta pela variavel PORT, padrao 8765)
"""
import asyncio
import json
import os

import websockets


class Sessao:
    __slots__ = ("host", "viewer", "pareado")

    def __init__(self, host):
        self.host = host
        self.viewer = None
        self.pareado = asyncio.Event()


# codigo -> Sessao (host aguardando um viewer)
AGUARDANDO = {}


async def repassar(origem, destino):
    """Lê mensagens de `origem` e reenvia para `destino` até uma cair."""
    try:
        async for msg in origem:
            await destino.send(msg)
    except Exception:
        pass
    finally:
        try:
            await destino.close()
        except Exception:
            pass


async def handler(ws):
    try:
        primeira = await ws.recv()
        info = json.loads(primeira)
    except Exception:
        return
    papel = info.get("role")
    codigo = str(info.get("id", "")).strip()
    if not codigo:
        await _enviar(ws, {"erro": "codigo ausente"})
        return

    if papel == "host":
        # Se ja houver um host online com esse codigo, substitui o antigo.
        antigo = AGUARDANDO.get(codigo)
        if antigo is not None:
            try:
                await antigo.host.close()
            except Exception:
                pass
        sess = Sessao(ws)
        AGUARDANDO[codigo] = sess
        await _enviar(ws, {"evt": "online"})
        # espera um viewer parear OU a conexao cair
        esperar_par = asyncio.ensure_future(sess.pareado.wait())
        esperar_queda = asyncio.ensure_future(ws.wait_closed())
        try:
            await asyncio.wait([esperar_par, esperar_queda],
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            esperar_par.cancel()
            esperar_queda.cancel()
        if AGUARDANDO.get(codigo) is sess and sess.viewer is None:
            del AGUARDANDO[codigo]  # host caiu antes de parear
        if sess.viewer is None:
            return
        # pareado: repassa host -> viewer
        await repassar(ws, sess.viewer)

    elif papel == "viewer":
        sess = AGUARDANDO.get(codigo)
        if sess is None or sess.viewer is not None:
            await _enviar(ws, {"erro": "offline"})
            return
        sess.viewer = ws
        del AGUARDANDO[codigo]  # ocupado; nao aceita outro viewer
        await _enviar(ws, {"evt": "paired"})
        await _enviar(sess.host, {"evt": "paired"})
        sess.pareado.set()
        # repassa viewer -> host
        await repassar(ws, sess.host)
    else:
        await _enviar(ws, {"erro": "papel invalido"})


async def _enviar(ws, obj):
    try:
        await ws.send(json.dumps(obj))
    except Exception:
        pass


async def _health(connection, request):
    """Responde 200 em GET comum (checagem de saude das plataformas).

    Conexoes WebSocket trazem o cabecalho `Upgrade: websocket` e devem
    passar direto (retornando None); so o GET comum recebe o 200.
    """
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return None
    return connection.respond(200, "Clean Remoto relay OK\n")


async def main():
    porta = int(os.environ.get("PORT", "8765"))
    async with websockets.serve(handler, "0.0.0.0", porta,
                                process_request=_health,
                                ping_interval=20, ping_timeout=20,
                                max_size=64 * 1024 * 1024):
        print("Clean Remoto relay ouvindo na porta %d" % porta, flush=True)
        await asyncio.Future()  # roda para sempre


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
