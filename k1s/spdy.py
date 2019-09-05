# Copyright 2019 Red Hat
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

# A bare minimal SPDY implementation for cherrypy suitable for k1s purpose

import cherrypy
import inspect
import logging
import struct
import threading
import zlib
from typing import Dict, Tuple

from cherrypy import Tool
from cheroot.server import HTTPConnection, HTTPRequest, KnownLengthRFile


log = logging.getLogger("K1S.spdy")


class SPDYTool(Tool):
    """Adapted from ws4py WebSocketTool"""
    def __init__(self):
        Tool.__init__(self, 'before_request_body', self.upgrade)

    def _setup(self):
        conf = self._merged_args()
        hooks = cherrypy.serving.request.hooks
        p = conf.pop("priority", getattr(
            self.callable, "priority", self._priority))
        hooks.attach(self._point, self.callable, priority=p, **conf)
        hooks.attach('before_finalize', self.complete, priority=p)
        hooks.attach('on_end_request', self.start_handler, priority=70)

    def upgrade(self, handler_cls):
        """Intercep the request and detach it from cherrypy"""
        request = cherrypy.serving.request
        request.process_request_body = False
        response = cherrypy.serving.response
        response.stream = True
        response.status = '101 Switching Protocols'
        response.headers['Upgrade'] = 'SPDY/3.1'
        response.headers['Connection'] = 'Upgrade'

        addr = (request.remote.ip, request.remote.port)
        rfile = request.rfile.rfile
        if isinstance(rfile, KnownLengthRFile):
            rfile = rfile.rfile
        conn = rfile.raw._sock
        request.spdy_handler = handler_cls(conn, addr)

    def start_handler(self):
        cherrypy.request.rfile.rfile.detach()

    def complete(self):
        current = inspect.currentframe()
        while True:
            if not current:
                break
            _locals = current.f_locals
            if 'self' in _locals:
                if isinstance(_locals['self'], HTTPRequest):
                    _locals['self'].close_connection = True
                if isinstance(_locals['self'], HTTPConnection):
                    _locals['self'].linger = True
                    return
            _locals = None
            current = current.f_back


FrameType = int
FrameFlag = int
FrameData = bytes
FrameInfo = Tuple[FrameType, FrameFlag, FrameData]
StreamId = int
StreamName = str
StreamInfo = Tuple[StreamId, StreamName]


class SPDYHandler:
    """An handler to takes care of the client socket"""
    # https://www.chromium.org/spdy/spdy-protocol/spdy-protocol-draft3-1
    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.zs = zlib.decompressobj(zdict=SPDY_dictionary)

    def read(self, count: int) -> bytes:
        chunks = []
        rcv = 0
        while rcv < count:
            chunk = self.sock.recv(count - rcv)
            if not chunk:
                raise RuntimeError("Socket connection closed")
            chunks.append(chunk)
            rcv += len(chunk)
        return b''.join(chunks)

    def sendFrame(self, streamId: StreamId, data: bytes):
        log.debug("OutFrame: %s", data)
        pck = struct.pack('!I', streamId) + b'\x00' + struct.pack(
            '!I', len(data))[1:] + data
        self.sock.sendall(pck)

    def readFrame(self, version: bytes) -> FrameInfo:
        header = self.read(8)
        if header[:2] != version:
            raise RuntimeError("Invalid frame", header)
        ptype, pflag = struct.unpack('!HB', header[2:5])
        plen = struct.unpack('!I', b'\x00' + header[5:8])[0]
        pdata = self.read(plen)
        log.debug("InFrame: %s", header)
        # print(header + pdata)
        return ptype, pflag, pdata

    def readDataFrame(self) -> FrameInfo:
        return self.readFrame(b'\x00\x00')

    def readControlFrame(self) -> FrameInfo:
        return self.readFrame(b'\x80\x03')

    def readStreamPacket(self) -> StreamInfo:
        ptype, pflag, data = self.readControlFrame()
        if ptype != 1:
            raise RuntimeError("Not a stream packet")
        # Parse SYN STREAM
        streamId, assocStreamId = struct.unpack('!II', data[:8])
        # print("streamId:", streamId, "assoc", assocStreamId)
        nvh = self.zs.decompress(data[10:]) + self.zs.flush(zlib.Z_SYNC_FLUSH)

        pair_num = struct.unpack('!I', nvh[:4])[0]
        if pair_num != 1:
            raise RuntimeError("Multiple value for stream")

        nam_len = struct.unpack('!I', nvh[4:8])[0]
        name = nvh[8:8+nam_len]
        if name != b"streamtype":
            raise RuntimeError("Unknown stream value")

        val_len = struct.unpack('!I', nvh[8+nam_len:12+nam_len])[0]
        val_name = nvh[12+nam_len:12+nam_len+val_len]
        # print(pair_num, name, val_name)
        # Sending SYN_REPLY
        syn_data = data[:4] + data[10:]
        syn_reply = b'\x80\x03\x00\x02\x00' + struct.pack(
            '!I', len(syn_data))[1:] + syn_data
        # print("OutPacket:")
        # print(syn_reply)
        self.sock.sendall(syn_reply)
        return val_name.decode('utf-8'), streamId

    def handle(self, args: Dict[str, str]):
        """Start thread from cherrypy controller"""
        # TODO: implement a manager to garbage collect bad streams
        self.args = args
        threading.Thread(target=self.run).start()

    def run(self):
        raise NotImplementedError("SPDYHandler needs to implement run")


SPDY_dictionary = (
    b'\x00\x00\x00\x07options\x00\x00\x00\x04head\x00\x00\x00\x04p'
    b'ost\x00\x00\x00\x03put\x00\x00\x00\x06delete\x00\x00\x00\x05trac'
    b'e\x00\x00\x00\x06accept\x00\x00\x00\x0eaccept-charset\x00\x00\x00'
    b'\x0faccept-encoding\x00\x00\x00\x0faccept-language\x00\x00\x00\ra'
    b'ccept-ranges\x00\x00\x00\x03age\x00\x00\x00\x05allow\x00\x00\x00\r'
    b'authorization\x00\x00\x00\rcache-control\x00\x00\x00\nconnection'
    b'\x00\x00\x00\x0ccontent-base\x00\x00\x00\x10content-encoding'
    b'\x00\x00\x00\x10content-language\x00\x00\x00\x0econtent-leng'
    b'th\x00\x00\x00\x10content-location\x00\x00\x00\x0bcontent-md'
    b'5\x00\x00\x00\rcontent-range\x00\x00\x00\x0ccontent-type\x00\x00'
    b'\x00\x04date\x00\x00\x00\x04etag\x00\x00\x00\x06expect'
    b'\x00\x00\x00\x07expires\x00\x00\x00\x04from\x00\x00\x00\x04h'
    b'ost\x00\x00\x00\x08if-match\x00\x00\x00\x11if-modified-since'
    b'\x00\x00\x00\rif-none-match\x00\x00\x00\x08if-range\x00\x00\x00'
    b'\x13if-unmodified-since\x00\x00\x00\rlast-modified\x00\x00\x00'
    b'\x08location\x00\x00\x00\x0cmax-forwards\x00\x00\x00\x06pragma\x00'
    b'\x00\x00\x12proxy-authenticate\x00\x00\x00\x13proxy-authorization'
    b'\x00\x00\x00\x05range\x00\x00\x00\x07referer\x00\x00\x00\x0bretr'
    b'y-after\x00\x00\x00\x06server\x00\x00\x00\x02te\x00\x00\x00\x07t'
    b'railer\x00\x00\x00\x11transfer-encoding\x00\x00\x00\x07upgra'
    b'de\x00\x00\x00\nuser-agent\x00\x00\x00\x04vary\x00\x00\x00\x03'
    b'via\x00\x00\x00\x07warning\x00\x00\x00\x10www-authenticate\x00\x00'
    b'\x00\x06method\x00\x00\x00\x03get\x00\x00\x00\x06statu'
    b's\x00\x00\x00\x06200 OK\x00\x00\x00\x07version\x00\x00\x00\x08HT'
    b'TP/1.1\x00\x00\x00\x03url\x00\x00\x00\x06public\x00\x00\x00\nset-c'
    b'ookie\x00\x00\x00\nkeep-alive\x00\x00\x00\x06origin100101201202205'
    b'2063003023033043053063074024054064074084094104114124134144154164'
    b'17502504505203 Non-Authoritative Information204 No Content301 Mo'
    b'ved Permanently400 Bad Request401 Unauthorized403 Forbidden404 N'
    b'ot Found500 Internal Server Error501 Not Implemented503 Service '
    b'UnavailableJan Feb Mar Apr May Jun Jul Aug Sept Oct Nov Dec 00:0'
    b'0:00 Mon, Tue, Wed, Thu, Fri, Sat, Sun, GMTchunked,text/html,ima'
    b'ge/png,image/jpg,image/gif,application/xml,application/xhtml+xml'
    b',text/plain,text/javascript,publicprivatemax-age=gzip,deflate,sd'
    b'chcharset=utf-8charset=iso-8859-1,utf-,*,enq=0.')
