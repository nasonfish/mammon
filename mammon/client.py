#!/usr/bin/env python
# mammon - a useless ircd
#
# Copyright (c) 2015, William Pitcock <nenolod@dereferenced.org>
#
# Permission to use, copy, modify, and/or distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

import asyncio
import logging
import time
import socket

from ircreactor.envelope import RFC1459Message
from .server import eventmgr, get_context
from . import __version__

REGISTRATION_LOCK_NICK = 0x1
REGISTRATION_LOCK_USER = 0x2
REGISTRATION_LOCK_DNS  = 0x4

# XXX - handle ping timeout
# XXX - exit_client() could eventually be handled using eventmgr.dispatch()
class ClientProtocol(asyncio.Protocol):
    def connection_made(self, transport):
        self.ctx = get_context()

        self.peername = transport.get_extra_info('peername')
        self.transport = transport
        self.recvq = list()
        self.channels = list()
        self.nickname = '*'
        self.username = str()
        self.hostname = self.peername[0]  # XXX - handle rdns...
        self.realaddr = self.peername[0]
        self.realname = '<unregistered>'

        self.registered = False
        self.registration_lock = 0
        self.push_registration_lock(REGISTRATION_LOCK_NICK | REGISTRATION_LOCK_USER | REGISTRATION_LOCK_DNS)

        logging.debug('new inbound connection from {}'.format(self.peername))

        asyncio.async(self.do_rdns_check())

    def do_rdns_check(self):
        self.dump_notice('Looking up your hostname...')

        rdns = yield from self.ctx.eventloop.getnameinfo(self.peername)
        logging.debug(repr(rdns))

        if rdns[0] == self.realaddr:
            self.dump_notice('Could not find your hostname...')
            self.release_registration_lock(REGISTRATION_LOCK_DNS)
            return

        fdns = yield from self.ctx.eventloop.getaddrinfo(rdns[0], rdns[1], proto=socket.IPPROTO_TCP)
        for fdns_e in fdns:
            if fdns_e[4][0] == self.realaddr:
                self.dump_notice('Found your hostname: ' + rdns[0])
                self.hostname = rdns[0]
                self.release_registration_lock(REGISTRATION_LOCK_DNS)
                return

        self.dump_notice('Could not find your hostname...')
        self.release_registration_lock(REGISTRATION_LOCK_DNS)

    def data_received(self, data):
        [self.message_received(m) for m in data.splitlines()]

    def message_received(self, data):
        m = RFC1459Message.from_message(data.decode('UTF-8', 'replace').strip('\r\n'))
        m.client = self

        logging.debug('client {0} --> {1}'.format(repr(self.__dict__), repr(m.serialize())))
        if len(self.recvq) > self.ctx.conf.recvq_len:
            self.exit_client('Excess flood')
            return

        self.recvq.append(m)

        # XXX - drain_queue should be called on all objects at once to enforce recvq limits
        self.drain_queue()

    def drain_queue(self):
        while self.recvq:
            m = self.recvq.pop(0)
            eventmgr.dispatch(*m.to_event())

    def dump_message(self, m):
        self.transport.write(bytes(m.to_message() + '\r\n', 'UTF-8'))

    def dump_numeric(self, numeric, params):
        msg = RFC1459Message.from_data(numeric, source=self.ctx.conf.name, params=[self.nickname] + params)
        self.dump_message(msg)

    def dump_notice(self, message):
        msg = RFC1459Message.from_data('NOTICE', source=self.ctx.conf.name, params=[self.nickname, '*** ' + message])
        self.dump_message(msg)

    @property
    def hostmask(self):
        if not self.registered:
            return None
        hm = self.nickname
        if self.username:
            hm += '!' + self.username
            if self.hostname:
                hm += '@' + self.hostname
        return hm

    def exit_client(self, message):
        m = RFC1459Message.from_data('QUIT', source=self.hostmask, params=[message])
        self.dump_message(m)
        while self.channels:
            i = self.channels.pop(0)
            i.clients.pop(self)
            i.dump_message(m)
        self.transport.close()
        if self.registered:
            self.ctx.clients.pop(self.nickname)

    def push_registration_lock(self, lock):
        if self.registered:
            return
        self.registration_lock |= lock

    def release_registration_lock(self, lock):
        if self.registered:
            return
        self.registration_lock &= ~lock
        if not self.registration_lock:
            self.register()

    def sendto_common_peers(self, message):
        [i.dump_message(message) for i in self.channels]

    def dump_isupport(self):
        isupport_tokens = {'NETWORK': self.ctx.conf.network, 'CLIENTVER': '3.2', 'CASEMAPPING': 'ascii', 'CHARSET': 'utf-8', 'SAFELIST': True}
        # XXX - split into multiple 005 lines if > 13 tokens
        def format_token(k, v):
            if isinstance(v, bool):
                return k
            return '{0}={1}'.format(k, v)

        self.dump_numeric('005', [format_token(k, v) for k, v in isupport_tokens.items()] + ['are supported by this server'])

    def register(self):
        self.registered = True
        self.ctx.clients[self.nickname] = self

        self.dump_numeric('001', ['Welcome to the ' + self.ctx.conf.network + ' IRC Network, ' + self.hostmask])
        self.dump_numeric('002', ['Your host is ' + self.ctx.conf.name + ', running version mammon-' + str(__version__)])
        self.dump_numeric('003', ['This server was started at ' + self.ctx.startstamp])
        # XXX - numeric 004
        self.dump_isupport()