import re, os, os.path, sys, threading, hmac, cPickle
from base64 import b64encode, b64decode
from binascii import hexlify
from random import random
from time import time, sleep
from urllib import quote_plus, unquote_plus

from pony.thirdparty import simplejson

import pony
from pony import options, httputils
from pony.utils import compress, decompress, simple_decorator, localbase
from pony.sessionstorage import ramstorage as storage
from pony.logging import log_exc

hash = pony.options.HASH_ALGORITHM
if hash is None:
    try: from hashlib import sha1 as hash
    except ImportError: import sha as hash

class Session(object):
    def __init__(self, dict=None, **keyargs):
        if dict: self.__dict__.update(dict)
        self.__dict__.update(keyargs)
    def __call__(self, key, default):
        return self.__dict__.get(key, default)
    def __getitem__(self, key):
        return self.__dict__[key]
    def __setitem__(self, key, value):
        self.__dict__[key] = value
    def __getattr__(self, attr):
        return self.__dict__.get(attr)
    def __setattr__(self, attr, value):
        if value is None: self.__dict__.pop(attr, None)
        else: self.__dict__[attr] = value
    def __contains__(self, key):
        return key in self.__dict__
    def __iter__(self):
        return iter(self.__dict__.keys())
    def __len__(self):
        return len(self.__dict__)
    def clear(self):
        self.__dict__.clear()

class Local(localbase):
    def __init__(self):
        self.lock = threading.Lock()
        self.lock.acquire()
        self.clear()
    def clear(self):
        now = int(time()) // 60
        lock = self.lock
        self.__dict__.clear()
        self.__dict__.update(lock=lock, user=None, environ={}, session=Session(), ctime=now, mtime=now,
                             cookie_value=None, remember_ip=False, longlife_session=False, longlife_key=None,
                             ip=None, user_agent=None, ticket=False, ticket_payload=None)
    def set_user(self, user, longlife_session=False, remember_ip=False):
        if self.user is not None or user is None: self.session.clear()
        self.user = user
        if user:
            self.longlife_session = longlife_session
            self.remember_ip = remember_ip
        elif self.longlife_key: remove_longlife_session()
        else: self.longlife_session = self.remember_ip = False

local = Local()

secret_cache = {}

set_user = local.set_user

def get_user():
    return local.user

def get_session():
    return local.session

def get_hashobject(minute):
    hashobject = secret_cache.get(minute) or _get_hashobject(minute)
    return hashobject.copy()

def load(environ, cookies=None):
    local.clear()
    local.environ = environ
    if cookies is None:
        cookies =  Cookie.SimpleCookie()
        if 'HTTP_COOKIE' in environ: cookies.load(environ['HTTP_COOKIE'])
    morsel = cookies.get(options.COOKIE_NAME)
    local.ip = ip = environ.get('REMOTE_ADDR')
    local.user_agent = user_agent = environ.get('HTTP_USER_AGENT')
    local.cookie_value = cookie_value = morsel and morsel.value or None
    if not cookie_value: return
    now = int(time()) // 60
    try:
        ctime_str, mtime_str, data_str, hash_str, longlife_key = cookie_value.split(':')
        ctime = local.ctime = int(ctime_str, 16)
        mtime = local.mtime = int(mtime_str, 16)
        ctime_diff = now - ctime
        mtime_diff = now - mtime
        if ctime_diff < -1 or mtime_diff < -1: return
        if ctime_diff > options.MAX_SESSION_CTIME or mtime_diff > options.MAX_SESSION_MTIME:
            resurrect_longlife_session(longlife_key); return
        data = b64decode(data_str)
        hash = b64decode(hash_str)
        hashobject = get_hashobject(mtime)
        hashobject.update(ctime_str)
        hashobject.update(data)
        hashobject.update(user_agent or '')
        if hash != hashobject.digest():
            hashobject.update(ip or '')
            if hash != hashobject.digest(): return
            local.remember_ip = True
        else: local.remember_ip = False
        if data.startswith('C'): data = data[1:]
        elif data.startswith('S'): data = storage.getdata(data[1:], ctime, mtime)
        else: return
        info = loads(data)
        local.user, session_dict = info
        local.session = Session(session_dict)
        local.longlife_key = longlife_key or None
        local.longlife_session = bool(longlife_key)
    except:
        log_exc()
        return

def set_longlife_session():
    if local.user is not None:
        data = dumps(local.user)
        ip = local.remember_ip and local.ip or None
        id, rnd = _create_longlife_session(data, ip)
        local.longlife_key = '%x+%s' % (id, b64encode(rnd))
        local.longlife_session = True
    else:
        local.longlife_session = False
        local.longlife_key = None

def resurrect_longlife_session(key):
    if not key: return
    try:
        id, rnd = key.split('+')
        id = int(id, 16)
        rnd = b64decode(rnd)
    except: return
    data, ip = _get_longlife_session(id, rnd)
    if data is None: return
    if ip:
        if ip != local.ip: return
        local.remember_ip = True
    local.user = loads(data)
    local.longlife_session = True
    local.longlife_key = key

def remove_longlife_session():
    local.longlife_session = False
    key = local.longlife_key
    if not key: return
    local.longlife_key = None
    try:
        id, rnd = key.split('+')
        id = int(id, 16)
        rnd = b64decode(rnd)
    except: return
    _remove_longlife_session(id, rnd)

def save(cookies):
    now = int(time()) // 60
    ctime_str = '%x' % local.ctime
    mtime_str = '%x' % now
    if local.user is not None or local.session:
        if local.user and local.longlife_session:
            if not local.longlife_key: set_longlife_session()
            longlife_key = local.longlife_key or ''
        else: longlife_key = ''

        info = local.user, local.session.__dict__
        data = dumps(info)
        hashobject = get_hashobject(now)
        hashobject.update(ctime_str)

        total_size = len(ctime_str)+len(mtime_str)+(1+int(len(data)*1.37)+3)+hashobject.digest_size+len(longlife_key)+4
        if total_size <= options.MAX_COOKIE_SIZE: data = 'C' + data
        else: data = 'S' + storage.putdata(data, local.ctime, now)

        hashobject.update(data)
        hashobject.update(local.user_agent or '')
        if local.remember_ip: hashobject.update(local.ip or '')
        data_str = b64encode(data)
        hash_str = b64encode(hashobject.digest())
        cookie_value = ':'.join([ ctime_str, mtime_str, data_str, hash_str, longlife_key ])
    else: cookie_value = ''
    if cookie_value != local.cookie_value:
        max_time = (options.MAX_LONGLIFE_SESSION+1)*24*60*60
        httputils.set_cookie(cookies, options.COOKIE_NAME, cookie_value, max_time, max_time,
                            options.COOKIE_PATH, options.COOKIE_DOMAIN, http_only=True)

def get_ticket(payload=None, prevent_resubmit=False):
    if not payload: payload = ''
    else:
        assert isinstance(payload, str)
        payload = compress(payload)
        
    now = int(time()) // 60
    now_str = '%x' % now
    rnd = os.urandom(8)
    hashobject = get_hashobject(now)
    hashobject.update(rnd)
    hashobject.update(payload)
    hashobject.update(dumps(local.user))
    if prevent_resubmit: hashobject.update('+')
    hash = hashobject.digest()

    payload_str = b64encode(payload)
    rnd_str = b64encode(rnd)
    hash_str = b64encode(hash)
    return ':'.join((now_str, payload_str, rnd_str, hash_str))

def verify_ticket(ticket_str):
    now = int(time()) // 60
    try:
        time_str, payload_str, rnd_str, hash_str = ticket_str.split(':')
        minute = int(time_str, 16)
        if minute < now - options.MAX_SESSION_MTIME or minute > now + 1: return
        rnd = b64decode(rnd_str)
        if len(rnd) != 8: return
        payload = b64decode(payload_str)
        hash = b64decode(hash_str)
        hashobject = get_hashobject(minute)
        hashobject.update(rnd)
        hashobject.update(payload)
        hashobject.update(dumps(local.user))
        if hash != hashobject.digest():
            hashobject.update('+')
            if hash != hashobject.digest(): return
            result = _verify_ticket(minute, rnd)
            if not result: local.ticket = result; return
        if payload: payload = decompress(payload)
        local.ticket = minute, rnd
        local.ticket_payload = payload or None
    except: return

def unexpire_ticket():
    if not local.ticket: return
    minute, rnd = local.ticket
    _unexpire_ticket(minute, rnd)

def loads(s):
    type = options.COOKIE_SERIALIZATION_TYPE
    if type == 'json': return simplejson.loads(decompress(s))
    elif type == 'pickle': return cPickle.loads(decompress(s))
    else: raise TypeError("Incorrect value of pony.options.COOKIE_SERIALIZATION_TYPE (must be 'json' or 'pickle')")

def dumps(obj):
    type = options.COOKIE_SERIALIZATION_TYPE
    if type == 'json': return compress(simplejson.dumps(obj, separators=(',', ':')))
    elif type == 'pickle': return compress(cPickle.dumps(obj, 2))
    else: raise TypeError("Incorrect value of pony.options.COOKIE_SERIALIZATION_TYPE (must be 'json' or 'pickle')")
    
if not pony.MODE.startswith('GAE-'):

    from Queue import Queue
    queue = Queue()

    @simple_decorator
    def exec_in_auth_thread(f, *args, **keyargs):
        result_holder = []
        queue.put((local.lock, f, args, keyargs, result_holder))
        local.lock.acquire()
        return result_holder[0]

    @simple_decorator
    def exec_async(f, *args, **keyargs):
        queue.put((None, f, args, keyargs, None))

    connection = None

    @exec_in_auth_thread
    def _verify_ticket(minute, rnd):
        rnd = buffer(rnd)
        row = connection.execute('select rowid from used_tickets where minute = ? and rnd = ?', [minute, rnd]).fetchone()
        if row is None: connection.execute('insert or ignore into used_tickets values(?, ?)', [minute, rnd])
        connection.commit()
        return row is None and True or None

    @exec_async
    def _unexpire_ticket(minute, rnd):
        connection.execute('delete from used_tickets where minute = ? and rnd = ?', [minute, buffer(rnd)])
        connection.commit()

    @exec_in_auth_thread
    def _get_hashobject(minute):
        result = secret_cache.get(minute)
        if result: return result
        row = connection.execute('select secret from time_secrets where minute = ?', [minute]).fetchone()
        if row is None:
            now = int(time()) // 60
            secret = os.urandom(32)
            connection.execute('delete from used_tickets where minute < ?', [ now - options.MAX_SESSION_MTIME ])
            connection.execute('delete from time_secrets where minute < ?', [ now - options.MAX_SESSION_MTIME ])
            connection.execute('insert or ignore into time_secrets values(?, ?)', [ minute, buffer(secret) ])
            row = connection.execute('select secret from time_secrets where minute = ?', [minute]).fetchone()
            connection.commit()
        else: connection.rollback()
        secret = str(row[0])
        secret_cache[minute] = result = hmac.new(secret, digestmod=hash)
        return result

    @exec_in_auth_thread
    def _get_longlife_session(id, rnd):
        row = connection.execute('select rnd, ctime, data, ip from longlife_sessions where id=?', [ id ]).fetchone()
        if row is None: connection.rollback(); return None, None
        rnd2, ctime, data, ip = row
        if buffer(rnd) != rnd2: connection.rollback(); return None, None
        now = int(time() // 60)
        old = now - options.MAX_LONGLIFE_SESSION*24*60
        if ctime < old:
            connection.execute('delete from longlife_sessions where ctime < ?', [ old ])
            connection.commit(); return None, None
        return str(data), ip

    @exec_in_auth_thread
    def _create_longlife_session(data, ip):
        rnd = os.urandom(8)
        now = int(time() // 60)
        cursor = connection.execute('insert into longlife_sessions(rnd, ctime, ip, data) values(?, ?, ?, ?)',
                                    [ buffer(rnd), now, ip, buffer(data) ])
        id = cursor.lastrowid
        connection.commit()
        return id, rnd

    @exec_in_auth_thread
    def _remove_longlife_session(id, rnd):
        connection.execute('delete from longlife_sessions where id = ? and rnd = ?', [ id, buffer(rnd) ])
        connection.commit()

    def get_sessiondb_name():
        # This function returns relative path, if possible.
        # It is workaround for bug in SQLite
        # (Problems with unicode symbols in directory name)
        if pony.MAIN_FILE is None: return ':memory:'
        root, ext = os.path.splitext(pony.MAIN_FILE)
        if pony.MODE != 'MOD_WSGI': root = os.path.basename(root)
        return root + '-secrets.sqlite'

    sql_create = """
    create table if not exists time_secrets (
        minute integer primary key,
        secret blob not null    
        );
    create table if not exists used_tickets (
        minute integer not null,
        rnd    blob  not null,
        primary key (minute, rnd)
        );
    create table if not exists longlife_sessions (
        id    integer primary key,
        rnd   blob    not null,
        ctime integer not null,
        ip    text,
        data  blob    not null
        );
    create index if not exists longlife_sessions_ctime on longlife_sessions(ctime);
    """

    class AuthThread(threading.Thread):
        def __init__(self):
            threading.Thread.__init__(self, name="AuthThread")
            self.setDaemon(True)
        def run(self):
            from pony.thirdparty import sqlite
            global connection
            connection = sqlite.connect(get_sessiondb_name())
            try:
                connection.execute("PRAGMA synchronous = OFF;")
                connection.executescript(sql_create)
                for minute, secret in connection.execute('select * from time_secrets'):
                    secret_cache[minute] = hmac.new(str(secret), digestmod=hash)
                connection.commit()
                while True:
                    x = queue.get()
                    if x is None: break
                    lock, func, args, keyargs, result_holder = x
                    while True:
                        try: result = func(*args, **keyargs)
                        except sqlite.OperationalError:
                            connection.rollback()
                            sleep(random())
                        else: break
                    if result_holder is not None: result_holder.append(result)
                    if lock is not None: lock.release()
            finally: connection.close()

    @pony.on_shutdown
    def do_shutdown():
        queue.put(None)
        auth_thread.join()

    auth_thread = AuthThread()
    auth_thread.start()

else:
    from google.appengine.ext import db
    from google.appengine.api import users

    class PonyTimeSecret(db.Model):
        minute = db.IntegerProperty(required=True)
        secret = db.BlobProperty(required=True)

    class PonyUsedTicket(db.Model):
        minute = db.IntegerProperty(required=True)
        rnd = db.BlobProperty(required=True)

    class PonyLonglifeSession(db.Model):
        rnd = db.BlobProperty(required=True)
        ctime = db.IntegerProperty(required=True)
        ip = db.TextProperty()
        data = db.BlobProperty(required=True)

    for time_secret in PonyTimeSecret.all():
        secret_cache[time_secret.minute] = hmac.new(time_secret.secret, digestmod=hash)

    def _verify_ticket(minute, rnd):
        keystr = 'm%s_%s' % (minute, hexlify(rnd))
        ticket = PonyUsedTicket.get_by_key_name(keystr)
        if ticket is None:
            while True:
                try: PonyUsedTicket(key_name=keystr, minute=minute, rnd=rnd).put()
                except db.TransactionFailedError: pass
                else: break
                if PonyUsedTicket.get_by_key_name(keystr) is not None: break
        return not ticket and True or None

    def _unexpire_ticket(minute, rnd):
        keystr = 'm%s_%s' % (minute, hexlify(rnd))
        ticket = PonyUsedTicket.get_by_key_name([keystr])
        if not ticket: return
        try: db.delete(ticket)
        except db.TransactionFailedError: pass

    def _get_hashobject(minute):
        hashobject = secret_cache.get(minute)
        if hashobject is not None: return hashobject.copy()

        keystr = 'm%s' % minute
        secretobj = PonyTimeSecret.get_by_key_name(keystr)
        if secretobj is None:
            now = int(time()) // 60
            old = now - options.MAX_SESSION_MTIME
            secret = os.urandom(32)
            for ticket in PonyUsedTicket.gql('where minute < :1', minute):
                try: db.delete(ticket)
                except db.TransactionFailedError: pass
            for secretobj in PonyTimeSecret.gql('where minute < :1', minute):
                try: db.delete(secretobj)
                except db.TransactionFailedError: pass
            while True:
                try: secretobj = PonyTimeSecret.get_or_insert(keystr, minute=minute, secret=secret)
                except db.TransactionFailedError: continue
                else: break
        hashobject = hmac.new(secretobj.secret, digestmod=hash)
        secret_cache[minute] = hashobject
        return hashobject.copy()

    def _get_longlife_session(id, rnd):
        sessionobj = PonyLonglifeSession.get_by_id(id)
        if sessionobj is None: return None, None
        if rnd != sessionobj.rnd: return None, None
        now = int(time() // 60)
        old = now - options.MAX_LONGLIFE_SESSION*24*60
        if sessionobj.ctime < old:
            for secretobj in PonyLonglifeSession.gql('where ctime < :1', old):
                try: db.delete(sessionobj)
                except db.TransactionFailedError: pass
        return sessionobj.data, sessionobj.ip

    def _create_longlife_session(data, ip):
        rnd = os.urandom(8)
        now = int(time() // 60)
        while True:
            try: key = PonyLonglifeSession(rnd=rnd, ctime=now, ip=ip, data=data).put()
            except db.TransactionFailedError: continue  # is try..except necessary here?
            else: break
        return key.id(), rnd

    def _remove_longlife_session(id, rnd):
        try:
            sessionobj = PonyLonglifeSession.get_by_id(id)
            if sessionobj is not None: sessionobj.delete()
        except db.TransactionFailedError: pass  # is try..except necessary here?
