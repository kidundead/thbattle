# -*- coding: utf-8 -*-

import gevent
from gevent import monkey
monkey.patch_all()

import random
import re
from gevent.pool import Pool
from gevent.coros import RLock
from gevent.queue import Queue
from cStringIO import StringIO
from deathbycaptcha import SocketClient as DBCClient
from qqbot import QQBot
import argparse
import logging
import redis
import sys
from functools import partial
from contextlib import contextmanager

from utils.interconnect import Interconnect as InterconnectBase
from utils import check, CheckFailed

# import httplib
# httplib.HTTPConnection.debuglevel = 1


parser = argparse.ArgumentParser('aya')
parser.add_argument('--qq', type=int)
parser.add_argument('--password')
parser.add_argument('--dbc-username')
parser.add_argument('--dbc-password')
parser.add_argument('--redis', default='localhost')
parser.add_argument('--redis-port', default=6379)
parser.add_argument('--member-service', default='localhost')
options = parser.parse_args()

log = logging.getLogger('Aya')
pool = Pool(5)

Interconnect = None


@contextmanager
def member_client_pool():
    global _cli_pool

    if not _cli_pool:
        from utils.rpc import RPCClient
        _cli_pool = Queue(5)
        for i in xrange(5):
            _cli_pool.put(RPCClient((options.member_service, 7000), timeout=6))

    try:
        cli = _cli_pool.get()
        yield cli
    finally:
        _cli_pool.put(cli)

_cli_pool = None


class AyaDAO(object):
    def __init__(self):
        self.redis = redis.Redis(options.redis, options.redis_port)

    def get_binding(self, qq):
        rst = self.redis.hget('aya:binding', int(qq))
        try:
            return int(rst)
        except:
            return None

    def get_all_bindings(self):
        return self.redis.hgetall('aya:binding')

    def set_binding(self, qq, uid):
        self.redis.hset('aya:binding', int(qq), int(uid))

    def is_group_on(self, group_num):
        return self.redis.sismember('aya:group_on', int(group_num))

    def get_all_groups_on(self):
        rst = self.redis.smembers('aya:group_on')
        if not rst:
            return []

        return [int(i) for i in rst]

    def set_group_on(self, group_num):
        self.redis.sadd('aya:group_on', int(group_num))

    def set_group_off(self, group_num):
        self.redis.srem('aya:group_on', int(group_num))


dao = AyaDAO()


class Aya(QQBot):
    def on_captcha(self, image):
        logging.info('Solving captcha...')
        f = StringIO()
        f.write(image)
        f.seek(0)
        dbccli = DBCClient(options.dbc_username, options.dbc_password)

        try:
            captcha = dbccli.decode(f, 60)
        except:
            log.exception('Error solving captcha')
            sys.exit(1)

        if captcha:
            self.captcha = captcha
            return captcha['text'], captcha

        sys.exit(1)

    def on_captcha_wrong(self, tag):
        log.info('Captcha wrong!')
        dbccli = DBCClient(options.dbc_username, options.dbc_password)
        dbccli.report(tag['captcha'])

    def polling_hook(self, f, v):
        pool.apply_async(f, (v,))

    def on_login(self):
        refresh = pool.map_async(lambda f: f(), [
            self.refresh_buddy_list,
            self.refresh_group_list,
        ])

        @pool.apply_async
        def _():
            refresh.get()
            Pool(2).map_async(self.gcode2groupnum, [g['code'] for g in aya.group_list])
            Pool(2).map_async(self.uin2qq, [i['uin'] for i in aya.buddy_list])

        global Interconnect
        Interconnect = AyaInterconnect.spawn('aya', options.redis, options.redis_port)

    def on_sess_message(self, msg):
        text = (
            u'文文不认识你，不会理你哦。\n'
            u'加好友的时候验证信息里像这样(文文求交朋友 3456 mima)填上你的论坛uid和密码，不要带括号，文文就会跟你做朋友。'
        )

        self.send_sess_message(msg['id'], msg['from_uin'], text)

    def on_message(self, msg):
        text = (
            u'文文最近很忙，没法跟你闲聊啦……\n'
            u'有空了会告诉你哦～'
        )

        self.send_buddy_message(msg['from_uin'], text)

    def on_group_message(self, msg):
        content = self._plaintext(msg['content']).strip()
        if not content:
            return

        if content.startswith(u'呼叫文文'):
            pool.apply_async(self.send_group_message, (msg['from_uin'], u'文文在哦'))

        elif content == u'文文on':
            superusers = self.get_group_superusers_uin(msg['group_code'])
            if msg['send_uin'] in superusers or self.uin2qq(msg['send_uin']) in (84065234,):
                dao.set_group_on(self.gcode2groupnum(msg['group_code']))
                pool.apply_async(self.send_group_message, (msg['from_uin'], u'收到～文文会以最快速度播报新闻～'))

        elif content == u'文文off':
            gnum = self.gcode2groupnum(msg['group_code'])
            if dao.is_group_on(gnum):
                dao.set_group_off(gnum)
                pool.apply_async(self.send_group_message, (msg['from_uin'], u'哼，不理你们了。管理员叫我我才回来。哼。'))

        elif content[0] in (u'`', u'•'):
            pool.apply_async(self.do_speaker, (msg['send_uin'], content[1:], msg['from_uin']))

    def on_system_message(self, msg):
        if msg['type'] == 'verify_required':
            qq = self.uin2qq(msg['from_uin'])

            def fail():
                self.deny_friend_request(qq, u'好友请求填写的不对，文文不要跟你做朋友。')
                return False

            def success():
                self.allow_friend_request(qq)
                self.refresh_buddy_list()
                return True

            req = msg['msg'].split(None, 2)
            req = [i.strip() for i in req]
            try:
                check(len(req) == 3)
                check(req[0] == u'文文求交朋友')
                check(req[1].isdigit())
            except CheckFailed:
                return fail()

            uid = int(req[1])
            pwd = req[2]
            with member_client_pool() as cli:
                member = cli.validate_by_uid(uid, pwd)

            if not member:
                return fail()

            dao.set_binding(qq, uid)

            return success()

    def do_speaker(self, uin, content, group_uin=None):
        fail_text = u'文文不认识你，才不帮你发新闻呢。'
        insufficient_funds_text = u'你的节操掉了一地，才不帮你发新闻呢。'
        friend_uins = [i['uin'] for i in self.buddy_list]
        if uin not in friend_uins:
            group_uin and self.send_group_message(group_uin, fail_text)
            return

        qq = self.uin2qq(uin)
        uid = dao.get_binding(qq)
        if not uid:
            # not bound, but are friends
            # delete him.
            self.delete_friend(uin)
            self.refresh_buddy_list()
            return

        foo = str(random.randint(0x10000000, 0xffffffff))
        content = content.replace('||', foo)
        content = re.sub(r'([\r\n]|\|(c[A-Fa-f0-9]{8}|s[12][A-Fa-f0-9]{8}|[BbIiUuHrRGYW]|LB|DB|![RGOB]))', '', content)
        content = content.replace(foo, '||')

        with member_client_pool() as cli:
            member = cli.get_user_info(uid)
            if member['credits'] < 10:
                group_uin and self.send_group_message(group_uin, insufficient_funds_text)
                return

            cli.add_credit(uid, 'credits', -10)
            Interconnect.publish('speaker', [member['username'], content])


class AyaInterconnect(InterconnectBase):
    lock = None

    def on_message(self, node, topic, message):
        if topic == 'speaker':
            from settings import ServerNames
            username, content = message

            send = u'{}『文々。新闻』{}： {}'.format(
                ServerNames.get(node, node), username, content,
            )

            groups_on = [int(i) for i in dao.get_all_groups_on()]
            gids = [
                g['gid'] for g in aya.group_list
                if aya.gcode2groupnum(g['code']) in groups_on
            ]

            pool.map_async(lambda f: f(), [
                partial(aya.send_group_message, i, send)
                for i in gids
            ])

    def publish(self, topic, data):
        lock = self.lock
        if not lock:
            lock = RLock()
            self.lock = lock

        with lock:
            return InterconnectBase.publish(self, topic, data)


logging.basicConfig(level=logging.DEBUG)

from gevent.backdoor import BackdoorServer
gevent.spawn(BackdoorServer(('127.0.0.1', 11111)).serve_forever)

aya = Aya(options.qq, options.password)
aya.loop()
